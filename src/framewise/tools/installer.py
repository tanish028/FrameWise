"""Download the external tools on first run."""
from __future__ import annotations

import os
import shutil
import stat
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from ..core.errors import Cancelled, ToolError
from ..core.logging_setup import get_logger

log = get_logger("installer")

ProgressFn = Callable[[str, float], None]  # (message, fraction 0..1)

_UPSCALER_BASE = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
_UPSCALER_ZIPS = {
    "win": "realesrgan-ncnn-vulkan-20220424-windows.zip",
    "linux": "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
    "mac": "realesrgan-ncnn-vulkan-20220424-macos.zip",
}
FFMPEG_WIN_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                  "ffmpeg-master-latest-win64-gpl.zip")
COMPONENT_LABELS = {"ffmpeg": "ffmpeg (video engine)", "upscaler": "Real-ESRGAN (AI upscaler)"}


def _platform() -> str:
    if sys.platform.startswith("win"):
        return "win"
    return "mac" if sys.platform == "darwin" else "linux"


def upscaler_url() -> str:
    return _UPSCALER_BASE + _UPSCALER_ZIPS[_platform()]


def can_auto_install(component: str) -> bool:
    return component == "upscaler" or (component == "ffmpeg" and _platform() == "win")


def download(url: str, dest: Path, progress: ProgressFn | None = None,
             cancel: threading.Event | None = None, label: str = "") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "FrameWise"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while chunk := r.read(1 << 18):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(f"Downloading {label or dest.name}", got / total if total else 0.0)
    except Cancelled:
        part.unlink(missing_ok=True)
        raise
    except OSError as e:
        part.unlink(missing_ok=True)
        raise ToolError(f"Download failed: {e}\n{url}") from e
    os.replace(part, dest)


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract a zip, refusing entries that would escape `dest`."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            target = (dest / member.filename).resolve()
            if target != dest and dest not in target.parents:
                raise ToolError(f"Unsafe path in archive: {member.filename}")
        z.extractall(dest)


def _mark_executable(root: Path) -> None:
    if os.name == "nt":
        return
    for p in root.rglob("*"):
        if p.is_file() and p.name.startswith(("realesrgan", "ffmpeg", "ffprobe")):
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_zip(url: str, target: Path, label: str, progress: ProgressFn | None, cancel) -> None:
    tmp_zip = target.with_name(target.name + ".zip")
    try:
        download(url, tmp_zip, progress, cancel, label)
        if progress:
            progress(f"Unpacking {label}", 1.0)
        staging = target.with_name(target.name + ".new")
        shutil.rmtree(staging, ignore_errors=True)
        safe_extract(tmp_zip, staging)
        _mark_executable(staging)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    except zipfile.BadZipFile as e:
        raise ToolError(f"The download of {label} was corrupt. Please try again.") from e
    finally:
        tmp_zip.unlink(missing_ok=True)


def install(component: str, tools_dir: Path, progress: ProgressFn | None = None,
            cancel: threading.Event | None = None) -> None:
    label = COMPONENT_LABELS.get(component, component)
    if component == "upscaler":
        log.info("Installing upscaler from %s", upscaler_url())
        _install_zip(upscaler_url(), tools_dir / "realesrgan", label, progress, cancel)
    elif component == "ffmpeg":
        if not can_auto_install("ffmpeg"):
            raise ToolError("ffmpeg was not found. Install it with your package manager "
                            "(for example 'sudo apt install ffmpeg' or 'brew install ffmpeg') and restart.")
        log.info("Installing ffmpeg from %s", FFMPEG_WIN_URL)
        _install_zip(FFMPEG_WIN_URL, tools_dir / "ffmpeg", label, progress, cancel)
    else:
        raise ValueError(f"Unknown component {component!r}")
