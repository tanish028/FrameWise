"""ffmpeg building blocks used by the render pipeline."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from ..core.errors import ToolError
from ..core.proc import run_cancellable
from ..tools.encoders import encoder_args

AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k"]


def _part(path: Path) -> Path:
    return path.with_name(path.stem + ".part" + path.suffix)


def read_progress_frame(progress_file: Path) -> int | None:
    """Last `frame=` value from an ffmpeg `-progress` file."""
    try:
        text = progress_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if line.startswith("frame="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def transcode(ffmpeg: Path, src: Path, out: Path, *, encoder: str, quality: int, has_audio: bool,
              max_seconds: float | None, work_dir: Path, cancel: threading.Event | None = None,
              on_frames: Callable[[int], None] | None = None) -> None:
    """Re-encode src to out in one pass (no AI step)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    prog = work_dir / "progress.txt"
    prog.unlink(missing_ok=True)
    part = _part(out)
    cmd: list = [ffmpeg, "-v", "error", "-y", "-nostats", "-progress", prog, "-i", src]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += ["-map", "0:v:0"] + (["-map", "0:a:0"] if has_audio else [])
    cmd += encoder_args(encoder, quality) + ["-pix_fmt", "yuv420p"]
    cmd += AUDIO_ARGS if has_audio else []
    cmd += ["-movflags", "+faststart", part]

    def tick() -> None:
        n = read_progress_frame(prog)
        if n is not None and on_frames:
            on_frames(n)

    try:
        run_cancellable(cmd, cancel=cancel, on_tick=tick)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


def extract_frames(ffmpeg: Path, src: Path, dest: Path, start_frame: int, count: int,
                   fps: float, cancel: threading.Event | None = None) -> int:
    """Write `count` frames starting at `start_frame` as 00000001.png...; return how many were written."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd: list = [ffmpeg, "-v", "error", "-y"]
    if start_frame > 0:
        # seek half a frame early so the first decoded frame is exactly `start_frame`
        cmd += ["-ss", f"{(start_frame - 0.5) / fps:.6f}"]
    cmd += ["-i", src, "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", str(count),
            "-fps_mode", "passthrough", "-start_number", "1", dest / "%08d.png"]
    run_cancellable(cmd, cancel=cancel)
    return sum(1 for _ in dest.glob("*.png"))


def encode_frames(ffmpeg: Path, frames_dir: Path, out: Path, fps_str: str, *,
                  resize_to: tuple[int, int] | None, encoder: str, quality: int,
                  cancel: threading.Event | None = None) -> None:
    part = _part(out)
    cmd: list = [ffmpeg, "-v", "error", "-y", "-framerate", fps_str, "-i", frames_dir / "%08d.png"]
    if resize_to:
        cmd += ["-vf", f"scale={resize_to[0]}:{resize_to[1]}:flags=lanczos"]
    cmd += encoder_args(encoder, quality) + ["-pix_fmt", "yuv420p", part]
    try:
        run_cancellable(cmd, cancel=cancel)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


def join_segments(ffmpeg: Path, segments: list[Path], src: Path, out: Path, *, with_audio: bool,
                  max_seconds: float | None, work_dir: Path,
                  cancel: threading.Event | None = None) -> None:
    """Concatenate encoded segments losslessly and attach the source's audio."""
    if not segments:
        raise ToolError("No video segments to join")
    list_file = work_dir / "segments.txt"
    list_file.write_text("".join(
        "file '{}'\n".format(p.resolve().as_posix().replace("'", "'\\''")) for p in segments),
        encoding="utf-8")
    part = _part(out)
    cmd: list = [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
    if with_audio:
        cmd += ["-i", src, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *AUDIO_ARGS, "-shortest"]
        if max_seconds:
            cmd += ["-t", f"{max_seconds:.3f}"]
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    cmd += ["-movflags", "+faststart", part]
    try:
        run_cancellable(cmd, cancel=cancel)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)
