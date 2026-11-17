"""Execute a RenderSpec: either one ffmpeg pass, or a chunked, resumable AI upscale.

Both paths take every filter from the same RenderPlan (render/graph.py).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.errors import Cancelled, ToolError
from ..core.logging_setup import get_logger
from ..core.proc import run_cancellable
from ..media.probe import MediaInfo, probe_source
from ..tools.capabilities import Capabilities
from ..tools.registry import ToolSet
from . import ffmpeg_ops, upscaler
from .graph import RenderPlan, build_audio_plan, build_plan
from .spec import AUDIO_JOB, IMAGE_JOB, RenderSpec

log = get_logger("pipeline")

# Edit sections whose values change the pixels of an upscaled chunk (audio only affects the final join)
CHUNK_SECTIONS = ("trim", "transform", "color", "filter", "enhance", "speed")

# A final chunk this short (in output frames) is folded into the one before it. ffmpeg 8's concat
# demuxer mis-times a 1-2 frame chunk that follows normal ones (it has no B-frame delay), which
# made joined videos 2 frames too long. Verified on ffmpeg 8.0.1 for speeds 0.7x-4x.
MIN_TAIL_OUT_FRAMES = 8


def chunk_ranges(total: int, segment_frames: int, plan: RenderPlan) -> list[tuple[int, int]]:
    """(start, count) of each chunk, in window frames. Deterministic, so resume finds the same chunks."""
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total:
        count = min(segment_frames, total - start)
        rest = total - (start + count)
        if 0 < rest and plan.chunk_out_frames(start + count, rest) < MIN_TAIL_OUT_FRAMES:
            count += rest
        ranges.append((start, count))
        start += count
    return ranges


@dataclass(frozen=True)
class Progress:
    stage: str      # "Preparing", "Upscaling", "Encoding", "Finishing", "Done", ...
    done: int       # frames
    total: int      # frames
    detail: str = ""

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0


ProgressFn = Callable[[Progress], None]


def work_dir_for(spec: RenderSpec) -> Path:
    return spec.output.parent / f".{spec.output.stem}.framewise-work"


def estimate_chunk_bytes(w: int, h: int, scale: int, frames: int) -> int:
    """Rough upper bound for one chunk's PNG frames (input + upscaled)."""
    return int((w * h) * (1 + scale * scale) * 3 * frames * 0.8)


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled()


def _validate(spec: RenderSpec) -> None:
    if not spec.input.is_file():
        raise ToolError(f"Source file not found: {spec.input}")
    if spec.output.resolve() == spec.input.resolve():
        raise ToolError("The output file must be different from the source file")
    if spec.segment_frames < 1:
        raise ValueError("segment_frames must be at least 1")


def _capabilities(tools: ToolSet) -> Capabilities:
    return Capabilities.detect(tools.ffmpeg)  # cached per ffmpeg build


def plan_for(spec: RenderSpec, info: MediaInfo, tools: ToolSet) -> RenderPlan:
    """Build the plan and make sure this ffmpeg has every filter it needs."""
    plan = build_plan(spec.edit, info, max_seconds=spec.max_seconds)
    if not plan.requires:
        return plan
    caps = _capabilities(tools)
    stretcher = "rubberband" if caps.has("rubberband") else "atempo"
    if plan.rate != 1 and stretcher == "atempo":
        log.info("ffmpeg has no rubberband filter; using atempo for the audio speed change")
    plan = build_plan(spec.edit, info, max_seconds=spec.max_seconds, stretcher=stretcher, available=caps.filters)
    caps.require(plan.requires)
    return plan


# Memory ffmpeg's reverse filter may use: it holds every frame of the clip until the end
REVERSE_MEMORY_LIMIT = 3 * 1024 ** 3


def _check_supported(spec: RenderSpec, plan: RenderPlan) -> None:
    """Combinations Phase 2 cannot render correctly are refused with a plain explanation."""
    if plan.upscale is not None and plan.stabilize:
        raise ToolError("Stabilization can't be combined with AI upscaling yet. Stabilize and export first "
                        "(without AI upscale), then import that file and upscale it.")
    if plan.upscale is not None and plan.reverse:
        raise ToolError("Reverse can't be combined with AI upscaling yet. Reverse and export first, "
                        "then import that file and upscale it.")
    if plan.reverse:
        w, h = plan.out_size
        need = plan.window.frames * w * h * 1.5
        if need > REVERSE_MEMORY_LIMIT:
            secs = REVERSE_MEMORY_LIMIT / (w * h * 1.5) / float(plan.window.fps)
            raise ToolError(f"Reversing this clip would need about {need / 1024 ** 3:.1f} GB of memory. "
                            f"Trim it to about {secs:.0f} seconds or less, or lower the export resolution.")


