"""ffmpeg commands used by the render pipeline.

Every command is built by a pure `*_args` function (tested directly, including exact parity with
Phase 1 for default edits) and executed by a thin wrapper. Numbers are converted with float() before
formatting: Fraction only accepts format specs from Python 3.12. Commands are always argument lists,
never shell strings. File paths are only ever separate arguments or concat-list entries,
never embedded inside filter expressions.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Sequence

from ..core.errors import ToolError
from ..core.proc import run_cancellable
from ..media.source import input_args
from ..project.edit import Export
from ..tools.encoders import TWO_PASS_ENCODERS, bitrate_args, encoder_args
from .graph import RenderPlan

AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k"]
HOLD_LAST_FRAME = "tpad=stop_mode=clone:stop=-1"  # lets fps fill the final slots of a re-timed clip

Cmd = list  # list of str | Path


def part_path(path: Path) -> Path:
    return path.with_name(path.stem + ".part" + path.suffix)


def progress_snapshot(progress_file: Path) -> tuple:
    """(frame, total_size, out_time_us) from the latest -progress block. ffmpeg keeps appending
    blocks even when stuck, so the values, not the file size, show whether it is moving."""
    try:
        text = progress_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    latest: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("frame", "total_size", "out_time_us"):
            latest[key] = value
    return (latest.get("frame"), latest.get("total_size"), latest.get("out_time_us"))


def read_progress_frame(progress_file: Path) -> int | None:
    """Last `frame=` value from an ffmpeg `-progress` file."""
    try:
        text = progress_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if line.startswith("frame="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------------- input windows

def video_window_args(plan: RenderPlan) -> list[str]:
    """Input options selecting exactly the window's frames (seek half a frame early so rounding
    can never drop or add a frame)."""
    w = plan.window
    if w.start_frame > 0:
        return ["-ss", f"{float((w.start_frame - 0.5) / w.fps):.6f}", "-t", f"{float(w.frames / w.fps):.6f}"]
    return ["-t", f"{float((w.frames - 0.5) / w.fps):.6f}"]


def audio_window_args(plan) -> list[str]:
    """Input options for the audio covering exactly the same time span as the video window."""
    w = plan.window
    args = ["-ss", f"{w.start:.6f}"] if w.start_frame > 0 else []
    return args + ["-t", f"{w.duration:.6f}"]


def _output_limit(plan, max_seconds: float | None) -> list[str]:
    """Phase 1 previews cut the *output* with -t; keep that exactly at normal speed."""
    if not max_seconds or plan.trimmed:
        return []
    if plan.rate == 1 and getattr(plan, "out_fps", None) is None:
        return ["-t", f"{max_seconds:.3f}"]
    return ["-t", f"{plan.out_duration:.6f}"]


def _vf(filters: Sequence[str]) -> list[str]:
    return ["-vf", ",".join(filters)] if filters else []


def _af(filters: Sequence[str]) -> list[str]:
    return ["-af", ",".join(filters)] if filters else []


# ------------------------------------------------------------------------- single pass (no AI)
# ------------------------------------------------------------------------- encoding settings

def audio_codec_args(fmt: str, export: Export | None) -> list[str]:
    """Audio encoding for a container. The default (MP4, 192 kb/s) is exactly Phase 1's AUDIO_ARGS."""
    kbps = export.audio_bitrate if export else 192
    if fmt == "webm":
        return ["-c:a", "libopus", "-b:a", f"{kbps}k"]
    return ["-c:a", "aac", "-b:a", f"{kbps}k"]


def container_args(fmt: str) -> list[str]:
    if fmt in ("mp4", "mov"):
        return ["-movflags", "+faststart"]
    if fmt == "gif":
        return ["-loop", "0"]
    return []


def video_codec_args(plan: RenderPlan, encoder: str, quality: int, export: Export | None, *,
                     kbps: int | None = None, pass_no: int | None = None) -> list[str]:
    """Video encoding: constant quality (Phase 1), average bitrate, or one pass of a 2-pass encode."""
    if plan.fmt == "gif":
        return []
    codec = plan.video_codec if export is not None else encoder
    if kbps is None:
        args = encoder_args(codec, quality)
    else:
        args = bitrate_args(codec, kbps)
        if pass_no is not None:
            args += ["-pass", str(pass_no), "-passlogfile", PASSLOG]
    return args + ["-pix_fmt", "yuv420p"]


PASSLOG = "fwpass"  # relative: 2-pass encodes run in the job's work folder


