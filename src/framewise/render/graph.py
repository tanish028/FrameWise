"""Turn an Edit + source MediaInfo into a RenderPlan: the exact filters and frame windows to run.

This module is pure (no ffmpeg calls, no Qt). Both render paths, the single-pass export and the
chunked AI upscale, and later the interactive preview, take their filters from the same plan,
so they cannot disagree.

Video filter order (lower stage first). The AI upscaler runs between PRE and POST stages:

    TRIM_RESET → ORIENT (rotate/flip) → CROP ── AI upscale ── RESIZE → TIME (speed) → FPS

Later steps insert their stages (deinterlace, denoise, stabilize before the AI boundary;
canvas, colour, look, sharpen, finish after it) without changing this contract.

Audio speed changes use rubberband when this ffmpeg has it (attacks stay within ~±25 ms of where
they belong at every speed), else chained atempo (up to ~0.1 s early at 0.25×). The audio window is
padded with silence *before* stretching and cut to the video's exact duration after it, so audio can
never shorten the video. (Padding *after* atempo deadlocks ffmpeg 8 at fast speeds; and audio is
always filtered in its own ffmpeg process, see ffmpeg_ops.transcode, for the same reason.)

Rounding of half frames follows ffmpeg (half away from zero), never Python's round-half-even.

Timing: a speed change re-times frame *indices*, not decoder timestamps. Frame k of the window is
placed at k / (fps · rate) seconds using an integer timebase, so there is no floating-point
drift, and the chunked AI path, which re-times each chunk using its global frame offset,
produces exactly the same frames as the single-pass path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from fractions import Fraction

from ..media.probe import MediaInfo
from ..project.edit import CropRect, Edit, Trim
from .presets import get_preset
from .scaling import ScalePlan, plan_scaling


class Stage(IntEnum):
    TRIM_RESET = 10
    DEINTERLACE = 20      # step 6
    ORIENT = 30
    CROP = 40
    DENOISE = 50          # step 6
    STABILIZE = 60        # step 6
    AI_BOUNDARY = 100     # frames leave ffmpeg here when AI upscaling
    RESIZE = 110
    CANVAS = 120          # step 4 (alignment / export resolution)
    COLOR = 130           # step 5
    LOOK = 140            # step 5 (filter presets)
    SHARPEN = 150         # step 6
    FINISH = 160          # step 5 (vignette, fade)
    TIME = 170
    FPS = 180


@dataclass(frozen=True)
class VNode:
    stage: Stage
    filters: tuple[str, ...]
    requires: frozenset[str]


@dataclass(frozen=True)
class Window:
    """Which source frames are rendered."""
    start_frame: int     # first frame index in the source
    frames: int          # number of source frames
    fps: Fraction

    @property
    def start(self) -> float:
        return float(self.start_frame / self.fps)

    @property
    def duration(self) -> float:
        return float(self.frames / self.fps)


@dataclass(frozen=True)
class RenderPlan:
    window: Window
    fps_str: str
    trimmed: bool                    # True when the window does not start at frame 0 / cover the whole file
    rate: Fraction                   # playback speed (exact, 3 decimals)
    oriented_size: tuple[int, int]   # after rotation
    crop: CropRect | None
    base_size: tuple[int, int]       # after rotation + crop: what the upscaler (if any) receives
    upscale: ScalePlan | None
    model: str | None
    out_size: tuple[int, int]
    out_frames: int
    nodes: tuple[VNode, ...] = field(default=())
    audio_filters: tuple[str, ...] = ()

    # -- derived -------------------------------------------------------------------------
    @property
    def out_duration(self) -> float:
        """Duration of the rendered video: whole output frames at the source frame rate."""
        return float(Fraction(self.out_frames) / self.window.fps)

    @property
    def requires(self) -> frozenset[str]:
        req: set[str] = set()
        for n in self.nodes:
            req |= n.requires
        req |= {f.split("=", 1)[0] for f in self.audio_filters}
        return frozenset(req)

    def video_filters(self, *, before_ai: bool, timestamp_reset: bool = True) -> list[str]:
        """Static filters on one side of the AI boundary (timing filters excluded).
        `timestamp_reset=False` drops the trim timestamp reset, which is meaningless when frames
        are written out as numbered images."""
        out: list[str] = []
        for n in self.nodes:
            if n.stage in (Stage.TIME, Stage.FPS):
                continue
            if n.stage == Stage.TRIM_RESET and not timestamp_reset:
                continue
            if (n.stage < Stage.AI_BOUNDARY) == before_ai:
                out.extend(n.filters)
        return out

    def timing_filters(self, start_frame: int = 0) -> list[str]:
        """Speed re-timing for frames numbered from `start_frame` (relative to the window)."""
        return timing_filters(self.rate, self.window.fps, start_frame) if self.rate != 1 else []

    def chunk_out_frames(self, start: int, count: int) -> int:
        """Output frames produced by window frames [start, start+count) (telescopes to out_frames)."""
        return round_half_up(Fraction(start + count) / self.rate) - round_half_up(Fraction(start) / self.rate)


# ---------------------------------------------------------------------------------- helpers

STRETCHERS = ("rubberband", "atempo")
AUDIO_PREPAD = 0.5  # seconds of silence appended before stretching; covers what stretchers drop at the end


def round_half_up(x: Fraction | float) -> int:
    """Round like ffmpeg's AV_ROUND_NEAR_INF for positive values (2.5 → 3), unlike Python's round()."""
    return math.floor(Fraction(x) + Fraction(1, 2))


def exact_rate(rate: float) -> Fraction:
    return Fraction(round(rate * 1000), 1000)


def oriented_size(w: int, h: int, rotate: int) -> tuple[int, int]:
    return (h, w) if rotate in (90, 270) else (w, h)


def orientation_filters(rotate: int, flip_h: bool, flip_v: bool) -> list[str]:
    """Rotate clockwise by `rotate`, then mirror in the displayed orientation.

    A 180° turn equals a horizontal plus a vertical flip, so it is folded into the flips
    (two filters fewer, and identical pixels)."""
    out: list[str] = []
    if rotate == 90:
        out.append("transpose=clock")
    elif rotate == 270:
        out.append("transpose=cclock")
    h, v = flip_h, flip_v
    if rotate == 180:
        h, v = not h, not v
    if h:
        out.append("hflip")
    if v:
        out.append("vflip")
    return out


def crop_filter(c: CropRect) -> str:
    return f"crop={c.w}:{c.h}:{c.x}:{c.y}"


def atempo_chain(rate: Fraction | float) -> list[str]:
    """atempo factors whose product is `rate`, each within [0.5, 2.0] (pitch is preserved)."""
    r = float(rate)
    if r <= 0:
        raise ValueError("speed must be positive")
    parts: list[float] = []
    while r > 2.0:
        parts.append(2.0)
        r /= 2.0
    while r < 0.5:
        parts.append(0.5)
        r /= 0.5
    if abs(r - 1.0) > 1e-9 or not parts:
        parts.append(r)
    return [f"atempo={p:.6g}" for p in parts if abs(p - 1.0) > 1e-9]


def stretch_filters(rate: Fraction, stretcher: str) -> list[str]:
    """Pitch-preserving tempo change."""
    if rate == 1:
        return []
    if stretcher == "rubberband":
        # crisp transients + short window: best timing accuracy for speech and on-screen events
        return [f"rubberband=tempo={float(rate):.6g}:transients=crisp:window=short"]
    if stretcher == "atempo":
        return atempo_chain(rate)
    raise ValueError(f"Unknown audio stretcher {stretcher!r}")


def timing_filters(rate: Fraction, fps: Fraction, start_frame: int = 0) -> list[str]:
    """Place frame (start_frame + n) at (start_frame + n) / (fps * rate) seconds, then resample to fps.

    With timebase 1 / (fps.num * p) and rate = p / q, that time is exactly
    (start_frame + n) * fps.den * q ticks, an integer, so no rounding drift is possible."""
    p, q = rate.numerator, rate.denominator
    return [f"settb=1/{fps.numerator * p}",
            f"setpts=({start_frame}+N)*{fps.denominator * q}",
            f"fps={fps.numerator}/{fps.denominator}"]


def compute_window(trim: Trim, info: MediaInfo, max_seconds: float | None) -> tuple[Window, bool]:
    fps = Fraction(info.fps_str)
    if trim.is_default:
        start, end = 0, info.frames
    else:
        s, e = trim.clamp_to(info.duration)
        start = min(info.frames - 1, max(0, round_half_up(s * fps)))
        end = info.frames if trim.end is None else min(info.frames, max(start + 1, round_half_up(e * fps)))
    # "trimmed" means the user chose a sub-range; a preview limit alone keeps Phase 1's output-side cut
    trimmed = start > 0 or end < info.frames
    frames = end - start
    if max_seconds:
        frames = min(frames, max(1, round_half_up(Fraction(max_seconds) * fps)))
    return Window(start, max(1, frames), fps), trimmed


# ---------------------------------------------------------------------------------- builder

def build_plan(edit: Edit, info: MediaInfo, *, max_seconds: float | None = None,
               upscale: bool | None = None, stretcher: str = "rubberband") -> RenderPlan:
    """Plan a render. `upscale` overrides edit.enhance.upscale (None = use the edit);
    `stretcher` picks the audio tempo filter (the pipeline chooses it from ffmpeg's capabilities).

    The edit is used as given: edits coming from a project are already validated, and invalid
    values passed by code (e.g. an upscale target smaller than the source) raise ValueError
    instead of being silently replaced."""
    window, trimmed = compute_window(edit.trim, info, max_seconds)
    t = edit.transform
    nodes: list[VNode] = []

    if trimmed:
        nodes.append(VNode(Stage.TRIM_RESET, ("setpts=PTS-STARTPTS",), frozenset({"setpts"})))

    orient = orientation_filters(t.rotate, t.flip_h, t.flip_v)
    if orient:
        nodes.append(VNode(Stage.ORIENT, tuple(orient), frozenset(f.split("=")[0] for f in orient)))
    ow, oh = oriented_size(info.width, info.height, t.rotate)

    crop = None
    bw, bh = ow, oh
    if t.crop is not None:
        crop = t.crop.fitted(ow, oh)
        if (crop.x, crop.y, crop.w, crop.h) != (0, 0, ow, oh):
            nodes.append(VNode(Stage.CROP, (crop_filter(crop),), frozenset({"crop"})))
            bw, bh = crop.w, crop.h
        else:
            crop = None

    do_upscale = edit.enhance.upscale if upscale is None else upscale
    scale_plan = model = None
    out_w, out_h = bw, bh
    if do_upscale:
        preset = get_preset(edit.enhance.preset)
        scale_plan = plan_scaling(bw, bh, edit.enhance.target, preset.native_scales)
        model = preset.model
        out_w, out_h = scale_plan.out_width, scale_plan.out_height
        if scale_plan.needs_resize:
            nodes.append(VNode(Stage.RESIZE, (f"scale={out_w}:{out_h}:flags=lanczos",), frozenset({"scale"})))

    rate = exact_rate(edit.speed.rate)
    audio: list[str] = []
    if trimmed:
        audio.append("asetpts=PTS-STARTPTS")
    out_frames = max(1, round_half_up(Fraction(window.frames) / rate))
    if rate != 1:
        tf = timing_filters(rate, window.fps)
        nodes.append(VNode(Stage.TIME, tuple(tf[:2]), frozenset({"settb", "setpts"})))
        nodes.append(VNode(Stage.FPS, (tf[2],), frozenset({"fps", "tpad"})))
    if rate != 1 or trimmed:
        # exact length: never shorter than the video (which would cut frames), never longer
        dur = float(Fraction(out_frames) / window.fps)
        audio.append(f"apad=pad_dur={AUDIO_PREPAD}")
        audio.extend(stretch_filters(rate, stretcher))
        audio.append(f"atrim=end={dur:.6f}")

    nodes.sort(key=lambda n: n.stage)
    return RenderPlan(window=window, fps_str=info.fps_str, trimmed=trimmed, rate=rate,
                      oriented_size=(ow, oh), crop=crop, base_size=(bw, bh), upscale=scale_plan,
                      model=model, out_size=(out_w, out_h), out_frames=out_frames,
                      nodes=tuple(nodes), audio_filters=tuple(audio))
