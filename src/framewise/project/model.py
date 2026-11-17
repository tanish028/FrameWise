"""Plain data classes describing a project. Serialisable to and from JSON-friendly dicts."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from ..media.probe import MediaInfo
from ..render.presets import DEFAULT_PRESET, PRESETS
from ..render.scaling import TARGET_CHOICES
from ..tools.encoders import DEFAULT_ENCODER, ENCODERS

QUALITY_RANGE = (10, 35)


@dataclass(frozen=True)
class ClipSettings:
    """What to do with one media item when it is rendered."""
    upscale: bool = True
    preset: str = DEFAULT_PRESET
    target: str = "2x"
    encoder: str = DEFAULT_ENCODER
    quality: int = 18
    output: str = ""  # empty = automatic name next to the source

    def with_changes(self, **changes) -> "ClipSettings":
        return replace(self, **changes).validated()

    def validated(self) -> "ClipSettings":
        """Clamp/repair values so a hand-edited or old project can never crash a render."""
        lo, hi = QUALITY_RANGE
        return replace(
            self,
            preset=self.preset if self.preset in PRESETS else DEFAULT_PRESET,
            target=self.target if self.target in TARGET_CHOICES else "2x",
            encoder=self.encoder if self.encoder in ENCODERS else DEFAULT_ENCODER,
            quality=min(hi, max(lo, int(self.quality))),
            upscale=bool(self.upscale),
            output=str(self.output or ""),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "ClipSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known}).validated()


@dataclass
class MediaItem:
    path: str
    settings: ClipSettings = field(default_factory=ClipSettings)
    info: MediaInfo | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def name(self) -> str:
        return Path(self.path).name

    def exists(self) -> bool:
        return Path(self.path).is_file()


@dataclass
class Project:
    items: list[MediaItem] = field(default_factory=list)

    def find(self, item_id: str) -> MediaItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def index_of(self, item_id: str) -> int:
        for n, i in enumerate(self.items):
            if i.id == item_id:
                return n
        raise KeyError(item_id)

    def to_dict(self, base_dir: Path | None = None) -> dict:
        def rel(p: str) -> str:
            if base_dir is None:
                return p
            try:
                return Path(p).resolve().relative_to(base_dir.resolve()).as_posix()
            except ValueError:
                return p

        return {"items": [{
            "id": i.id,
            "path": rel(i.path),
            "settings": asdict(i.settings),
            "info": i.info.to_dict() if i.info else None,
        } for i in self.items]}

    @classmethod
    def from_dict(cls, d: dict, base_dir: Path | None = None) -> "Project":
        items = []
        for raw in d.get("items") or []:
            p = Path(str(raw["path"]))
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            info = None
            if raw.get("info"):
                try:
                    info = MediaInfo.from_dict(raw["info"])
                except (KeyError, TypeError):
                    info = None  # re-probed later
            items.append(MediaItem(path=str(p), settings=ClipSettings.from_dict(raw.get("settings") or {}),
                                   info=info, id=str(raw.get("id") or uuid.uuid4().hex)))
        return cls(items=items)
