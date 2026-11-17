"""Platform export presets. Pure functions: (preset, source) → export settings.

Upload limits change; the sizes below were checked in November 2026 and are deliberately a little
under each limit (Discord: 20 MB for accounts without Nitro; WhatsApp: 16 MB is the conservative
figure for videos sent as media). Every value stays editable after a preset is applied.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..media.probe import MediaInfo


@dataclass(frozen=True)
class ExportPreset:
    key: str
    label: str
    note: str


PRESETS: dict[str, ExportPreset] = {p.key: p for p in (
    ExportPreset("custom", "Custom", "Your own settings."),
    ExportPreset("youtube", "YouTube 1080p", "H.264 1080p, high quality. Vertical videos keep their shape."),
    ExportPreset("youtube4k", "YouTube 4K", "H.264 2160p, high quality."),
    ExportPreset("shorts", "YouTube Shorts", "1080×1920 vertical; wide videos get a blurred background."),
    ExportPreset("reels", "Instagram Reels", "1080×1920 vertical at 30 fps; wide videos get a blurred background."),
    ExportPreset("whatsapp", "WhatsApp", "720p, about 15 MB so it sends as a normal video. Size is approximate."),
    ExportPreset("discord", "Discord", "720p, about 19 MB (under the 20 MB free upload limit). Size is approximate."),
    ExportPreset("gif", "GIF (480 px, 12 fps)", "Small animated GIF without sound."),
)}


def _oriented(w: int, h: int, info: MediaInfo | None) -> tuple[int, int]:
    """Landscape size for landscape sources, swapped for vertical ones."""
    if info is not None and info.height > info.width:
        return h, w
    return w, h


def preset_settings(key: str, info: MediaInfo | None) -> tuple[dict, dict]:
    """(export section changes, transform section changes) for a preset."""
    base = dict(preset_name=key, format="mp4", video_codec="libx264", audio_codec="aac", rate_control="quality",
                fps=0)
    if key == "youtube":
        w, h = _oriented(1920, 1080, info)
        return {**base, "width": w, "height": h, "quality": 18, "audio_bitrate": 192}, {"alignment": "fit"}
    if key == "youtube4k":
        w, h = _oriented(3840, 2160, info)
        return {**base, "width": w, "height": h, "quality": 18, "audio_bitrate": 192}, {"alignment": "fit"}
    if key == "shorts":
        return {**base, "width": 1080, "height": 1920, "quality": 20, "audio_bitrate": 192}, {"alignment": "blur"}
    if key == "reels":
        return {**base, "width": 1080, "height": 1920, "quality": 20, "fps": 30, "audio_bitrate": 128}, \
            {"alignment": "blur"}
    if key in ("whatsapp", "discord"):
        w, h = _oriented(1280, 720, info)
        mb = 15.0 if key == "whatsapp" else 19.0
        return {**base, "width": w, "height": h, "rate_control": "size", "target_mb": mb, "audio_bitrate": 128}, \
            {"alignment": "fit"}
    if key == "gif":
        w, h = (480, 270) if info is None or info.height == 0 else (480, max(2, round(480 * info.height / info.width / 2) * 2))
        return {**base, "format": "gif", "width": w, "height": h, "fps": 12}, {"alignment": "fit"}
    return {"preset_name": "custom"}, {}
