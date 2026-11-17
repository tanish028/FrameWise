"""ffprobe wrapper returning a plain MediaInfo record."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from ..core.errors import ToolError
from ..core.proc import run_cancellable

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
GIF_EXTENSIONS = (".gif",)
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma")
ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS + IMAGE_EXTENSIONS + GIF_EXTENSIONS + AUDIO_EXTENSIONS

# Kinds of media. Images and image sequences share the still-image tools; video, GIF and sequences play.
VIDEO, IMAGE, AUDIO, GIF, SEQUENCE = "video", "image", "audio", "gif", "sequence"
KINDS = (VIDEO, IMAGE, AUDIO, GIF, SEQUENCE)
_STILL_CODECS = {"png", "mjpeg", "webp", "bmp", "tiff", "jpeg2000", "jpegls", "ppm", "pgm"}
_STILL_FORMATS = {"image2", "png_pipe", "jpeg_pipe", "webp_pipe", "bmp_pipe", "tiff_pipe", "jpegls_pipe"}
AUDIO_FPS = "100/1"  # audio-only media use a 10 ms "frame" grid so trims share the video window maths


@dataclass(frozen=True)
class MediaInfo:
    width: int          # display width (after rotation); 0 for audio
    height: int         # display height (after rotation); 0 for audio
    fps: float
    fps_str: str        # exact rational, e.g. "30000/1001"
    frames: int
    duration: float     # seconds
    has_audio: bool
    video_codec: str
    audio_codec: str | None
    size_bytes: int
    kind: str = VIDEO
    has_video: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaInfo":
        """Tolerant: info saved by older versions has no kind/has_video and was always a video."""
        fields = cls.__dataclass_fields__
        required = [k for k, f in fields.items() if f.default is dataclasses.MISSING]
        missing = [k for k in required if k not in d]
        if missing:
            raise KeyError(", ".join(missing))
        return cls(**{k: d[k] for k in fields if k in d})

    @property
    def resolution(self) -> str:
        return f"{self.width}×{self.height}" if self.has_video else "audio"

    @property
    def is_still(self) -> bool:
        return self.kind == IMAGE

    @property
    def is_timed(self) -> bool:
        """Has a timeline (can be trimmed, re-timed, played)."""
        return self.kind != IMAGE


def _fraction(s: str | None) -> Fraction:
    try:
        return Fraction(s or "0")
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def kind_for_extension(path: Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return IMAGE
    if ext in GIF_EXTENSIONS:
        return GIF
    if ext in AUDIO_EXTENSIONS:
        return AUDIO
    if ext in VIDEO_EXTENSIONS:
        return VIDEO
    return None


def parse_probe_json(text: str, size_bytes: int = 0, path: Path | None = None) -> MediaInfo:
    """Turn `ffprobe -print_format json -show_streams -show_format` output into MediaInfo."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolError("Could not read the file's information") from e
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    fmt_names = set(str(fmt.get("format_name") or "").split(","))
    size = int(fmt.get("size") or size_bytes or 0)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and (s.get("disposition") or {}).get("attached_pic") != 1), None)

    if video is None:
        if audio is None:
            raise ToolError("This file has no video, image or audio stream")
        duration = float(audio.get("duration") or fmt.get("duration") or 0)
        if duration <= 0:
            raise ToolError("Could not determine the audio's length")
        fps = Fraction(AUDIO_FPS)
        return MediaInfo(0, 0, float(fps), AUDIO_FPS, max(1, round(duration * fps)), duration, True,
                         "none", str(audio.get("codec_name")), size, kind=AUDIO, has_video=False)

    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if w <= 0 or h <= 0:
        raise ToolError("Could not determine the picture size")
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    tag_rot = (video.get("tags") or {}).get("rotate")
    if tag_rot:
        rotation = int(float(tag_rot))
    if abs(rotation) % 180 == 90:
        w, h = h, w
    codec = str(video.get("codec_name") or "?")
    nb = str(video.get("nb_frames") or "")
    ext_kind = kind_for_extension(path) if path else None

    still = (codec in _STILL_CODECS and (fmt_names & _STILL_FORMATS or ext_kind == IMAGE)
             and (not nb.isdigit() or int(nb) <= 1))
    if still:
        return MediaInfo(w, h, 25.0, "25/1", 1, 0.04, False, codec, None, size, kind=IMAGE)

    fps = _fraction(video.get("avg_frame_rate"))
    if fps <= 0:
        fps = _fraction(video.get("r_frame_rate"))
    if fps <= 0:
        raise ToolError("Could not determine the video's frame rate")
    duration = float(video.get("duration") or fmt.get("duration") or 0)
    frames = int(nb) if nb.isdigit() and int(nb) > 0 else int(round(duration * float(fps)))
    kind = GIF if ("gif" in fmt_names or codec == "gif") else VIDEO
    if kind == GIF and frames <= 1:
        return MediaInfo(w, h, 25.0, "25/1", 1, 0.04, False, codec, None, size, kind=IMAGE)
    if frames <= 0:
        raise ToolError("Could not determine the number of frames")
    return MediaInfo(
        width=w, height=h, fps=float(fps), fps_str=f"{fps.numerator}/{fps.denominator}",
        frames=frames, duration=duration or frames / float(fps), has_audio=audio is not None,
        video_codec=codec, audio_codec=str(audio.get("codec_name")) if audio else None,
        size_bytes=size, kind=kind,
    )


def probe(ffprobe: Path, src: Path) -> MediaInfo:
    if not src.is_file():
        raise ToolError(f"File not found: {src}")
    out = run_cancellable([ffprobe, "-v", "error", "-print_format", "json",
                           "-show_streams", "-show_format", src])
    return parse_probe_json(out, src.stat().st_size, src)


def probe_source(ffprobe: Path, src: Path, sequence=None) -> MediaInfo:
    """Probe a file, or an image sequence (probing its first image for the picture size)."""
    if sequence is None:
        return probe(ffprobe, src)
    first = probe(ffprobe, src)
    if not first.has_video:
        raise ToolError("The first file of the image sequence is not an image")
    fps = Fraction(sequence.fps)
    return MediaInfo(first.width, first.height, float(fps), f"{fps.numerator}/{fps.denominator}", sequence.count,
                     float(sequence.count / fps), False, first.video_codec, None, first.size_bytes * sequence.count,
                     kind=SEQUENCE)


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
