"""Editing operations as ffmpeg filter builders, shared by video, images and previews.

Every function here is pure: it maps user-facing values (slider units from project.edit) to
filter strings. render/graph.py decides *where* each operation runs; this module decides *what*
it does. Keeping the maths here makes it testable and guarantees preview == export.
"""
from __future__ import annotations

import math
from dataclasses import fields, replace

from ..project.edit import Color, Enhance, Filter

# ------------------------------------------------------------------------------------- colour


def _fmt(x: float) -> str:
    """Compact, locale-independent number for filter arguments (0.25, 3, -0.06)."""
    text = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def curve_points(c: Color) -> list[tuple[float, float]]:
    """Tone curve for highlights / shadows / whites / blacks / fade.

    Start from identity points at 0, ¼, ½, ¾, 1. Shadows move the ¼ point, highlights the ¾ point,
    blacks and whites move the end points (lifting/lowering output, or crushing/clipping input),
    fade lifts the black output level. The result is kept strictly increasing in x and
    non-decreasing in y so the curve can never invert tones."""
    x0, y0 = 0.0, 0.0
    x1, y1 = 1.0, 1.0
    if c.blacks > 0:
        y0 = c.blacks / 100 * 0.12
    elif c.blacks < 0:
        x0 = -c.blacks / 100 * 0.12
    if c.whites > 0:
        x1 = 1 - c.whites / 100 * 0.12
    elif c.whites < 0:
        y1 = 1 + c.whites / 100 * 0.12
    y0 = min(0.45, y0 + c.fade / 100 * 0.25)
    span_x, span_y = x1 - x0, y1 - y0

    def mid(t: float, delta: float) -> tuple[float, float]:
        return x0 + span_x * t, y0 + span_y * t + delta

    pts = [(x0, y0), mid(0.25, c.shadows / 100 * 0.12), mid(0.5, 0.0), mid(0.75, c.highlights / 100 * 0.12), (x1, y1)]
    if x0 > 0:
        pts.insert(0, (0.0, y0))
    if x1 < 1:
        pts.append((1.0, y1))
    out: list[tuple[float, float]] = []
    for x, y in pts:
        x, y = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
        if out and x <= out[-1][0]:
            continue
        if out and y < out[-1][1]:
            y = out[-1][1]
        out.append((round(x, 4), round(y, 4)))
    return out


def _curves_needed(c: Color) -> bool:
    return any((c.highlights, c.shadows, c.whites, c.blacks, c.fade))


def color_filters(c: Color) -> tuple[list[str], set[str]]:
    """Colour adjustments in a fixed, documented order:
    exposure → brightness/contrast/saturation/gamma → tone curve → white balance → tint → hue."""
    out: list[str] = []
    req: set[str] = set()
    if c.exposure:
        out.append(f"exposure=exposure={_fmt(c.exposure)}")
        req.add("exposure")
    eq = []
    if c.brightness:
        eq.append(f"brightness={_fmt(c.brightness / 100 * 0.25)}")
    if c.contrast:
        eq.append(f"contrast={_fmt(1 + c.contrast / 100 * 0.6)}")
    if c.saturation:
        eq.append(f"saturation={_fmt(max(0.0, 1 + c.saturation / 100))}")
    if c.gamma != 1:
        eq.append(f"gamma={_fmt(c.gamma)}")
    if eq:
        out.append("eq=" + ":".join(eq))
        req.add("eq")
    if _curves_needed(c):
        pts = " ".join(f"{_fmt(x)}/{_fmt(y)}" for x, y in curve_points(c))
        out.append(f"curves=master='{pts}'")
        req.add("curves")
    if c.temperature:
        # colortemperature renders the scene as if lit at this temperature: lower K looks warmer
        kelvin = round(6500 - c.temperature * 30)
        out.append(f"colortemperature=temperature={kelvin}:mix=1:pl=1")
        req.add("colortemperature")
    if c.tint:
        k = c.tint / 100 * 0.25  # positive = magenta, negative = green (midtones)
        out.append(f"colorbalance=rm={_fmt(k / 2)}:gm={_fmt(-k)}:bm={_fmt(k / 2)}:pl=1")
        req.add("colorbalance")
    if c.hue:
        out.append(f"hue=h={_fmt(c.hue)}")
        req.add("hue")
    return out, req


def vignette_filters(c: Color) -> tuple[list[str], set[str]]:
    if not c.vignette:
        return [], set()
    angle = c.vignette / 100 * (math.pi / 2.6)
    return [f"vignette=angle={_fmt(angle)}"], {"vignette"}


# ------------------------------------------------------------------------------------- looks

# Each look is a set of colour adjustments (added to the user's own) plus optional extra filters.
# Strength scales everything linearly towards "no change", so 50 % is exactly half the effect.
LOOKS: dict[str, dict] = {
    "none": {"label": "None", "color": {}},
    "natural": {"label": "Natural", "color": {"contrast": 8, "saturation": 8, "highlights": -10, "shadows": 8}},
    "cinematic": {"label": "Cinematic",
                  "color": {"contrast": 15, "saturation": -12, "highlights": -15, "fade": 6, "temperature": -6},
                  "extra": lambda s: [f"colorbalance=rs={_fmt(-0.06 * s)}:bs={_fmt(0.08 * s)}"
                                      f":rh={_fmt(0.07 * s)}:bh={_fmt(-0.06 * s)}"]},
    "warm": {"label": "Warm", "color": {"temperature": 35, "tint": 4, "saturation": 6}},
    "cool": {"label": "Cool", "color": {"temperature": -35, "saturation": -4}},
    "vintage": {"label": "Vintage",
                "color": {"fade": 28, "saturation": -28, "contrast": -10, "temperature": 18, "vignette": 30}},
    "bw": {"label": "Black & White", "color": {"saturation": -100, "contrast": 12}},
    "vibrant": {"label": "Vibrant", "color": {"saturation": 22, "contrast": 8},
                "extra": lambda s: [f"vibrance=intensity={_fmt(0.35 * s)}"]},
    "boost3": {"label": "3 Color Boost", "color": {"saturation": 12, "contrast": 6},
               "extra": lambda s: [f"vibrance=intensity={_fmt(0.6 * s)}:rbal=1.4:gbal=1.4:bbal=1.4"]},
}
LOOK_EXTRA_FILTERS = {"colorbalance", "vibrance"}


