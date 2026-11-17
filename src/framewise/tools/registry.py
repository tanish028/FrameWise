"""Find the external programs FrameWise relies on."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_EXE = ".exe" if os.name == "nt" else ""
UPSCALER_EXE = f"realesrgan-ncnn-vulkan{_EXE}"


@dataclass(frozen=True)
class ToolSet:
    ffmpeg: Path
    ffprobe: Path
    upscaler: Path | None  # None until the AI upscaler has been downloaded

    def require_upscaler(self) -> Path:
        from ..core.errors import ToolError
        if self.upscaler is None:
            raise ToolError("The AI upscaler is not installed. Open Tools → Download tools.")
        return self.upscaler


def _search(root: Path, name: str) -> Path | None:
    if root.exists():
        for p in sorted(root.rglob(name)):
            if p.is_file():
                return p
    return None


def find_ffmpeg(tools_dir: Path) -> tuple[Path, Path] | None:
    """Prefer FrameWise's own copy, then PATH."""
    ff, fp = _search(tools_dir, f"ffmpeg{_EXE}"), _search(tools_dir, f"ffprobe{_EXE}")
    if ff and fp:
        return ff, fp
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return Path(ff), Path(fp)
    return None


def find_upscaler(tools_dir: Path) -> Path | None:
    return _search(tools_dir, UPSCALER_EXE)


def locate(tools_dir: Path) -> ToolSet | None:
    """Return the tool set, or None if ffmpeg (the one hard requirement) is missing."""
    ff = find_ffmpeg(tools_dir)
    if ff is None:
        return None
    return ToolSet(ff[0], ff[1], find_upscaler(tools_dir))


def missing_components(tools_dir: Path) -> list[str]:
    missing = []
    if find_ffmpeg(tools_dir) is None:
        missing.append("ffmpeg")
    if find_upscaler(tools_dir) is None:
        missing.append("upscaler")
    return missing