def target_kbps(export: Export, duration: float, has_audio: bool) -> int:
    """Video bitrate for 'target size' exports: ~2% container overhead, minus the audio track.
    The result is approximate: encoders cannot hit an exact size."""
    if duration <= 0:
        raise ValueError("Cannot size an export with no duration")
    total_kbit = export.target_mb * 8 * 1024 * 1024 / 1000 * 0.98
    audio = export.audio_bitrate if has_audio else 0
    return int(total_kbit / duration - audio)


MIN_VIDEO_KBPS = 100


# ------------------------------------------------------------------------- single pass (no AI)

def transcode_args(ffmpeg: Path, src, part: Path, progress: Path, plan: RenderPlan, *,
                   encoder: str, quality: int, has_audio: bool, max_seconds: float | None,
                   export: Export | None = None, kbps: int | None = None, pass_no: int | None = None) -> Cmd:
    cmd: Cmd = [ffmpeg, "-v", "error", "-y", "-nostats", "-progress", progress]
    if plan.trimmed:
        cmd += [*video_window_args(plan), *input_args(src)]
        if has_audio:
            cmd += [*audio_window_args(plan), *input_args(src)]
        a_map = "1:a:0"
    else:
        cmd += input_args(src)
        a_map = "0:a:0"
    cmd += _output_limit(plan, max_seconds)
    cmd += ["-map", "0:v:0"] + (["-map", a_map] if has_audio else [])
    vf = plan.video_filters(before_ai=True) + plan.video_filters(before_ai=False) + plan.reverse_filters()
    if plan.rate != 1:
        vf += plan.timing_filters(0) + [HOLD_LAST_FRAME]
    vf += plan.final_filters()
    cmd += _vf(vf)
    if plan.rate != 1 or plan.trimmed or plan.out_fps is not None:
        cmd += ["-frames:v", str(plan.out_frames)]
    cmd += video_codec_args(plan, encoder, quality, export, kbps=kbps, pass_no=pass_no)
    if pass_no == 1:
        return cmd + ["-an", "-f", "null", "-"]
    if has_audio:
        cmd += _af(plan.audio_filters) + audio_codec_args(plan.fmt, export)
    cmd += [*container_args(plan.fmt), part]
    return cmd


def stabilize_analysis_args(ffmpeg: Path, src, plan: RenderPlan) -> Cmd:
    """First pass of stabilization: measure camera motion into transforms.trf (in the work folder)."""
    cmd: Cmd = [ffmpeg, "-v", "error", "-y"]
    cmd += [*video_window_args(plan)] if plan.trimmed else []
    cmd += [*input_args(src), "-map", "0:v:0", "-an", *_vf(plan.analysis_filters), "-f", "null", "-"]
    return cmd


def transcode(ffmpeg: Path, src, out: Path, plan: RenderPlan, *, encoder: str, quality: int,
              has_audio: bool, max_seconds: float | None, work_dir: Path,
              cancel: threading.Event | None = None, on_frames: Callable[[int], None] | None = None,
              export: Export | None = None, on_stage: Callable[[str], None] | None = None) -> None:
    """Render without AI upscaling.

    Default edits: one ffmpeg pass, exactly as in Phase 1. When the audio needs filtering (trim,
    speed, volume, fades...), the video is rendered first and the processed audio is added in a
    second, copy-only pass (the same audio step the AI path uses): filtering audio and video in one
    process can deadlock ffmpeg 8 (atempo + padding). Stabilization adds a motion-analysis pass
    first; 'target size' with x264/VP9 adds a first encoding pass."""
    work_dir.mkdir(parents=True, exist_ok=True)
    prog = work_dir / "progress.txt"
    part = part_path(out)
    has_audio = has_audio and not plan.mute
    two_pass_audio = has_audio and bool(plan.audio_filters)
    video_out = work_dir / f"video{out.suffix}" if two_pass_audio else part
    video_part = part_path(video_out) if two_pass_audio else part
    stage = on_stage or (lambda _s: None)

    kbps = None
    passes: list[int | None] = [None]
    if export is not None and export.rate_control != "quality" and plan.fmt != "gif":
        kbps = export.video_bitrate if export.rate_control == "bitrate" else target_kbps(
            export, plan.out_duration, has_audio)
        if kbps < MIN_VIDEO_KBPS:
            raise ToolError(f"{export.target_mb:g} MB is too small for {plan.out_duration:.1f} s of video "
                            f"(it leaves only {max(kbps, 0)} kb/s for the picture). Choose a larger size, "
                            "a shorter trim or a lower resolution.")
        if export.rate_control == "size" and plan.video_codec in TWO_PASS_ENCODERS:
            passes = [1, 2]

    def tick() -> None:
        n = read_progress_frame(prog)
        if n is not None and on_frames:
            on_frames(n)

    cwd = work_dir  # stabilization and 2-pass logs use short relative names inside the work folder
    try:
        if plan.stabilize:
            stage("Analysing motion")
            run_cancellable(stabilize_analysis_args(ffmpeg, src, plan), cancel=cancel, cwd=cwd)
        for pass_no in passes:
            if pass_no == 1:
                stage("Encoding (pass 1 of 2)")
            elif pass_no == 2:
                stage("Encoding (pass 2 of 2)")
            prog.unlink(missing_ok=True)
            cmd = transcode_args(ffmpeg, src, video_part, prog, plan, encoder=encoder, quality=quality,
                                 has_audio=has_audio and not two_pass_audio, max_seconds=max_seconds,
                                 export=export, kbps=kbps, pass_no=pass_no)
            run_cancellable(cmd, cancel=cancel, on_tick=tick, cwd=cwd, progress=lambda: progress_snapshot(prog))
        if two_pass_audio:
            os.replace(video_part, video_out)
            stage("Adding audio")
            mux_prog = work_dir / "mux-progress.txt"
            mux_prog.unlink(missing_ok=True)
            run_cancellable(mux_args(ffmpeg, ["-i", video_out], src, part, plan, with_audio=True,
                                     max_seconds=max_seconds, progress_file=mux_prog, export=export),
                            cancel=cancel, progress=lambda: progress_snapshot(mux_prog))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


