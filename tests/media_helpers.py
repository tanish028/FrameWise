"""Synthetic test media and pixel/sample readers (pure Python, no numpy) for real-ffmpeg tests."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
from fractions import Fraction
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MARK = (0, 0, 16, 8)  # red block (x, y, w, h) in marker videos: asymmetric so rotations are distinguishable


def ff(*args) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", *map(str, args)], check=True)


def make_marker_video(path: Path, w=64, h=48, rate="10", seconds=1, display_rotation: int | None = None) -> Path:
    x, y, bw, bh = MARK
    src = f"color=black:s={w}x{h}:r={rate}:d={seconds},drawbox=x={x}:y={y}:w={bw}:h={bh}:color=red:t=fill"
    target = path if display_rotation is None else path.with_name("unrotated_" + path.name)
    ff("-f", "lavfi", "-i", src, "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", target)
    if display_rotation is not None:
        ff("-display_rotation", display_rotation, "-i", target, "-c", "copy", path)
    return path


def make_index_video(path: Path, rate="25", frames=40, audio: str | None = None, seconds: float | None = None) -> Path:
    """Each frame's brightness encodes its index (Y = 20 + 4*index). `audio`: "tone" (constant
    440 Hz) or "beep" (silence, then 440 Hz from t=1.0 s).

    Y must stay inside video range (16-235): brighter values are clipped when frames pass through
    RGB images on the AI path, which would make different frames look identical."""
    if 20 + 4 * (frames - 1) > 235:
        raise ValueError(f"index videos support at most 54 frames, not {frames}")
    dur = seconds or float(frames / Fraction(rate))
    v = f"nullsrc=s=32x16:r={rate}:d={dur:.6f},geq=lum='20+4*N':cb=128:cr=128"
    args = ["-f", "lavfi", "-i", v]
    if audio == "tone":
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={dur:.6f}"]
    elif audio == "beep":
        args += ["-f", "lavfi", "-i", f"aevalsrc='if(gte(t,1.0),0.8*sin(2*PI*440*t),0)':s=48000:d={dur:.6f}"]
    args += ["-frames:v", str(frames), "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "pcm_s16le" if path.suffix == ".mov" else "aac", "-b:a", "256k"]
    ff(*args, path)
    return path


def probe_streams(path: Path) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,width,height,nb_read_frames,duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    res = {}
    for s in json.loads(out)["streams"]:
        res[s["codec_type"]] = s
    return res


def video_size(path: Path) -> tuple[int, int]:
    v = probe_streams(path)["video"]
    return int(v["width"]), int(v["height"])


def rgb_frames(path: Path) -> tuple[int, int, list[bytes]]:
    w, h = video_size(path)
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    size = w * h * 3
    return w, h, [raw[i:i + size] for i in range(0, len(raw), size)]


def red_bbox(frame: bytes, w: int, h: int) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) exclusive bounds of strongly red pixels."""
    xs, ys = [], []
    for y in range(h):
        row = frame[y * w * 3:(y + 1) * w * 3]
        for x in range(w):
            r, g, b = row[3 * x], row[3 * x + 1], row[3 * x + 2]
            if r > 150 and g < 90 and b < 90:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if xs else None


def frame_indices(path: Path) -> list[int]:
    """Decode an index video and return the source index shown in every frame."""
    w, h = video_size(path)
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         capture_output=True, check=True).stdout
    size = w * h * 3 // 2
    px = (h // 3) * w + w // 3  # a pixel safely inside the frame
    return [round((raw[i + px] - 20) / 4) for i in range(0, len(raw) - size + 1, size)]


def audio_samples(path: Path, rate=48000) -> list[int]:
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


def onset_seconds(samples: list[int], rate=48000, threshold=0.2) -> float | None:
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return None
    lim = peak * threshold
    for i, s in enumerate(samples):
        if abs(s) >= lim:
            return i / rate
    return None


def dominant_frequency(samples: list[int], rate=48000) -> float:
    """Frequency from zero crossings over the middle 80% of the loud part (skips fade-in/padding)."""
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return 0.0
    loud = [i for i, s in enumerate(samples) if abs(s) > peak * 0.2]
    first, last = loud[0], loud[-1]
    margin = (last - first) // 10
    seg = samples[first + margin:last - margin]
    crossings = sum(1 for a, b in zip(seg, seg[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(seg) / rate)
