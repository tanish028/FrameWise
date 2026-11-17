"""RenderSpec: a self-contained description of one render, built from a media item."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..media.probe import AUDIO, IMAGE
from ..media.source import SequenceInfo, Source
from ..project.edit import Edit
from ..project.model import MediaItem

VIDEO_JOB, IMAGE_JOB, AUDIO_JOB = "video", "image", "audio"
IMAGE_EXT = {"png": ".png", "jpeg": ".jpg", "webp": ".webp", "bmp": ".bmp"}


@dataclass(frozen=True)
class RenderSpec:
    input: Path
    output: Path
    edit: Edit
    max_seconds: float | None = None  # set for previews
    segment_frames: int = 240         # AI path: frames per chunk (bounds disk use, enables resume)
    tile: int = 0                     # AI path: 0 = auto; smaller uses less GPU memory
    kind: str = VIDEO_JOB             # video | image | audio
    sequence: SequenceInfo | None = None

    @property
    def source(self) -> Source:
        return Source(self.input, self.sequence)

    # Convenience accessors used by the pipeline, queue and UI
    @property
    def upscale(self) -> bool:
        return self.edit.enhance.upscale

    @property
    def preset(self) -> str:
        return self.edit.enhance.preset

    @property
    def target(self) -> str:
        return self.edit.enhance.target

    @property
    def encoder(self) -> str:
        return self.edit.export.video_codec

    @property
    def quality(self) -> int:
        return self.edit.export.quality


def auto_output_path(src: Path, suffix: str, output_dir: Path | None = None, ext: str = ".mp4") -> Path:
    folder = output_dir if output_dir else src.parent
    stem = src.stem.replace("%", "") or "output"
    return folder / f"{stem}_{suffix}{ext}"


def job_kind(item: MediaItem, audio_only: bool = False) -> str:
    kind = item.kind
    if kind == IMAGE:
        return IMAGE_JOB
    if kind == AUDIO or audio_only:
        return AUDIO_JOB
    return VIDEO_JOB


def output_extension(edit: Edit, kind: str) -> str:
    if kind == IMAGE_JOB:
        return IMAGE_EXT[edit.export.image_format]
    if kind == AUDIO_JOB:
        return "." + edit.export.audio_format
    return "." + edit.export.format


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while (cand := p.with_name(f"{p.stem} ({n}){p.suffix}")).exists():
        n += 1
    return cand


def spec_from_item(item: MediaItem, *, preview: bool = False, output_dir: Path | None = None,
                   preview_seconds: float = 3.0, audio_only: bool = False) -> RenderSpec:
    up, ex = item.edit.enhance, item.edit.export
    kind = job_kind(item, audio_only)
    ext = output_extension(item.edit, kind)
    src = Path(item.path)
    if kind == AUDIO_JOB:
        suffix = "audio"
    else:
        suffix = up.target if up.upscale else ("edit" if kind == IMAGE_JOB else "export")
    suffix += "_preview" if preview else ""
    if ex.output and not preview and kind == VIDEO_JOB:
        out = Path(ex.output)
        if out.suffix.lower() != ext:
            out = out.with_suffix(ext)
    elif ex.output and preview and kind == VIDEO_JOB:
        out = Path(ex.output).with_name(Path(ex.output).stem + "_preview" + ext)
    else:
        base = src if item.sequence is None else src.with_name(item.name.split(" (")[0].replace("%", ""))
        out = auto_output_path(base, suffix, output_dir, ext)
        if not preview:
            out = unique_path(out)
    return RenderSpec(input=src, output=out, edit=item.edit, kind=kind, sequence=item.sequence,
                      max_seconds=preview_seconds if preview and kind != IMAGE_JOB else None)