# ------------------------------------------------------------------------- chunked AI path

def extract_args(ffmpeg: Path, src, dest: Path, plan: RenderPlan, chunk_start: int, count: int) -> Cmd:
    """Decode window frames [chunk_start, chunk_start+count) through the pre-AI filters to PNGs."""
    frame = plan.window.start_frame + chunk_start
    cmd: Cmd = [ffmpeg, "-v", "error", "-y"]
    if frame > 0:
        cmd += ["-ss", f"{float((frame - 0.5) / plan.window.fps):.6f}"]
    cmd += [*input_args(src), "-map", "0:v:0", "-an", "-sn", "-dn"]
    cmd += _vf(plan.video_filters(before_ai=True, timestamp_reset=False))
    cmd += ["-frames:v", str(count), "-fps_mode", "passthrough", "-start_number", "1", dest / "%08d.png"]
    return cmd


def extract_frames(ffmpeg: Path, src, dest: Path, plan: RenderPlan, chunk_start: int, count: int,
                   cancel: threading.Event | None = None) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    run_cancellable(extract_args(ffmpeg, src, dest, plan, chunk_start, count), cancel=cancel)
    return sum(1 for _ in dest.glob("*.png"))


def encode_chunk_args(ffmpeg: Path, frames_dir: Path, part: Path, plan: RenderPlan, chunk_start: int,
                      count: int, *, encoder: str, quality: int) -> Cmd:
    """Encode upscaled PNGs through the post-AI filters. Re-timing uses the chunk's global
    frame offset so chunk boundaries line up exactly with the single-pass result."""
    cmd: Cmd = [ffmpeg, "-v", "error", "-y", "-framerate", plan.fps_str, "-i", frames_dir / "%08d.png"]
    vf = plan.video_filters(before_ai=False)
    if plan.rate != 1:
        vf += plan.timing_filters(chunk_start) + ["setpts=PTS-STARTPTS", HOLD_LAST_FRAME]
    cmd += _vf(vf)
    if plan.rate != 1:
        cmd += ["-frames:v", str(plan.chunk_out_frames(chunk_start, count))]
    cmd += encoder_args(encoder, quality) + ["-pix_fmt", "yuv420p", part]
    return cmd


def encode_chunk(ffmpeg: Path, frames_dir: Path, out: Path, plan: RenderPlan, chunk_start: int, count: int,
                 *, encoder: str, quality: int, cancel: threading.Event | None = None) -> None:
    part = part_path(out)
    try:
        run_cancellable(encode_chunk_args(ffmpeg, frames_dir, part, plan, chunk_start, count,
                                          encoder=encoder, quality=quality), cancel=cancel)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


def concat_list_text(segments: Sequence[Path]) -> str:
    """ffmpeg concat-demuxer list. Single quotes in paths are escaped as '\\'' per the demuxer's rules."""
    return "".join("file '{}'\n".format(p.resolve().as_posix().replace("'", "'\\''")) for p in segments)


