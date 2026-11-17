"""Real-ESRGAN (ncnn-vulkan) runner. Works on any Vulkan GPU: NVIDIA, AMD or Intel."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..core.errors import ToolError
from ..core.proc import run_cancellable


def model_available(exe: Path, model: str, scale: int) -> bool:
    models = exe.parent / "models"
    return (models / f"{model}-x{scale}.param").exists() or (models / f"{model}.param").exists()


def build_command(exe: Path, in_dir: Path, out_dir: Path, model: str, scale: int,
                  tile: int = 0, gpu: str | None = None) -> list[str]:
    cmd = [str(exe), "-i", str(in_dir), "-o", str(out_dir), "-n", model, "-s", str(scale),
           "-f", "png", "-m", str(exe.parent / "models"), "-t", str(tile)]
    if gpu not in (None, "", "auto"):
        cmd += ["-g", str(gpu)]
    return cmd


def upscale_dir(exe: Path, in_dir: Path, out_dir: Path, *, model: str, scale: int, expected: int,
                tile: int = 0, gpu: str | None = None, cancel: threading.Event | None = None,
                on_frames: Callable[[int], None] | None = None) -> None:
    if not model_available(exe, model, scale):
        raise ToolError(f"AI model '{model}' (x{scale}) was not found next to the upscaler. "
                        "Re-download tools from the Tools menu.")
    out_dir.mkdir(parents=True, exist_ok=True)

    def tick() -> None:
        if on_frames:
            on_frames(min(expected, sum(1 for _ in out_dir.glob("*.png"))))

    run_cancellable(build_command(exe, in_dir, out_dir, model, scale, tile, gpu),
                    cancel=cancel, on_tick=tick, cwd=exe.parent)
    produced = sum(1 for _ in out_dir.glob("*.png"))
    if produced != expected:
        raise ToolError(f"The upscaler produced {produced} of {expected} frames. If your GPU ran out "
                        "of memory, lower the tile size in Settings.")
    if on_frames:
        on_frames(expected)
