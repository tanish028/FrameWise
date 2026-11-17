"""The non-destructive edit applied to one clip (project format v2).

An Edit is an immutable tree of sections, one per inspector panel. Every section knows how to
repair its own values (`validated`), so a hand-edited, older or damaged project can never make a
render crash. Values are stored in user-facing units (seconds, degrees, -100..100 sliders, dB);
turning them into ffmpeg filters is the job of render/, not of the model.

Neutral defaults mean "leave the video unchanged", so a default Edit renders exactly like Phase 1.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

from ..render.presets import DEFAULT_PRESET, PRESETS
from ..render.scaling import TARGET_CHOICES
from ..tools.encoders import DEFAULT_ENCODER, ENCODERS

# --------------------------------------------------------------------------- value helpers


def _num(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return min(hi, max(lo, v))


def _int(value: Any, lo: int, hi: int, default: int) -> int:
    return int(round(_num(value, lo, hi, default)))


def _choice(value: Any, allowed, default):
    try:
        return value if value in allowed else default
    except TypeError:  # unhashable junk (list/dict) checked against a dict of choices
        return default


def _even(n: int) -> int:
    return n - (n % 2)


def _section_from_dict(cls, d: Any):
    """Build a section from a dict, ignoring unknown keys and repairing bad values."""
    if not isinstance(d, dict):
        return cls()
    names = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in d.items() if k in names}
    try:
        obj = cls(**kwargs)
    except TypeError:
        obj = cls()
    return obj.validated()


# --------------------------------------------------------------------------- slider specs


@dataclass(frozen=True)
class AdjustSpec:
    """Describes one slider: range, default, display unit and a plain-language tooltip."""
    name: str
    label: str
    lo: float
    hi: float
    default: float
    step: float
    unit: str
    tip: str


COLOR_BASIC = (
    AdjustSpec("brightness", "Brightness", -100, 100, 0, 1, "",
               "Lightens or darkens the whole picture evenly."),
    AdjustSpec("contrast", "Contrast", -100, 100, 0, 1, "",
               "Increases or reduces the difference between light and dark areas."),
    AdjustSpec("saturation", "Saturation", -100, 100, 0, 1, "",
               "Makes colours more vivid, or less. -100 gives black and white."),
    AdjustSpec("hue", "Hue shift", -180, 180, 0, 1, "°",
               "Rotates every colour around the colour wheel."),
    AdjustSpec("gamma", "Gamma", 0.3, 3.0, 1.0, 0.01, "",
               "Brightens (above 1) or darkens (below 1) mid-tones while keeping pure black and white."),
    AdjustSpec("exposure", "Exposure", -3.0, 3.0, 0.0, 0.05, " EV",
               "Simulates opening or closing the camera aperture. +1 EV doubles the light."),
    AdjustSpec("temperature", "Temperature", -100, 100, 0, 1, "",
               "Shifts colours towards warm orange (positive) or cool blue (negative)."),
    AdjustSpec("tint", "Tint", -100, 100, 0, 1, "",
               "Shifts colours towards magenta (positive) or green (negative). Corrects colour casts."),
)
COLOR_ADVANCED = (
    AdjustSpec("highlights", "Highlights", -100, 100, 0, 1, "",
               "Recovers (negative) or boosts (positive) the brightest areas without touching shadows."),
    AdjustSpec("shadows", "Shadows", -100, 100, 0, 1, "",
               "Lifts (positive) or deepens (negative) dark areas without touching highlights."),
    AdjustSpec("whites", "Whites", -100, 100, 0, 1, "",
               "Moves the white point: how bright the brightest pixels become."),
    AdjustSpec("blacks", "Blacks", -100, 100, 0, 1, "",
               "Moves the black point: how dark the darkest pixels become."),
    AdjustSpec("vignette", "Vignette", 0, 100, 0, 1, "",
               "Darkens the corners to draw attention to the centre."),
    AdjustSpec("fade", "Fade", 0, 100, 0, 1, "",
               "Lifts blacks for a soft, matte, film-like look."),
)
COLOR_SPECS = COLOR_BASIC + COLOR_ADVANCED
COLOR_SPEC_BY_NAME = {s.name: s for s in COLOR_SPECS}

# --------------------------------------------------------------------------- sections


@dataclass(frozen=True)
class Trim:
    """Part of the source to use, in source seconds. end=None means "to the end"."""
    start: float = 0.0
    end: float | None = None

    MIN_LENGTH = 0.1

    def validated(self) -> "Trim":
        start = round(_num(self.start, 0.0, 1e7, 0.0), 3)
        end = None
        if self.end is not None:
            e = _num(self.end, 0.0, 1e7, -1.0)
            if e >= start + self.MIN_LENGTH:
                end = round(e, 3)
        return replace(self, start=start, end=end)

    @property
    def is_default(self) -> bool:
        return self.start == 0.0 and self.end is None

    def clamp_to(self, duration: float) -> tuple[float, float]:
        """Effective (start, end) for a source of `duration` seconds."""
        start = min(self.start, max(0.0, duration - self.MIN_LENGTH))
        end = duration if self.end is None else min(self.end, duration)
        return start, max(end, start + min(self.MIN_LENGTH, duration))


@dataclass(frozen=True)
class CropRect:
    """Crop in pixels of the frame *after* rotation/flip. Always even values so yuv420 output
    is exactly the rectangle the user drew."""
    x: int
    y: int
    w: int
    h: int

    def validated(self) -> "CropRect":
        return CropRect(_even(max(0, int(self.x))), _even(max(0, int(self.y))),
                        max(2, _even(int(self.w))), max(2, _even(int(self.h))))

    def fitted(self, frame_w: int, frame_h: int) -> "CropRect":
        """Clamp to a frame of the given size."""
        w = min(self.w, _even(frame_w))
        h = min(self.h, _even(frame_h))
        x = min(self.x, _even(frame_w - w))
        y = min(self.y, _even(frame_h - h))
        return CropRect(max(0, x), max(0, y), max(2, w), max(2, h))


CROP_RATIOS = ("free", "16:9", "9:16", "1:1", "4:3", "4:5", "21:9")
ALIGNMENTS = ("fit", "fill", "blur", "stretch")
ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class Transform:
    rotate: int = 0           # clockwise degrees
    flip_h: bool = False
    flip_v: bool = False
    crop: CropRect | None = None
    crop_ratio: str = "free"  # ratio lock used by the crop tool
    alignment: str = "fit"    # how the picture fills a different-shaped export canvas

    def validated(self) -> "Transform":
        crop = self.crop
        if isinstance(crop, dict):
            try:
                crop = CropRect(**{k: crop[k] for k in ("x", "y", "w", "h")})
            except (KeyError, TypeError):
                crop = None
        if crop is not None:
            try:
                crop = crop.validated()
            except (TypeError, ValueError, AttributeError):
                crop = None
        rot = _int(self.rotate, -720, 720, 0) % 360
        return replace(self, rotate=rot if rot in ROTATIONS else 0, flip_h=bool(self.flip_h),
                       flip_v=bool(self.flip_v), crop=crop,
                       crop_ratio=_choice(self.crop_ratio, CROP_RATIOS, "free"),
                       alignment=_choice(self.alignment, ALIGNMENTS, "fit"))


def _color_defaults() -> dict[str, float]:
    return {s.name: s.default for s in COLOR_SPECS}


@dataclass(frozen=True)
class Color:
    brightness: float = 0
    contrast: float = 0
    saturation: float = 0
    hue: float = 0
    gamma: float = 1.0
    exposure: float = 0.0
    temperature: float = 0
    tint: float = 0
    highlights: float = 0
    shadows: float = 0
    whites: float = 0
    blacks: float = 0
    vignette: float = 0
    fade: float = 0

    def validated(self) -> "Color":
        vals = {}
        for s in COLOR_SPECS:
            v = _num(getattr(self, s.name), s.lo, s.hi, s.default)
            vals[s.name] = round(round(v / s.step) * s.step, 4)
        return replace(self, **vals)

    @property
    def is_default(self) -> bool:
        return all(getattr(self, k) == v for k, v in _color_defaults().items())


FILTER_PRESETS = ("none", "natural", "cinematic", "warm", "cool", "vintage", "bw", "vibrant", "boost3")


@dataclass(frozen=True)
class Filter:
    preset: str = "none"
    strength: float = 100  # percent

    def validated(self) -> "Filter":
        return replace(self, preset=_choice(self.preset, FILTER_PRESETS, "none"),
                       strength=round(_num(self.strength, 0, 100, 100)))


DENOISE_MODES = ("off", "fast", "quality")
DEINTERLACE_MODES = ("off", "yadif", "bwdif")


@dataclass(frozen=True)
class Enhance:
    upscale: bool = True
    preset: str = DEFAULT_PRESET
    target: str = "2x"
    denoise: str = "off"
    denoise_strength: float = 40
    deinterlace: str = "off"
    sharpen: float = 0
    blur: float = 0
    stabilize: bool = False
    stab_smoothing: int = 15     # frames each way used to smooth the camera path
    stab_zoom: float = 5         # percent zoom to hide moving edges (crop compensation)

    def validated(self) -> "Enhance":
        return replace(
            self, upscale=bool(self.upscale),
            preset=_choice(self.preset, PRESETS, DEFAULT_PRESET),
            target=_choice(self.target, TARGET_CHOICES, "2x"),
            denoise=_choice(self.denoise, DENOISE_MODES, "off"),
            denoise_strength=round(_num(self.denoise_strength, 0, 100, 40)),
            deinterlace=_choice(self.deinterlace, DEINTERLACE_MODES, "off"),
            sharpen=round(_num(self.sharpen, 0, 100, 0)),
            blur=round(_num(self.blur, 0, 100, 0)),
            stabilize=bool(self.stabilize),
            stab_smoothing=_int(self.stab_smoothing, 1, 60, 15),
            stab_zoom=round(_num(self.stab_zoom, 0, 20, 5), 1),
        )


SPEED_PRESETS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 4.0)
SPEED_RANGE = (0.25, 4.0)


@dataclass(frozen=True)
class Speed:
    rate: float = 1.0
    reverse: bool = False

    def validated(self) -> "Speed":
        return replace(self, rate=round(_num(self.rate, *SPEED_RANGE, 1.0), 3), reverse=bool(self.reverse))


REPLACE_FITS = ("trim", "loop")


@dataclass(frozen=True)
class Audio:
    mute: bool = False
    replace_path: str = ""     # external audio file; empty = keep original
    replace_fit: str = "trim"  # what to do when the new audio is shorter/longer than the video
    volume_db: float = 0.0
    normalize: bool = False
    fade_in: float = 0.0       # seconds, on the output timeline
    fade_out: float = 0.0

    def validated(self) -> "Audio":
        return replace(self, mute=bool(self.mute), replace_path=str(self.replace_path or ""),
                       replace_fit=_choice(self.replace_fit, REPLACE_FITS, "trim"),
                       volume_db=round(_num(self.volume_db, -30, 12, 0.0), 1),
                       normalize=bool(self.normalize),
                       fade_in=round(_num(self.fade_in, 0, 30, 0.0), 2),
                       fade_out=round(_num(self.fade_out, 0, 30, 0.0), 2))


EXPORT_FORMATS = ("mp4", "mov", "webm", "gif")
IMAGE_FORMATS = ("png", "jpeg", "webp", "bmp")
AUDIO_FORMATS = ("mp3", "m4a", "wav", "flac")
AUDIO_CODECS = ("aac", "opus", "mp3", "pcm")
RATE_CONTROLS = ("quality", "bitrate", "size")
QUALITY_RANGE = (10, 35)


@dataclass(frozen=True)
class Export:
    format: str = "mp4"
    video_codec: str = DEFAULT_ENCODER
    audio_codec: str = "aac"
    audio_bitrate: int = 192    # kbps
    width: int = 0              # 0 = follow the source (after crop/rotation/upscale)
    height: int = 0
    fps: float = 0              # 0 = same as source
    rate_control: str = "quality"
    quality: int = 18           # CRF-like scale, lower = better
    video_bitrate: int = 8000   # kbps, for rate_control == "bitrate"
    target_mb: float = 50.0     # for rate_control == "size"
    preset_name: str = "custom"  # last applied export preset, for display only
    output: str = ""            # empty = automatic name next to the source
    image_format: str = "png"   # still images
    image_quality: int = 92     # JPEG/WebP quality 1-100 (100 = lossless WebP)
    audio_format: str = "mp3"   # audio-only exports (audio files, "extract audio")

    def validated(self) -> "Export":
        lo, hi = QUALITY_RANGE
        w, h = _int(self.width, 0, 7680, 0), _int(self.height, 0, 7680, 0)
        if (w == 0) != (h == 0):  # half-specified resolution is meaningless
            w = h = 0
        return replace(
            self, format=_choice(self.format, EXPORT_FORMATS, "mp4"),
            video_codec=_choice(self.video_codec, ENCODERS, DEFAULT_ENCODER),
            audio_codec=_choice(self.audio_codec, AUDIO_CODECS, "aac"),
            audio_bitrate=_int(self.audio_bitrate, 32, 512, 192),
            width=_even(w), height=_even(h),
            fps=round(_num(self.fps, 0, 240, 0), 3),
            rate_control=_choice(self.rate_control, RATE_CONTROLS, "quality"),
            quality=_int(self.quality, lo, hi, 18),
            video_bitrate=_int(self.video_bitrate, 100, 200_000, 8000),
            target_mb=round(_num(self.target_mb, 0.1, 100_000, 50.0), 2),
            preset_name=str(self.preset_name or "custom"),
            output=str(self.output or ""),
            image_format=_choice(self.image_format, IMAGE_FORMATS, "png"),
            image_quality=_int(self.image_quality, 1, 100, 92),
            audio_format=_choice(self.audio_format, AUDIO_FORMATS, "mp3"),
        )


SECTIONS: dict[str, type] = {
    "trim": Trim, "transform": Transform, "color": Color, "filter": Filter,
    "enhance": Enhance, "speed": Speed, "audio": Audio, "export": Export,
}


@dataclass(frozen=True)
class Edit:
    trim: Trim = field(default_factory=Trim)
    transform: Transform = field(default_factory=Transform)
    color: Color = field(default_factory=Color)
    filter: Filter = field(default_factory=Filter)
    enhance: Enhance = field(default_factory=Enhance)
    speed: Speed = field(default_factory=Speed)
    audio: Audio = field(default_factory=Audio)
    export: Export = field(default_factory=Export)

    def validated(self) -> "Edit":
        return Edit(**{name: getattr(self, name).validated() for name in SECTIONS})

    def with_section(self, section: str, **changes) -> "Edit":
        """Return a copy with fields of one section changed (and re-validated)."""
        if section not in SECTIONS:
            raise ValueError(f"Unknown edit section {section!r}")
        current = getattr(self, section)
        names = {f.name for f in fields(current)}
        unknown = set(changes) - names
        if unknown:
            raise ValueError(f"Unknown field(s) for {section}: {', '.join(sorted(unknown))}")
        return replace(self, **{section: replace(current, **changes).validated()})

    def reset_section(self, section: str) -> "Edit":
        if section not in SECTIONS:
            raise ValueError(f"Unknown edit section {section!r}")
        return replace(self, **{section: SECTIONS[section]()})

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "Edit":
        d = d if isinstance(d, dict) else {}
        return cls(**{name: _section_from_dict(sec_cls, d.get(name)) for name, sec_cls in SECTIONS.items()})

    def fingerprint(self, *sections: str) -> str:
        """Stable hash of the given sections (all if none) — used to decide if render work can be reused."""
        names = sections or tuple(SECTIONS)
        payload = {n: asdict(getattr(self, n)) for n in names}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