def mux_args(ffmpeg: Path, video_input: Sequence, src, part: Path, plan, *,
             with_audio: bool, max_seconds: float | None, progress_file: Path | None = None,
             export: Export | None = None) -> Cmd:
    """Copy finished video and add the source's (trimmed, re-timed, adjusted) audio."""
    cmd: Cmd = [ffmpeg, "-v", "error", "-y"]
    if progress_file is not None:
        cmd += ["-nostats", "-progress", progress_file]
    cmd += [*video_input]
    fmt = getattr(plan, "fmt", "mp4")
    if with_audio:
        if plan.trimmed:
            cmd += [*audio_window_args(plan)]
        cmd += [*input_args(src), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
        cmd += _af(plan.audio_filters) + [*audio_codec_args(fmt, export), "-shortest"]
        cmd += _output_limit(plan, max_seconds)
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    cmd += [*container_args(fmt), part]
    return cmd


def join_args(ffmpeg: Path, list_file: Path, src, part: Path, plan: RenderPlan, *,
              with_audio: bool, max_seconds: float | None, progress_file: Path | None = None,
              export: Export | None = None) -> Cmd:
    return mux_args(ffmpeg, ["-f", "concat", "-safe", "0", "-i", list_file], src, part, plan,
                    with_audio=with_audio, max_seconds=max_seconds, progress_file=progress_file, export=export)


def join_segments(ffmpeg: Path, segments: list[Path], src, out: Path, plan: RenderPlan, *,
                  with_audio: bool, max_seconds: float | None, work_dir: Path,
                  cancel: threading.Event | None = None, export: Export | None = None) -> None:
    """Concatenate encoded chunks losslessly and attach the (re-timed) source audio."""
    if not segments:
        raise ToolError("No video segments to join")
    list_file = work_dir / "segments.txt"
    list_file.write_text(concat_list_text(segments), encoding="utf-8")
    part = part_path(out)
    # Phase 1 joins (no audio filters) keep their exact command; filtered audio gets the stall watchdog
    prog = work_dir / "join-progress.txt" if plan.audio_filters else None
    if prog is not None:
        prog.unlink(missing_ok=True)
    try:
        run_cancellable(join_args(ffmpeg, list_file, src, part, plan, with_audio=with_audio and not plan.mute,
                                  max_seconds=max_seconds, progress_file=prog, export=export), cancel=cancel,
                        progress=(lambda: progress_snapshot(prog)) if prog else None)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


def final_encode_args(ffmpeg: Path, master: Path, part: Path, progress: Path, plan: RenderPlan, export: Export,
                      *, kbps: int | None = None, pass_no: int | None = None) -> Cmd:
    """AI path, last step for non-MP4/quality exports: re-encode the joined master once (video only)."""
    cmd: Cmd = [ffmpeg, "-v", "error", "-y", "-nostats", "-progress", progress, "-i", master, "-map", "0:v:0"]
    cmd += _vf(plan.final_filters())
    if plan.out_fps is not None:
        cmd += ["-frames:v", str(plan.out_frames)]
    cmd += video_codec_args(plan, export.video_codec, export.quality, export, kbps=kbps, pass_no=pass_no)
    if pass_no == 1:
        return cmd + ["-an", "-f", "null", "-"]
    return cmd + ["-an", *container_args(plan.fmt), part]


# ------------------------------------------------------------------------- still images

def image_codec_args(export: Export) -> list[str]:
    q = export.image_quality
    if export.image_format == "jpeg":
        return ["-c:v", "mjpeg", "-q:v", str(round(2 + (100 - q) / 100 * 29)), "-pix_fmt", "yuvj444p"]
    if export.image_format == "webp":
        return ["-c:v", "libwebp", "-quality", str(q)] + (["-lossless", "1"] if q >= 100 else [])
    if export.image_format == "bmp":
        return ["-c:v", "bmp"]
    return ["-c:v", "png"]


def image_args(ffmpeg: Path, src, part: Path, filters: Sequence[str], export: Export) -> Cmd:
    return [ffmpeg, "-v", "error", "-y", *input_args(src), "-map", "0:v:0", *_vf(filters),
            "-frames:v", "1", "-update", "1", *image_codec_args(export), part]


# ------------------------------------------------------------------------- audio only

AUDIO_FORMAT_ARGS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
}


def audio_export_args(ffmpeg: Path, src, part: Path, progress: Path, plan, audio_format: str) -> Cmd:
    cmd: Cmd = [ffmpeg, "-v", "error", "-y", "-nostats", "-progress", progress]
    if plan.trimmed:
        cmd += audio_window_args(plan)
    cmd += [*input_args(src), "-map", "0:a:0", "-vn", "-sn", *_af(plan.audio_filters)]
    return cmd + AUDIO_FORMAT_ARGS[audio_format] + [part]