def run_render(spec: RenderSpec, tools: ToolSet, cancel: threading.Event | None = None,
               on_progress: ProgressFn | None = None) -> Path:
    """Render one job: a video, a still image or an audio file."""
    cancel = cancel or threading.Event()
    emit = on_progress or (lambda _p: None)
    _validate(spec)
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    emit(Progress("Preparing", 0, 1))
    info = probe_source(tools.ffprobe, spec.input, spec.sequence)
    if spec.kind == IMAGE_JOB:
        return _run_image(spec, tools, info, cancel, emit)
    if spec.kind == AUDIO_JOB:
        return _run_audio(spec, tools, info, cancel, emit)
    plan = plan_for(spec, info, tools)
    _check_supported(spec, plan)
    work = work_dir_for(spec)
    if plan.upscale is not None:
        _run_upscale(spec, tools, info, plan, work, cancel, emit)
        total = plan.window.frames
    else:
        total = plan.out_frames
        stage = ["Encoding"]
        emit(Progress("Encoding", 0, total))

        def on_stage(name: str) -> None:
            stage[0] = name
            emit(Progress(name, 0, total))

        ffmpeg_ops.transcode(tools.ffmpeg, spec.source, spec.output, plan, encoder=spec.encoder,
                             quality=spec.quality, has_audio=info.has_audio, max_seconds=spec.max_seconds,
                             work_dir=work, cancel=cancel, export=spec.edit.export, on_stage=on_stage,
                             on_frames=lambda n: emit(Progress(stage[0], min(n, total), total)))
        shutil.rmtree(work, ignore_errors=True)
    emit(Progress("Done", total, total, str(spec.output)))
    log.info("Wrote %s", spec.output)
    return spec.output


def _run_image(spec: RenderSpec, tools: ToolSet, info: MediaInfo, cancel: threading.Event,
               emit: ProgressFn) -> Path:
    """Still image: one ffmpeg pass, or pre-filters → AI upscaler → post-filters."""
    if not info.has_video:
        raise ToolError("This file has no picture to export")
    caps = _capabilities(tools)
    plan = build_plan(spec.edit, info, still=True, available=caps.filters)
    if plan.requires:
        caps.require(plan.requires)
    part = ffmpeg_ops.part_path(spec.output)
    work = work_dir_for(spec)
    try:
        if plan.upscale is None:
            emit(Progress("Processing", 0, 1))
            run_cancellable(ffmpeg_ops.image_args(tools.ffmpeg, spec.source, part,
                                                  plan.video_filters(before_ai=True) + plan.video_filters(before_ai=False),
                                                  spec.edit.export), cancel=cancel)
        else:
            exe = tools.require_upscaler()
            in_dir, out_dir = work / "in", work / "out"
            for d in (in_dir, out_dir):
                shutil.rmtree(d, ignore_errors=True)
            emit(Progress("Preparing", 0, 1))
            ffmpeg_ops.extract_frames(tools.ffmpeg, spec.source, in_dir, plan, 0, 1, cancel)
            _check(cancel)
            emit(Progress("Upscaling", 0, 1, "AI upscale"))
            upscaler.upscale_dir(exe, in_dir, out_dir, model=plan.model, scale=plan.upscale.engine_scale,
                                 expected=1, tile=spec.tile, cancel=cancel)
            _check(cancel)
            emit(Progress("Finishing", 1, 1))
            run_cancellable(ffmpeg_ops.image_args(tools.ffmpeg, out_dir / "00000001.png", part,
                                                  plan.video_filters(before_ai=False), spec.edit.export), cancel=cancel)
        os.replace(part, spec.output)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    emit(Progress("Done", 1, 1, str(spec.output)))
    log.info("Wrote %s", spec.output)
    return spec.output


