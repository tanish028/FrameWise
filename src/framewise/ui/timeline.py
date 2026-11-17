"""Bottom workspace for Video Studio: a single-clip timeline with trim handles and a playhead.

This is the foundation of the Phase 3 multi-track timeline: one track, the clip's full length,
the selected (trimmed) range, and a playhead you can click or drag to seek.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import theme
from .widgets import format_time, muted

HANDLE_W = 8


class TrimTrack(QWidget):
    seek_requested = Signal(float)
    trim_changed = Signal(float, float)   # start, end (seconds) when a handle drag ends

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(56)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.playhead = 0.0
        self._drag: str | None = None

    def set_clip(self, duration: float, start: float, end: float) -> None:
        self.duration, self.start, self.end = max(0.0, duration), start, end
        self.update()

    def set_playhead(self, t: float) -> None:
        self.playhead = t
        self.update()

    def _track(self) -> QRectF:
        return QRectF(12, 14, max(10, self.width() - 24), self.height() - 22)

    def _x(self, t: float) -> float:
        r = self._track()
        return r.x() + (t / self.duration * r.width() if self.duration else 0)

    def _t(self, x: float) -> float:
        r = self._track()
        return min(self.duration, max(0.0, (x - r.x()) / r.width() * self.duration)) if self.duration else 0.0

    def mousePressEvent(self, e) -> None:
        if not self.duration:
            return
        x = e.position().x()
        if abs(x - self._x(self.start)) <= HANDLE_W:
            self._drag = "start"
        elif abs(x - self._x(self.end)) <= HANDLE_W:
            self._drag = "end"
        else:
            self._drag = "seek"
            self.playhead = self._t(x)
            self.seek_requested.emit(self.playhead)
        self.update()

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self._drag == "start":
            self.start = min(self._t(x), self.end - 0.1)
        elif self._drag == "end":
            self.end = max(self._t(x), self.start + 0.1)
        elif self._drag == "seek":
            self.playhead = self._t(x)
            self.seek_requested.emit(self.playhead)
        elif self.duration:
            near = abs(x - self._x(self.start)) <= HANDLE_W or abs(x - self._x(self.end)) <= HANDLE_W
            self.setCursor(Qt.SizeHorCursor if near else Qt.PointingHandCursor)
        self.update()

    def mouseReleaseEvent(self, _e) -> None:
        if self._drag in ("start", "end"):
            self.trim_changed.emit(round(self.start, 3), round(self.end, 3))
        self._drag = None

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._track()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.PANEL_2))
        p.drawRoundedRect(r, 6, 6)
        if not self.duration:
            p.setPen(QColor(theme.SUBTLE))
            p.drawText(r, Qt.AlignCenter, "Select a video, GIF, sequence or audio file")
            p.end()
            return
        sel = QRectF(self._x(self.start), r.y(), self._x(self.end) - self._x(self.start), r.height())
        sel_color = QColor(theme.ACCENT)
        sel_color.setAlpha(70)
        p.setBrush(sel_color)
        p.drawRoundedRect(sel, 4, 4)
        p.setBrush(QColor(theme.ACCENT))
        for t in (self.start, self.end):
            p.drawRoundedRect(QRectF(self._x(t) - HANDLE_W / 2, r.y() - 2, HANDLE_W, r.height() + 4), 3, 3)
        p.setPen(QPen(QColor(theme.MUTED), 1))
        steps = 10
        for i in range(steps + 1):
            x = r.x() + r.width() * i / steps
            p.drawLine(int(x), int(r.y() - 6), int(x), int(r.y() - 2))
        x = self._x(self.playhead)
        p.setPen(QPen(QColor(theme.TEXT), 2))
        p.drawLine(int(x), int(r.y() - 8), int(x), int(r.bottom() + 2))
        p.end()


class TimelinePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.track = TrimTrack()
        self.lbl = muted("", wrap=False)
        note = muted("Single-clip timeline. Drag the blue handles to trim and click to seek. "
                     "Multi-track editing arrives in Phase 3.", wrap=True)
        top = QHBoxLayout()
        top.addWidget(QLabel("Timeline"))
        top.addStretch()
        top.addWidget(self.lbl)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.addLayout(top)
        lay.addWidget(self.track)
        lay.addWidget(note)

    def set_clip(self, duration: float, start: float, end: float) -> None:
        self.track.set_clip(duration, start, end)
        if duration:
            self.lbl.setText(f"In {format_time(start)} · Out {format_time(end)} · Length {format_time(end - start)}")
        else:
            self.lbl.setText("")
