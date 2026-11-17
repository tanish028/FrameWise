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
from ..tools.encoders import encoder_args
from .graph import RenderPlan

AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k"]
HOLD_LAST_FRAME = "tpad=stop_mode=clone:stop=-1"  # lets fps fill the final slots of a re-timed clip

Cmd = list  # list of str | Path


def part_path(path: Path) -> Path:
    return path.with_name(path.stem + ".part" + path.suffix)


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


def audio_window_args(plan: RenderPlan) -> list[str]:
    """Input options for the audio covering exactly the same time span as the video window."""
    w = plan.window
    args = ["-ss", f"{w.start:.6f}"] if w.start_frame > 0 else []
    return args + ["-t", f"{w.duration:.6f}"]


def _output_limit(plan: RenderPlan, max_seconds: float | None) -> list[str]:
    """Phase 1 previews cut the *output* with -t; keep that exactly at normal speed."""
    if not max_seconds or plan.trimmed:
        return []
    if plan.rate == 1:
        return ["-t", f"{max_seconds:.3f}"]
    return ["-t", f"{plan.out_duration:.6f}"]


def _vf(filters: Sequence[str]) -> list[str]:
    return ["-vf", ",".join(filters)] if filters else []


def _af(filters: Sequence[str]) -> list[str]:
    return ["-af", ",".join(filters)] if filters else []


# ------------------------------------------------------------------------- single pass (no AI)

def transcode_args(ffmpeg: Path, src: Path, part: Path, progress: Path, plan: RenderPlan, *,
                   encoder: str, quality: int, has_audio: bool, max_seconds: float | None) -> Cmd:
    cmd: Cmd = [ffmpeg, "-v", "error", "-y", "-nostats", "-progress", progress]
    if plan.trimmed:
        cmd += [*video_window_args(plan), "-i", src]
        if has_audio:
            cmd += [*audio_window_args(plan), "-i", src]
        a_map = "1:a:0"
    else:
        cmd += ["-i", src]
        a_map = "0:a:0"
    cmd += _output_limit(plan, max_seconds)
    cmd += ["-map", "0:v:0"] + (["-map", a_map] if has_audio else [])
    vf = plan.video_filters(before_ai=True) + plan.video_filters(before_ai=False)
    if plan.rate != 1:
        vf += plan.timing_filters(0) + [HOLD_LAST_FRAME]
    cmd += _vf(vf)
    if plan.rate != 1 or plan.trimmed:
        cmd += ["-frames:v", str(plan.out_frames)]
    cmd += encoder_args(encoder, quality) + ["-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += _af(plan.audio_filters) + AUDIO_ARGS
    cmd += ["-movflags", "+faststart", part]
    return cmd


def transcode(ffmpeg: Path, src: Path, out: Path, plan: RenderPlan, *, encoder: str, quality: int,
              has_audio: bool, max_seconds: float | None, work_dir: Path,
              cancel: threading.Event | None = None, on_frames: Callable[[int], None] | None = None) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    prog = work_dir / "progress.txt"
    prog.unlink(missing_ok=True)
    part = part_path(out)
    cmd = transcode_args(ffmpeg, src, part, prog, plan, encoder=encoder, quality=quality,
                         has_audio=has_audio, max_seconds=max_seconds)

    def tick() -> None:
        n = read_progress_frame(prog)
        if n is not None and on_frames:
            on_frames(n)

    try:
        run_cancellable(cmd, cancel=cancel, on_tick=tick)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


# ------------------------------------------------------------------------- chunked AI path

def extract_args(ffmpeg: Path, src: Path, dest: Path, plan: RenderPlan, chunk_start: int, count: int) -> Cmd:
    """Decode window frames [chunk_start, chunk_start+count) through the pre-AI filters to PNGs."""
    frame = plan.window.start_frame + chunk_start
    cmd: Cmd = [ffmpeg, "-v", "error", "-y"]
    if frame > 0:
        cmd += ["-ss", f"{float((frame - 0.5) / plan.window.fps):.6f}"]
    cmd += ["-i", src, "-map", "0:v:0", "-an", "-sn", "-dn"]
    cmd += _vf(plan.video_filters(before_ai=True, timestamp_reset=False))
    cmd += ["-frames:v", str(count), "-fps_mode", "passthrough", "-start_number", "1", dest / "%08d.png"]
    return cmd


def extract_frames(ffmpeg: Path, src: Path, dest: Path, plan: RenderPlan, chunk_start: int, count: int,
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


def join_args(ffmpeg: Path, list_file: Path, src: Path, part: Path, plan: RenderPlan, *,
              with_audio: bool, max_seconds: float | None) -> Cmd:
    cmd: Cmd = [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
    if with_audio:
        if plan.trimmed:
            cmd += [*audio_window_args(plan)]
        cmd += ["-i", src, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
        cmd += _af(plan.audio_filters) + [*AUDIO_ARGS, "-shortest"]
        cmd += _output_limit(plan, max_seconds)
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    cmd += ["-movflags", "+faststart", part]
    return cmd


def join_segments(ffmpeg: Path, segments: list[Path], src: Path, out: Path, plan: RenderPlan, *,
                  with_audio: bool, max_seconds: float | None, work_dir: Path,
                  cancel: threading.Event | None = None) -> None:
    """Concatenate encoded chunks losslessly and attach the (re-timed) source audio."""
    if not segments:
        raise ToolError("No video segments to join")
    list_file = work_dir / "segments.txt"
    list_file.write_text(concat_list_text(segments), encoding="utf-8")
    part = part_path(out)
    try:
        run_cancellable(join_args(ffmpeg, list_file, src, part, plan, with_audio=with_audio,
                                  max_seconds=max_seconds), cancel=cancel)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)
