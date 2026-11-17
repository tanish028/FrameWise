"""Preview engine: one still frame, processed with exactly the export's filters.

The frame is processed at full resolution with the same RenderPlan filters as the export, and only
then scaled down for display, so sharpening, blur radius, vignette and crop look the same as in the
exported file. Edits that a still frame cannot show (speed, stabilization) are reported in
`PreviewResult.omitted`, never silently substituted. AI upscaling is included only when asked
for (it takes seconds per frame).

Results are cached on disk, keyed by everything that affects the pixels, so scrubbing back to a
position or undoing an edit is instant. Qt-free; the UI runs it on a worker thread and drops
results that arrive for a request that is no longer current.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from ..core.errors import ToolError
from ..core.logging_setup import get_logger
from ..core.proc import run_cancellable
from ..media.probe import MediaInfo
from ..media.source import Source, input_args
from ..project.edit import Edit
from ..tools.capabilities import Capabilities
from ..tools.registry import ToolSet
from . import upscaler
from .graph import build_plan

log = get_logger("preview")
CACHE_LIMIT = 300
VISUAL_SECTIONS = ("transform", "color", "filter", "enhance", "export")


@dataclass(frozen=True)
class PreviewRequest:
    source: Source
    info: MediaInfo
    edit: Edit
    time: float = 0.0             # source seconds (ignored for still images)
    max_w: int = 1280
    max_h: int = 720
    edited: bool = True           # False = original frame
    include_ai: bool = False      # run the AI upscaler on this frame too
    without_crop: bool = False    # show the whole frame (the crop tool draws its rectangle on top)


@dataclass(frozen=True)
class PreviewResult:
    image: Path
    frame_size: tuple[int, int]   # size of the processed frame before display scaling
    omitted: tuple[str, ...] = ()
    ai_applied: bool = False


def _edit_for(req: PreviewRequest) -> Edit:
    e = req.edit
    if req.without_crop:
        e = e.with_section("transform", crop=None)
    if not req.include_ai:
        e = e.with_section("enhance", upscale=False)
    return e


def cache_key(req: PreviewRequest) -> str:
    src = req.source
    try:
        st = os.stat(src.path)
        file_id = [str(Path(src.path).resolve()), st.st_size, int(st.st_mtime)]
    except OSError:
        file_id = [str(src.path), 0, 0]
    seq = src.sequence.to_dict() if src.sequence else None
    edit = _edit_for(req).fingerprint(*VISUAL_SECTIONS) if req.edited else "original"
    t = 0 if req.info.is_still else round(req.time, 3)
    raw = json.dumps([file_id, seq, edit, t, req.max_w, req.max_h, req.edited, req.include_ai, req.without_crop])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _display_scale(max_w: int, max_h: int) -> str:
    # never enlarge: small sources stay pixel-exact; commas inside min() are protected by quotes
    return f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease:flags=bicubic"


def _seek(req: PreviewRequest) -> list:
    if req.info.is_still or req.time <= 0:
        return []
    return ["-ss", f"{max(0.0, min(req.time, req.info.duration - 0.05)):.3f}"]


def render_preview(req: PreviewRequest, tools: ToolSet, cache_dir: Path,
                   cancel: threading.Event | None = None) -> PreviewResult:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(req)
    image, meta_file = cache_dir / f"{key}.png", cache_dir / f"{key}.json"
    if image.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            os.utime(image)  # LRU
            return PreviewResult(image, tuple(meta["size"]), tuple(meta["omitted"]), bool(meta["ai"]))
        except (OSError, ValueError, KeyError):
            pass
    if not req.info.has_video:
        raise ToolError("This media has no picture to preview")

    ff = tools.ffmpeg
    tmp = cache_dir / f"{key}.part.png"
    omitted: tuple[str, ...] = ()
    ai = False
    if not req.edited:
        size = (req.info.width, req.info.height)
        cmd = [ff, "-v", "error", "-y", *_seek(req), *input_args(req.source), "-map", "0:v:0",
               "-vf", _display_scale(req.max_w, req.max_h), "-frames:v", "1", "-update", "1", tmp]
        run_cancellable(cmd, cancel=cancel)
    else:
        caps = Capabilities.detect(ff)
        plan = build_plan(_edit_for(req), req.info, still=True, available=caps.filters)
        if plan.requires:
            caps.require(plan.requires, "This preview")
        omitted = plan.omitted
        size = plan.out_size
        pre = plan.video_filters(before_ai=True, timestamp_reset=False)
        post = plan.video_filters(before_ai=False) + [_display_scale(req.max_w, req.max_h)]
        if plan.upscale is None:
            cmd = [ff, "-v", "error", "-y", *_seek(req), *input_args(req.source), "-map", "0:v:0",
                   "-vf", ",".join(pre + post), "-frames:v", "1", "-update", "1", tmp]
            run_cancellable(cmd, cancel=cancel)
        else:
            exe = tools.require_upscaler()
            work = cache_dir / f"{key}.work"
            shutil.rmtree(work, ignore_errors=True)
            try:
                (work / "in").mkdir(parents=True)
                cmd = [ff, "-v", "error", "-y", *_seek(req), *input_args(req.source), "-map", "0:v:0",
                       *(["-vf", ",".join(pre)] if pre else []), "-frames:v", "1", work / "in" / "00000001.png"]
                run_cancellable(cmd, cancel=cancel)
                upscaler.upscale_dir(exe, work / "in", work / "out", model=plan.model,
                                     scale=plan.upscale.engine_scale, expected=1, cancel=cancel)
                run_cancellable([ff, "-v", "error", "-y", "-i", work / "out" / "00000001.png",
                                 "-vf", ",".join(post), "-frames:v", "1", "-update", "1", tmp], cancel=cancel)
                ai = True
            finally:
                shutil.rmtree(work, ignore_errors=True)
    if not tmp.exists():
        raise ToolError("ffmpeg produced no preview frame at this position")
    os.replace(tmp, image)
    meta_file.write_text(json.dumps({"size": list(size), "omitted": list(omitted), "ai": ai}), encoding="utf-8")
    prune_cache(cache_dir)
    return PreviewResult(image, size, omitted, ai)


def prune_cache(cache_dir: Path, limit: int = CACHE_LIMIT) -> None:
    images = sorted(cache_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in images[limit:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def original_request(req: PreviewRequest) -> PreviewRequest:
    return replace(req, edited=False, include_ai=False, without_crop=False)
