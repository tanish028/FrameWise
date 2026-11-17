"""AI upscaling presets: which Real-ESRGAN model suits which kind of footage."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UpscalePreset:
    key: str
    label: str
    description: str
    model: str
    native_scales: tuple[int, ...]  # output scales the model supports directly


PRESETS: dict[str, UpscalePreset] = {p.key: p for p in (
    UpscalePreset("general", "General / mixed",
                  "Safe default for anything. Real-world detail recovery, slowest.",
                  "realesrgan-x4plus", (4,)),
    UpscalePreset("live", "Live action / film",
                  "Camera footage that is already fairly clean. Keeps texture, less smoothing.",
                  "realesrnet-x4plus", (4,)),
    UpscalePreset("anime", "Anime / cartoon (fast)",
                  "Animation. Tuned for video and flicker-resistant. Fastest.",
                  "realesr-animevideov3", (2, 3, 4)),
    UpscalePreset("anime_hq", "Anime / illustration (quality)",
                  "Sharper line art for anime and drawings. Slower than the fast preset.",
                  "realesrgan-x4plus-anime", (4,)),
    UpscalePreset("game", "Game / screen recording",
                  "Clean rendered footage: gameplay, screen captures, UI. Fast.",
                  "realesr-animevideov3", (2, 3, 4)),
)}

DEFAULT_PRESET = "general"


def get_preset(key: str) -> UpscalePreset:
    try:
        return PRESETS[key]
    except KeyError:
        raise ValueError(f"Unknown preset {key!r}. Choose from: {', '.join(PRESETS)}") from None
