"""RenderSpec: a self-contained description of one render, built from a media item."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..project.model import MediaItem


@dataclass(frozen=True)
class RenderSpec:
    input: Path
    output: Path
    upscale: bool
    preset: str
    target: str
    encoder: str
    quality: int
    max_seconds: float | None = None  # set for previews
    segment_frames: int = 240         # AI path: frames per chunk (bounds disk use, enables resume)
    tile: int = 0                     # AI path: 0 = auto; smaller uses less GPU memory


def auto_output_path(src: Path, suffix: str, output_dir: Path | None = None) -> Path:
    folder = output_dir if output_dir else src.parent
    return folder / f"{src.stem}_{suffix}.mp4"


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while (cand := p.with_name(f"{p.stem} ({n}){p.suffix}")).exists():
        n += 1
    return cand


def spec_from_item(item: MediaItem, *, preview: bool = False, output_dir: Path | None = None,
                   preview_seconds: float = 3.0) -> RenderSpec:
    s = item.settings
    src = Path(item.path)
    suffix = (s.target if s.upscale else "export") + ("_preview" if preview else "")
    if s.output and not preview:
        out = Path(s.output)
    elif s.output and preview:
        out = Path(s.output).with_name(Path(s.output).stem + "_preview.mp4")
    else:
        out = auto_output_path(src, suffix, output_dir)
        if not preview:
            out = unique_path(out)
    return RenderSpec(input=src, output=out, upscale=s.upscale, preset=s.preset, target=s.target,
                      encoder=s.encoder, quality=s.quality,
                      max_seconds=preview_seconds if preview else None)
