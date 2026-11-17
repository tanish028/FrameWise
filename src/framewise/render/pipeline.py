"""Execute a RenderSpec: either one ffmpeg pass, or a chunked, resumable AI upscale.

Both paths take every filter from the same RenderPlan (render/graph.py).
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.errors import Cancelled, ToolError
from ..core.logging_setup import get_logger
from ..media.probe import MediaInfo, probe
from ..tools.capabilities import Capabilities
from ..tools.registry import ToolSet
from . import ffmpeg_ops, upscaler
from .graph import RenderPlan, build_plan
from .spec import RenderSpec

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


def plan_for(spec: RenderSpec, info: MediaInfo, tools: ToolSet) -> RenderPlan:
    """Build the plan and make sure this ffmpeg has every filter it needs."""
    plan = build_plan(spec.edit, info, max_seconds=spec.max_seconds)
    if not plan.requires:
        return plan
    caps = Capabilities.detect(tools.ffmpeg)  # cached per ffmpeg build
    if plan.rate != 1 and not caps.has("rubberband"):
        log.info("ffmpeg has no rubberband filter; using atempo for the audio speed change")
        plan = build_plan(spec.edit, info, max_seconds=spec.max_seconds, stretcher="atempo")
    caps.require(plan.requires)
    return plan


def run_render(spec: RenderSpec, tools: ToolSet, cancel: threading.Event | None = None,
               on_progress: ProgressFn | None = None) -> Path:
    cancel = cancel or threading.Event()
    emit = on_progress or (lambda _p: None)
    _validate(spec)
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    emit(Progress("Preparing", 0, 1))
    info = probe(tools.ffprobe, spec.input)
    plan = plan_for(spec, info, tools)
    work = work_dir_for(spec)
    if plan.upscale is not None:
        _run_upscale(spec, tools, info, plan, work, cancel, emit)
        total = plan.window.frames
    else:
        total = plan.out_frames
        emit(Progress("Encoding", 0, total))
        ffmpeg_ops.transcode(tools.ffmpeg, spec.input, spec.output, plan, encoder=spec.encoder,
                             quality=spec.quality, has_audio=info.has_audio, max_seconds=spec.max_seconds,
                             work_dir=work, cancel=cancel,
                             on_frames=lambda n: emit(Progress("Encoding", min(n, total), total)))
        shutil.rmtree(work, ignore_errors=True)
    emit(Progress("Done", total, total, str(spec.output)))
    log.info("Wrote %s", spec.output)
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
        got = ffmpeg_ops.extract_frames(tools.ffmpeg, spec.input, in_dir, plan, start, count, cancel)
        _check(cancel)  # fast steps can finish between the runner's cancel checks
        if got == 0:
            raise ToolError("ffmpeg could not read frames from this part of the video; the file may be damaged")
        base = done
        upscaler.upscale_dir(exe, in_dir, out_dir, model=plan.model, scale=sp.engine_scale,
                             expected=got, tile=spec.tile, cancel=cancel,
                             on_frames=lambda n, b=base, lb=label: emit(Progress("Upscaling", b + n, total, lb)))
        _check(cancel)
        emit(Progress("Encoding", done + got, total, label))
        ffmpeg_ops.encode_chunk(tools.ffmpeg, out_dir, seg, plan, start, got,
                                encoder=spec.encoder, quality=spec.quality, cancel=cancel)
        for d in (in_dir, out_dir):
            shutil.rmtree(d, ignore_errors=True)
        done += got
        log.info("%s done (%.2f fps overall)", label, done / max(1e-6, time.monotonic() - started))

    _check(cancel)
    emit(Progress("Finishing", total, total, "Joining chunks and adding audio"))
    ffmpeg_ops.join_segments(tools.ffmpeg, segments, spec.input, spec.output, plan, with_audio=info.has_audio,
                             max_seconds=spec.max_seconds, work_dir=work, cancel=cancel)
    shutil.rmtree(work, ignore_errors=True)
