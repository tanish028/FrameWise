"""Cached poster-frame thumbnails generated with ffmpeg."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.proc import run_cancellable


def cache_key(src: Path, width: int) -> str:
    st = src.stat()
    raw = f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}|{width}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def make_thumbnail(ffmpeg: Path, src: Path, cache: Path, duration: float, width: int = 240) -> Path:
    """Return a JPEG thumbnail of `src` taken ~10% in, creating it if not cached."""
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{cache_key(src, width)}.jpg"
    if dest.exists():
        return dest
    at = max(0.0, min(duration * 0.1, max(0.0, duration - 0.1)))
    tmp = dest.with_suffix(".part.jpg")
    run_cancellable([ffmpeg, "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", src,
                     "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", tmp])
    tmp.replace(dest)
    return dest
