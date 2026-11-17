"""Measurements that suggest edits (never apply them): black-bar detection."""
from __future__ import annotations

import re
import threading
from collections import Counter
from pathlib import Path

from ..core.proc import run_cancellable
from ..media.probe import MediaInfo
from ..media.source import Source, input_args
from ..project.edit import CropRect, Transform
from .graph import orientation_filters, oriented_size

_CROP = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def parse_cropdetect(output: str) -> list[CropRect]:
    """cropdetect prints crop=w:h:x:y; return them as rectangles."""
    return [CropRect(int(x), int(y), int(w), int(h)) for w, h, x, y in _CROP.findall(output)]


def detect_black_bars(ffmpeg: Path, source: Source, info: MediaInfo, transform: Transform,
                      samples: int = 5, cancel: threading.Event | None = None) -> CropRect | None:
    """Suggest a crop that removes black borders, in the rotated frame's coordinates.

    Several moments are sampled; the suggestion is the union of what each sample considers picture,
    so a dark scene can never make it cut into the image. Returns None when there are no bars."""
    ow, oh = oriented_size(info.width, info.height, transform.rotate)
    orient = orientation_filters(transform.rotate, transform.flip_h, transform.flip_v)
    vf = ",".join(orient + ["cropdetect=limit=0.094:round=2:reset=1"])
    times = [0.0] if info.is_still else [info.duration * (i + 1) / (samples + 1) for i in range(samples)]
    found: list[CropRect] = []
    for t in times:
        cmd = [ffmpeg, "-hide_banner", "-v", "info", "-y"]
        if t > 0:
            cmd += ["-ss", f"{t:.3f}"]
        cmd += [*input_args(source), "-map", "0:v:0", "-vf", vf, "-frames:v", "12", "-f", "null", "-"]
        out = run_cancellable(cmd, cancel=cancel)
        rects = parse_cropdetect(out)
        if rects:
            found.append(Counter(rects).most_common(1)[0][0])
    if not found:
        return None
    x0 = min(r.x for r in found)
    y0 = min(r.y for r in found)
    x1 = max(r.x + r.w for r in found)
    y1 = max(r.y + r.h for r in found)
    rect = CropRect(x0, y0, x1 - x0, y1 - y0).validated().fitted(ow, oh)
    if rect.w >= ow - 2 and rect.h >= oh - 2:
        return None
    return rect
