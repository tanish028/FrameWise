"""ffprobe wrapper returning a plain MediaInfo record."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from ..core.errors import ToolError
from ..core.proc import run_cancellable

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp")


@dataclass(frozen=True)
class MediaInfo:
    width: int          # display width (after rotation)
    height: int         # display height (after rotation)
    fps: float
    fps_str: str        # exact rational, e.g. "30000/1001"
    frames: int
    duration: float     # seconds
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    size_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaInfo":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})

    @property
    def resolution(self) -> str:
        return f"{self.width}×{self.height}"


def _fraction(s: str | None) -> Fraction:
    try:
        return Fraction(s or "0")
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def parse_probe_json(text: str, size_bytes: int = 0) -> MediaInfo:
    """Turn `ffprobe -print_format json -show_streams -show_format` output into MediaInfo."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolError("Could not read the video's information") from e
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and (s.get("disposition") or {}).get("attached_pic") != 1), None)
    if video is None:
        raise ToolError("This file has no video stream")
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if w <= 0 or h <= 0:
        raise ToolError("Could not determine the video size")
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    tag_rot = (video.get("tags") or {}).get("rotate")
    if tag_rot:
        rotation = int(float(tag_rot))
    if abs(rotation) % 180 == 90:
        w, h = h, w
    fps = _fraction(video.get("avg_frame_rate"))
    if fps <= 0:
        fps = _fraction(video.get("r_frame_rate"))
    if fps <= 0:
        raise ToolError("Could not determine the video's frame rate")
    fmt = data.get("format") or {}
    duration = float(video.get("duration") or fmt.get("duration") or 0)
    nb = str(video.get("nb_frames") or "")
    frames = int(nb) if nb.isdigit() and int(nb) > 0 else int(round(duration * float(fps)))
    if frames <= 0:
        raise ToolError("Could not determine the number of frames")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return MediaInfo(
        width=w, height=h, fps=float(fps), fps_str=f"{fps.numerator}/{fps.denominator}",
        frames=frames, duration=duration or frames / float(fps), has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or "?"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        size_bytes=int(fmt.get("size") or size_bytes or 0),
    )


def probe(ffprobe: Path, src: Path) -> MediaInfo:
    if not src.is_file():
        raise ToolError(f"File not found: {src}")
    out = run_cancellable([ffprobe, "-v", "error", "-print_format", "json",
                           "-show_streams", "-show_format", src])
    return parse_probe_json(out, src.stat().st_size)


def format_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