def _run_audio(spec: RenderSpec, tools: ToolSet, info: MediaInfo, cancel: threading.Event,
               emit: ProgressFn) -> Path:
    """Audio file, or the soundtrack of a video ("extract audio")."""
    if not info.has_audio:
        raise ToolError("This file has no audio to export")
    caps = _capabilities(tools)
    stretcher = "rubberband" if caps.has("rubberband") else "atempo"
    plan = build_audio_plan(spec.edit, info, max_seconds=spec.max_seconds, stretcher=stretcher)
    if plan.requires:
        caps.require(plan.requires)
    work = work_dir_for(spec)
    work.mkdir(parents=True, exist_ok=True)
    prog = work / "progress.txt"
    part = ffmpeg_ops.part_path(spec.output)
    total_ms = max(1, int(plan.duration * 1000))

    def tick() -> None:
        snap = ffmpeg_ops.progress_snapshot(prog)
        if snap and snap[2] and str(snap[2]).isdigit():
            emit(Progress("Encoding audio", min(total_ms, int(snap[2]) // 1000), total_ms))

    try:
        run_cancellable(ffmpeg_ops.audio_export_args(tools.ffmpeg, spec.source, part, prog, plan,
                                                     spec.edit.export.audio_format),
                        cancel=cancel, on_tick=tick, progress=lambda: ffmpeg_ops.progress_snapshot(prog))
        os.replace(part, spec.output)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    emit(Progress("Done", total_ms, total_ms, str(spec.output)))
    return spec.output


def _signature(spec: RenderSpec, plan: RenderPlan) -> dict:
    """Everything that affects already-finished chunks. Any change discards them."""
    st = spec.input.stat()
    return {"input": str(spec.input.resolve()), "size": st.st_size, "mtime": int(st.st_mtime),
            "preset": spec.preset, "target": spec.target, "encoder": spec.encoder,
            "quality": spec.quality, "segment_frames": spec.segment_frames,
            "total": plan.window.frames, "window_start": plan.window.start_frame,
            "edit": spec.edit.fingerprint(*CHUNK_SECTIONS)}


def _prepare_work_dir(work: Path, sig: dict) -> None:
    """Reuse finished chunks only when every setting that affects them is unchanged."""
    sig_file = work / "job.json"
    if sig_file.exists():
        try:
            same = json.loads(sig_file.read_text(encoding="utf-8")) == sig
        except (OSError, json.JSONDecodeError):
            same = False
        if not same:
            log.info("Settings changed since the last attempt; discarding old chunks")
            shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    sig_file.write_text(json.dumps(sig), encoding="utf-8")


def _run_upscale(spec: RenderSpec, tools: ToolSet, info: MediaInfo, plan: RenderPlan, work: Path,
                 cancel: threading.Event, emit: ProgressFn) -> None:
    exe = tools.require_upscaler()
    sp = plan.upscale
    total = plan.window.frames
    bw, bh = plan.base_size
    log.info("%s: %s @ %.3f fps, frames %d..%d -> %dx%d (model %s x%d, speed %s)", spec.input.name,
             info.resolution, info.fps, plan.window.start_frame, plan.window.start_frame + total,
             sp.out_width, sp.out_height, plan.model, sp.engine_scale, plan.rate)

    work.mkdir(parents=True, exist_ok=True)
    chunk = min(spec.segment_frames + MIN_TAIL_OUT_FRAMES * 4, total)  # a folded tail makes the last chunk larger
    needed = estimate_chunk_bytes(bw, bh, sp.engine_scale, chunk)
    free = shutil.disk_usage(work).free
    if free < needed:
        raise ToolError(f"Not enough free disk space: about {needed / 1e9:.1f} GB needed, "
                        f"{free / 1e9:.1f} GB free on {work.anchor or work}.")
    _prepare_work_dir(work, _signature(spec, plan))

    # Plain MP4 exports encode chunks directly (Phase 1). Other formats/rate controls encode a
    # high-quality H.264 master first and convert it once at the end.
    final = plan.needs_final_encode or spec.edit.export.rate_control != "quality"
    chunk_encoder, chunk_quality = ("libx264", 12) if final else (spec.encoder, spec.quality)
    ranges = chunk_ranges(total, spec.segment_frames, plan)
    n_chunks = len(ranges)
    segments: list[Path] = []
    done = 0
    started = time.monotonic()
    for i, (start, count) in enumerate(ranges):
        if cancel.is_set():
            raise Cancelled()
        seg = work / f"seg_{i:05d}.mp4"
        segments.append(seg)
        label = f"Chunk {i + 1}/{n_chunks}"
        if seg.exists():
            done += count
            emit(Progress("Resuming", done, total, f"{label} already done"))
            continue
        in_dir, out_dir = work / "in", work / "out"
        for d in (in_dir, out_dir):
            shutil.rmtree(d, ignore_errors=True)
        emit(Progress("Extracting", done, total, label))
        got = ffmpeg_ops.extract_frames(tools.ffmpeg, spec.source, in_dir, plan, start, count, cancel)
        _check(cancel)  # fast steps can finish between the runner's cancel checks
        if got == 0:
            raise ToolError("ffmpeg could not read frames from this part of the video; the file may be damaged")
        base = done
        upscaler.upscale_dir(exe, in_dir, out_dir, model=plan.model, scale=sp.engine_scale,
                             expected=got, tile=spec.tile, cancel=cancel,
                             on_frames=lambda n, b=base, lb=label: emit(Progress("Upscaling", b + n, total, lb)))
        _check(cancel)
        emit(Progress("Encoding", done + got, total, label))
        ffmpeg_ops.encode_chunk(tools.ffmpeg, out_dir, seg, plan, start, got, encoder=chunk_encoder,
                                quality=chunk_quality, cancel=cancel)
        for d in (in_dir, out_dir):
            shutil.rmtree(d, ignore_errors=True)
        done += got
        log.info("%s done (%.2f fps overall)", label, done / max(1e-6, time.monotonic() - started))

    _check(cancel)
    if not final:
        emit(Progress("Finishing", total, total, "Joining chunks and adding audio"))
        ffmpeg_ops.join_segments(tools.ffmpeg, segments, spec.source, spec.output, plan, with_audio=info.has_audio,
                                 max_seconds=spec.max_seconds, work_dir=work, cancel=cancel,
                                 export=spec.edit.export)
    else:
        _final_encode(spec, tools, info, plan, segments, work, cancel, emit)
    shutil.rmtree(work, ignore_errors=True)


def _final_encode(spec: RenderSpec, tools: ToolSet, info: MediaInfo, plan: RenderPlan, segments: list[Path],
                  work: Path, cancel: threading.Event, emit: ProgressFn) -> None:
    """AI path for WebM/GIF/bitrate/target-size/frame-rate exports: join → convert → add audio."""
    ex = spec.edit.export
    total = plan.out_frames
    master = work / "master.mp4"
    emit(Progress("Finishing", 0, total, "Joining chunks"))
    ffmpeg_ops.join_segments(tools.ffmpeg, segments, spec.source, master, plan, with_audio=False,
                             max_seconds=None, work_dir=work, cancel=cancel)
    has_audio = info.has_audio and not plan.mute
    video = work / f"video{spec.output.suffix}" if has_audio else spec.output
    vpart = ffmpeg_ops.part_path(video)
    prog = work / "final-progress.txt"
    kbps, passes = None, [None]
    if ex.rate_control != "quality" and plan.fmt != "gif":
        kbps = ex.video_bitrate if ex.rate_control == "bitrate" else ffmpeg_ops.target_kbps(
            ex, plan.out_duration, has_audio)
        if kbps < ffmpeg_ops.MIN_VIDEO_KBPS:
            raise ToolError(f"{ex.target_mb:g} MB is too small for {plan.out_duration:.1f} s of video. "
                            "Choose a larger size, a shorter trim or a lower resolution.")
        if ex.rate_control == "size" and plan.video_codec in ffmpeg_ops.TWO_PASS_ENCODERS:
            passes = [1, 2]
    try:
        for pass_no in passes:
            label = "Converting" + (f" (pass {pass_no} of 2)" if pass_no else "")
            prog.unlink(missing_ok=True)
            run_cancellable(ffmpeg_ops.final_encode_args(tools.ffmpeg, master, vpart, prog, plan, ex,
                                                         kbps=kbps, pass_no=pass_no),
                            cancel=cancel, cwd=work, progress=lambda: ffmpeg_ops.progress_snapshot(prog),
                            on_tick=lambda: emit(Progress(label, min(total, ffmpeg_ops.read_progress_frame(prog) or 0),
                                                          total)))
        os.replace(vpart, video)
        if has_audio:
            emit(Progress("Finishing", total, total, "Adding audio"))
            part = ffmpeg_ops.part_path(spec.output)
            mux_prog = work / "mux-progress.txt"
            run_cancellable(ffmpeg_ops.mux_args(tools.ffmpeg, ["-i", video], spec.source, part, plan, with_audio=True,
                                                max_seconds=spec.max_seconds, progress_file=mux_prog, export=ex),
                            cancel=cancel, progress=lambda: ffmpeg_ops.progress_snapshot(mux_prog))
            os.replace(part, spec.output)
    except BaseException:
        vpart.unlink(missing_ok=True)
        ffmpeg_ops.part_path(spec.output).unlink(missing_ok=True)
        raise
