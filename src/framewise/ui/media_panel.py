"""Media library: the list of imported videos with thumbnails and key facts."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

from ..media.probe import AUDIO, GIF, IMAGE, SEQUENCE, format_duration
from ..project.model import MediaItem
from . import theme
from .icons import icon

THUMB = QSize(96, 54)
ID_ROLE = Qt.UserRole
KIND_ROLE = Qt.UserRole + 1
KIND_LABEL = {"video": "Video", IMAGE: "Image", AUDIO: "Audio", GIF: "GIF", SEQUENCE: "Sequence"}
KIND_ICON = {"video": "film", IMAGE: "image", AUDIO: "music", GIF: "clapperboard", SEQUENCE: "images"}


def _placeholder(kind: str | None = None) -> QIcon:
    pm = QPixmap(THUMB)
    pm.fill(QColor(theme.PANEL_2))
    if kind:
        from PySide6.QtGui import QPainter
        glyph = icon(KIND_ICON.get(kind, "film"), theme.SUBTLE, 64).pixmap(28, 28)
        painter = QPainter(pm)
        painter.drawPixmap((THUMB.width() - 28) // 2, (THUMB.height() - 28) // 2, glyph)
        painter.end()
    return QIcon(pm)


class MediaPanel(QWidget):
    selection_changed = Signal(object)    # item id or None
    import_requested = Signal()
    remove_requested = Signal(list)       # item ids

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._placeholder = _placeholder()
        title = QLabel("Media")
        title.setObjectName("heading")
        self._filter: set[str] | None = None
        self.list = QListWidget()
        self.list.setIconSize(THUMB)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setWordWrap(False)
        self.list.itemSelectionChanged.connect(self._on_selection)
        self.empty = QLabel("Drop media here\nor click Import")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("muted")
        btn_import = QPushButton("Import…")
        btn_import.setIcon(icon("plus", theme.TEXT))
        btn_import.clicked.connect(self.import_requested)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(lambda: self.remove_requested.emit(self.selected_ids()))
        row = QHBoxLayout()
        row.addWidget(btn_import)
        row.addWidget(self.btn_remove)
        row.addStretch()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(title)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.empty, 1)
        lay.addLayout(row)
        self._update_empty()

    # -- queries ----------------------------------------------------------
    def selected_ids(self) -> list[str]:
        """Selected items in the current workspace (items hidden by the studio filter never count)."""
        return [i.data(ID_ROLE) for i in self.list.selectedItems() if not i.isHidden()]

    def current_id(self) -> str | None:
        ids = self.selected_ids()
        return ids[0] if len(ids) == 1 else None

    def _row(self, item_id: str) -> QListWidgetItem | None:
        for n in range(self.list.count()):
            w = self.list.item(n)
            if w.data(ID_ROLE) == item_id:
                return w
        return None

    # -- updates from the document ---------------------------------------
    def reset(self, items: list[MediaItem]) -> None:
        self.list.clear()
        for it in items:
            self.add_item(it)
        self._update_empty()

    def set_filter(self, kinds: set[str] | None) -> None:
        """Show only these media kinds (None = all). Hidden items are deselected."""
        self._filter = kinds
        for n in range(self.list.count()):
            w = self.list.item(n)
            hide = kinds is not None and w.data(KIND_ROLE) not in kinds
            w.setHidden(hide)
            if hide:
                w.setSelected(False)
        self._update_empty()

    def add_item(self, item: MediaItem, index: int | None = None) -> None:
        w = QListWidgetItem(_placeholder(item.kind), "")
        w.setData(ID_ROLE, item.id)
        if index is None:
            self.list.addItem(w)
        else:
            self.list.insertItem(index, w)
        self.refresh_item(item)
        self._update_empty()

    def remove_item(self, item_id: str) -> None:
        w = self._row(item_id)
        if w is not None:
            self.list.takeItem(self.list.row(w))
        self._update_empty()

    def refresh_item(self, item: MediaItem) -> None:
        w = self._row(item.id)
        if w is None:
            return
        kind = item.kind or "video"
        if w.data(KIND_ROLE) != kind:
            w.setData(KIND_ROLE, kind)
            if item.info is not None and kind == AUDIO:
                w.setIcon(_placeholder(AUDIO))
            if self._filter is not None:
                w.setHidden(kind not in self._filter)
        if not item.exists():
            detail = "⚠ File missing"
        elif item.info is None:
            detail = "Reading…"
        else:
            i = item.info
            if kind == IMAGE:
                detail = i.resolution
            elif kind == AUDIO:
                detail = format_duration(i.duration)
            else:
                detail = f"{i.resolution} · {format_duration(i.duration)} · {i.fps:.3g} fps"
        up = item.edit.enhance
        plan = f"AI upscale {up.target}" if up.upscale and kind != AUDIO else KIND_LABEL.get(kind, "Video")
        label = KIND_LABEL.get(kind, "Video")
        w.setText(f"{item.name}\n{detail}\n{plan}")
        w.setToolTip(f"{label}: {item.path}\n{detail}")

    def set_thumbnail(self, item_id: str, image: Path) -> None:
        w = self._row(item_id)
        pm = QPixmap(str(image))
        if w is not None and not pm.isNull():
            w.setIcon(QIcon(pm.scaled(THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def select(self, item_id: str) -> None:
        w = self._row(item_id)
        if w is not None:
            self.list.clearSelection()
            self.list.setCurrentItem(w)
            w.setSelected(True)

    def _on_selection(self) -> None:
        self.btn_remove.setEnabled(bool(self.selected_ids()))
        self.selection_changed.emit(self.current_id())

    def _update_empty(self) -> None:
        has = any(not self.list.item(n).isHidden() for n in range(self.list.count()))
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        self.btn_remove.setEnabled(bool(self.selected_ids()))
