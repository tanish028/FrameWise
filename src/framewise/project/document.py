"""The open project: data + undo history + file path. All edits go through here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..media.probe import MediaInfo
from .io import load_project, save_project
from .edit import Edit
from .model import MediaItem, Project
from .undo import UndoStack

# Events: ("reset", None) | ("added", id) | ("removed", id) | ("edit", id) | ("info", id) | ("state", None)
Listener = Callable[[str, object], None]


@dataclass
class _AddItems:
    doc: "ProjectDocument"
    items: list[MediaItem]
    text: str = "Import media"

    def do(self) -> None:
        for it in self.items:
            self.doc.project.items.append(it)
            self.doc._emit("added", it.id)

    def undo(self) -> None:
        for it in reversed(self.items):
            self.doc.project.items.remove(it)
            self.doc._emit("removed", it.id)


@dataclass
class _RemoveItems:
    doc: "ProjectDocument"
    ids: list[str]
    text: str = "Remove media"
    _saved: list[tuple[int, MediaItem]] | None = None

    def do(self) -> None:
        items = self.doc.project.items
        self._saved = sorted(((items.index(i), i) for i in items if i.id in self.ids), key=lambda t: t[0])
        for _, it in reversed(self._saved):
            items.remove(it)
            self.doc._emit("removed", it.id)

    def undo(self) -> None:
        for idx, it in self._saved or []:
            self.doc.project.items.insert(idx, it)
            self.doc._emit("added", it.id)


@dataclass
class _ChangeEdit:
    doc: "ProjectDocument"
    item_id: str
    old: Edit
    new: Edit
    text: str = "Change edit"

    def _apply(self, e: Edit) -> None:
        item = self.doc.project.find(self.item_id)
        if item is not None:
            item.edit = e
            self.doc._emit("edit", self.item_id)

    def do(self) -> None:
        self._apply(self.new)

    def undo(self) -> None:
        self._apply(self.old)


class ProjectDocument:
    def __init__(self) -> None:
        self.project = Project()
        self.path: Path | None = None
        self.undo = UndoStack()
        self._listeners: list[Listener] = []
        self.undo.subscribe(lambda: self._emit("state", None))

    # -- observation ------------------------------------------------------
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def _emit(self, event: str, payload: object) -> None:
        for fn in list(self._listeners):
            fn(event, payload)

    @property
    def dirty(self) -> bool:
        return not self.undo.is_clean()

    @property
    def title(self) -> str:
        return self.path.stem if self.path else "Untitled"

    # -- file operations --------------------------------------------------
    def new(self) -> None:
        self.project = Project()
        self.path = None
        self.undo.clear()
        self._emit("reset", None)

    def open(self, path: Path) -> None:
        project = load_project(path)  # raises ProjectError before touching current state
        self.project, self.path = project, Path(path)
        self.undo.clear()
        self._emit("reset", None)

    def save(self, path: Path | None = None) -> None:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("No path to save to")
        save_project(self.project, target)
        self.path = target
        self.undo.set_clean()

    # -- edits (undoable) -------------------------------------------------
    def add_media(self, paths: list[str], defaults: Edit | None = None, sequences: dict | None = None,
                  defaults_for=None) -> list[MediaItem]:
        """Import files (one undo step). `sequences` maps a first-frame path to its SequenceInfo;
        `defaults_for(path)` may return per-file default edits (e.g. images without AI upscaling)."""
        sequences = sequences or {}
        existing = {str(Path(i.path).resolve()) for i in self.project.items}
        new_items = []
        for p in paths:
            key = str(Path(p).resolve())
            if key not in existing:
                existing.add(key)
                edit = defaults_for(p) if defaults_for else None
                new_items.append(MediaItem(path=str(Path(p)), edit=edit or defaults or Edit(),
                                           sequence=sequences.get(p)))
        if new_items:
            self.undo.push(_AddItems(self, new_items,
                                     "Import media" if len(new_items) > 1 else f"Import {new_items[0].name}"))
        return new_items

    def remove_media(self, ids: list[str]) -> None:
        ids = [i for i in ids if self.project.find(i)]
        if ids:
            self.undo.push(_RemoveItems(self, ids))

    def update_edit(self, item_id: str, section: str, text: str | None = None, **changes) -> None:
        """Change fields of one edit section as a single undoable step (no-op changes are not recorded)."""
        item = self.project.find(item_id)
        if item is None:
            return
        self.set_edit(item_id, item.edit.with_section(section, **changes), text or f"Change {section}")

    def reset_section(self, item_id: str, section: str) -> None:
        item = self.project.find(item_id)
        if item is not None:
            self.set_edit(item_id, item.edit.reset_section(section), f"Reset {section}")

    def set_edit(self, item_id: str, edit: Edit, text: str = "Change edit") -> None:
        item = self.project.find(item_id)
        if item is None:
            return
        new = edit.validated()
        if new != item.edit:
            self.undo.push(_ChangeEdit(self, item_id, item.edit, new, text))

    # -- not undoable: cached facts about files ----------------------------
    def set_info(self, item_id: str, info: MediaInfo) -> None:
        item = self.project.find(item_id)
        if item is not None and item.info != info:
            item.info = info
            self._emit("info", item_id)