def apply_look(color: Color, look: Filter) -> tuple[Color, list[str]]:
    """The user's colour settings with the look added on top, plus the look's extra filters."""
    spec = LOOKS.get(look.preset, LOOKS["none"])
    s = look.strength / 100
    if look.preset == "none" or s <= 0:
        return color, []
    changes = {k: getattr(color, k) + v * s for k, v in spec["color"].items()}
    combined = replace(color, **changes).validated()
    extra = spec.get("extra")
    return combined, (extra(s) if extra else [])


# ------------------------------------------------------------------------------------- enhance


def sharpen_filters(amount: float, have_cas: bool = True) -> tuple[list[str], set[str]]:
    """Contrast-adaptive sharpening (no halos) capped at 0.8; unsharp fallback with a capped amount."""
    if amount <= 0:
        return [], set()
    if have_cas:
        return [f"cas=strength={_fmt(amount / 100 * 0.8)}"], {"cas"}
    return [f"unsharp=5:5:{_fmt(amount / 100 * 1.2)}:5:5:0"], {"unsharp"}


def blur_filters(amount: float) -> tuple[list[str], set[str]]:
    if amount <= 0:
        return [], set()
    return [f"gblur=sigma={_fmt(amount / 100 * 12)}"], {"gblur"}


def denoise_filters(e: Enhance) -> tuple[list[str], set[str]]:
    s = e.denoise_strength / 100
    if e.denoise == "fast":
        luma = 1 + s * 7
        return [f"hqdn3d={_fmt(luma)}:{_fmt(luma * 0.75)}:{_fmt(luma * 1.5)}:{_fmt(luma * 1.1)}"], {"hqdn3d"}
    if e.denoise == "quality":
        return [f"nlmeans=s={_fmt(1 + s * 9)}"], {"nlmeans"}
    return [], set()


def deinterlace_filters(mode: str) -> tuple[list[str], set[str]]:
    if mode == "yadif":
        return ["yadif=mode=send_frame:parity=auto:deint=all"], {"yadif"}
    if mode == "bwdif":
        return ["bwdif=mode=send_frame:parity=auto:deint=all"], {"bwdif"}
    return [], set()


STAB_FILE = "transforms.trf"  # relative name: ffmpeg runs in the job's work folder, so no path escaping


def stabilize_detect_filter() -> str:
    return f"vidstabdetect=shakiness=6:accuracy=15:result={STAB_FILE}"


def stabilize_transform_filter(e: Enhance) -> str:
    return (f"vidstabtransform=input={STAB_FILE}:smoothing={e.stab_smoothing}"
            f":zoom={_fmt(e.stab_zoom)}:optzoom=0:interpol=bicubic")


# ------------------------------------------------------------------------------------- canvas


def canvas_filters(alignment: str, w: int, h: int) -> tuple[list[str], set[str]]:
    """Place the picture on a w×h canvas without distorting it (unless 'stretch' is chosen)."""
    if alignment == "fill":
        return [f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos", f"crop={w}:{h}", "setsar=1"], \
            {"scale", "crop", "setsar"}
    if alignment == "blur":
        return [f"split[fwbg][fwfg];[fwbg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"gblur=sigma=30[fwbgb];[fwfg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fwfgs];"
                f"[fwbgb][fwfgs]overlay=(W-w)/2:(H-h)/2", "setsar=1"], \
            {"split", "scale", "crop", "gblur", "overlay", "setsar"}
    if alignment == "stretch":
        return [f"scale={w}:{h}:flags=lanczos", "setsar=1"], {"scale", "setsar"}
    return [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1"], {"scale", "pad", "setsar"}


def gif_filters(fps: str, colors: int = 256) -> tuple[list[str], set[str]]:
    return [f"fps={fps}", f"split[fwg1][fwg2];[fwg1]palettegen=max_colors={colors}:stats_mode=diff[fwpal];"
                          f"[fwg2][fwpal]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"], \
        {"fps", "split", "palettegen", "paletteuse"}


# ------------------------------------------------------------------------------------- audio


def audio_level_filters(volume_db: float, normalize: bool) -> tuple[list[str], set[str]]:
    out, req = [], set()
    if volume_db:
        out.append(f"volume={_fmt(volume_db)}dB")
        req.add("volume")
    if normalize:
        out.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        req.add("loudnorm")
    return out, req


def audio_fade_filters(fade_in: float, fade_out: float, duration: float) -> tuple[list[str], set[str]]:
    out = []
    fi = min(fade_in, duration)
    fo = min(fade_out, duration)
    if fi > 0:
        out.append(f"afade=t=in:st=0:d={_fmt(fi)}")
    if fo > 0:
        out.append(f"afade=t=out:st={_fmt(max(0.0, duration - fo))}:d={_fmt(fo)}")
    return out, ({"afade"} if out else set())


def color_is_neutral(c: Color) -> bool:
    return all(getattr(c, f.name) == f.default for f in fields(Color))
