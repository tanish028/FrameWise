"""Turn a target such as "2x" or "1080p" into an engine scale and an exact output size."""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_CHOICES = ("2x", "3x", "4x", "1080p", "1440p", "2160p")
_HEIGHTS = {"720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "4k": 2160}
_FACTOR_RE = re.compile(r"^(\d+(?:\.\d+)?)x$")


@dataclass(frozen=True)
class ScalePlan:
    engine_scale: int   # value for the engine's -s
    out_width: int
    out_height: int
    needs_resize: bool  # engine output must be resized to out_width x out_height


def _even(n: float) -> int:
    v = max(2, int(round(n)))
    return v + (v % 2)


def plan_scaling(width: int, height: int, target: str, native_scales: tuple[int, ...]) -> ScalePlan:
    if width <= 0 or height <= 0:
        raise ValueError("Video dimensions must be positive")
    spec = target.strip().lower()
    m = _FACTOR_RE.match(spec)
    if m:
        factor = float(m.group(1))
        out_w, out_h = _even(width * factor), _even(height * factor)
    elif spec in _HEIGHTS:
        out_h = _even(_HEIGHTS[spec])
        out_w = _even(width * out_h / height)
        factor = out_h / height
    else:
        raise ValueError(f"Unknown target {target!r}. Use e.g. 2x, 4x, 1080p, 2160p")
    if factor <= 1.0:
        raise ValueError(f"{target} is not larger than the source ({width}×{height}); nothing to upscale")
    bigger = [s for s in sorted(native_scales) if s >= factor]
    engine_scale = bigger[0] if bigger else max(native_scales)
    needs_resize = (width * engine_scale, height * engine_scale) != (out_w, out_h)
    return ScalePlan(engine_scale, out_w, out_h, needs_resize)
