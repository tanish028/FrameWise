"""Plain data classes describing a project. Serialisable to and from JSON-friendly dicts (format v2)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..media.probe import MediaInfo
from ..media.source import SequenceInfo, Source
from .edit import Edit


@dataclass
class MediaItem:
    path: str
    edit: Edit = field(default_factory=Edit)
    info: MediaInfo | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sequence: SequenceInfo | None = None  # set when this item is a numbered image sequence

    @property
    def name(self) -> str:
        if self.sequence is not None:
            return f"{Path(self.sequence.pattern).name.replace('%%', '%')} ({self.sequence.count} frames)"
        return Path(self.path).name

    @property
    def source(self) -> Source:
        return Source(Path(self.path), self.sequence)

    @property
    def kind(self) -> str | None:
        """Media kind, once known (probed), else guessed from the extension."""
        from ..media.probe import SEQUENCE, kind_for_extension
        if self.sequence is not None:
            return SEQUENCE
        return self.info.kind if self.info else kind_for_extension(Path(self.path))

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
            "edit": i.edit.to_dict(),
            "info": i.info.to_dict() if i.info else None,
            **({"sequence": i.sequence.to_dict()} if i.sequence else {}),
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
            seq = SequenceInfo.from_dict(raw["sequence"]) if isinstance(raw.get("sequence"), dict) else None
            items.append(MediaItem(path=str(p), edit=Edit.from_dict(raw.get("edit")),
                                   info=info, id=str(raw.get("id") or uuid.uuid4().hex), sequence=seq))
        return cls(items=items)
