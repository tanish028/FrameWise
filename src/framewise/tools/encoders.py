"""Video encoders: ffmpeg arguments per encoder, and detection of which ones actually work here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ToolError
from ..core.logging_setup import get_logger
from ..core.proc import run_cancellable

log = get_logger("encoders")


@dataclass(frozen=True)
class Encoder:
    key: str
    label: str
    hardware: bool


ENCODERS: dict[str, Encoder] = {e.key: e for e in (
    Encoder("libx264", "H.264 · CPU (most compatible)", False),
    Encoder("libx265", "H.265 · CPU (smaller files)", False),
    Encoder("h264_nvenc", "H.264 · NVIDIA GPU", True),
    Encoder("hevc_nvenc", "H.265 · NVIDIA GPU", True),
    Encoder("h264_amf", "H.264 · AMD GPU", True),
    Encoder("hevc_amf", "H.265 · AMD GPU", True),
    Encoder("h264_qsv", "H.264 · Intel GPU", True),
    Encoder("hevc_qsv", "H.265 · Intel GPU", True),
    Encoder("libvpx-vp9", "VP9 · CPU (WebM)", False),
    Encoder("libsvtav1", "AV1 · CPU (WebM, smallest, slow)", False),
)}
DEFAULT_ENCODER = "libx264"

# Which video encoders each container accepts (GIF uses its own palette encoder)
FORMAT_ENCODERS = {
    "mp4": ("libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf", "h264_qsv", "hevc_qsv"),
    "mov": ("libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf", "h264_qsv", "hevc_qsv"),
    "webm": ("libvpx-vp9", "libsvtav1"),
    "gif": (),
}
TWO_PASS_ENCODERS = ("libx264", "libvpx-vp9")  # encoders where FrameWise runs a 2-pass target-size encode


def encoder_for_format(fmt: str, preferred: str, available: list[str] | None = None) -> str | None:
    """The preferred encoder if the container accepts it, else the first usable one for that container."""
    allowed = FORMAT_ENCODERS.get(fmt, ())
    if not allowed:
        return None
    usable = [e for e in allowed if available is None or e in available]
    if preferred in usable:
        return preferred
    return usable[0] if usable else allowed[0]


def bitrate_args(key: str, kbps: int) -> list[str]:
    """Average-bitrate encoding (used for 'bitrate' and 'target size' rate control)."""
    k = max(50, int(kbps))
    base = {
        "libx264": ["-c:v", "libx264", "-preset", "medium"],
        "libx265": ["-c:v", "libx265", "-preset", "medium", "-tag:v", "hvc1"],
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr"],
        "hevc_nvenc": ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-tag:v", "hvc1"],
        "h264_amf": ["-c:v", "h264_amf", "-rc", "vbr_peak"],
        "hevc_amf": ["-c:v", "hevc_amf", "-rc", "vbr_peak", "-tag:v", "hvc1"],
        "h264_qsv": ["-c:v", "h264_qsv"],
        "hevc_qsv": ["-c:v", "hevc_qsv", "-tag:v", "hvc1"],
        "libvpx-vp9": ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1"],
        "libsvtav1": ["-c:v", "libsvtav1", "-preset", "8"],
    }
    if key not in base:
        raise ValueError(f"Unknown encoder {key!r}")
    return base[key] + ["-b:v", f"{k}k", "-maxrate", f"{int(k * 1.5)}k", "-bufsize", f"{k * 2}k"]


def encoder_args(key: str, quality: int) -> list[str]:
    """Constant-quality settings; `quality` follows the CRF scale (lower = better, ~18 visually lossless)."""
    q = str(int(quality))
    hevc_tag = ["-tag:v", "hvc1"]
    table = {
        "libx264": ["-c:v", "libx264", "-preset", "medium", "-crf", q],
        "libx265": ["-c:v", "libx265", "-preset", "medium", "-crf", q] + hevc_tag,
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0"],
        "hevc_nvenc": ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0"] + hevc_tag,
        "h264_amf": ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", q, "-qp_p", q],
        "hevc_amf": ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_i", q, "-qp_p", q] + hevc_tag,
        "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", q],
        "hevc_qsv": ["-c:v", "hevc_qsv", "-global_quality", q] + hevc_tag,
        # VP9/AV1 use their own CRF scales; map FrameWise's 10-35 quality onto comparable values
        "libvpx-vp9": ["-c:v", "libvpx-vp9", "-crf", str(min(63, round(int(quality) * 1.4 + 6))), "-b:v", "0",
                       "-deadline", "good", "-cpu-used", "4", "-row-mt", "1"],
        "libsvtav1": ["-c:v", "libsvtav1", "-crf", str(min(63, int(quality) + 12)), "-preset", "8"],
    }
    try:
        return table[key]
    except KeyError:
        raise ValueError(f"Unknown encoder {key!r}") from None


def compiled_encoders(ffmpeg: Path) -> set[str]:
    out = run_cancellable([ffmpeg, "-hide_banner", "-encoders"])
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def detect_available(ffmpeg: Path) -> list[str]:
    """Encoders that are compiled in AND work on this machine (hardware ones get a test encode)."""
    try:
        compiled = compiled_encoders(ffmpeg)
    except ToolError:
        return [DEFAULT_ENCODER]
    usable = []
    for key, enc in ENCODERS.items():
        if key not in compiled:
            continue
        if enc.hardware:
            try:
                run_cancellable([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                                 "-frames:v", "3", *encoder_args(key, 23), "-f", "null", "-"],
                                expect_failure=True)
            except ToolError:
                log.info("Encoder %s present but not usable on this machine", key)
                continue
        usable.append(key)
    return usable or [DEFAULT_ENCODER]
