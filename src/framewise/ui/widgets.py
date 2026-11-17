"""Reusable Midnight Studio components."""
from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QSlider, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .icons import icon


class Section(QWidget):
    """A collapsible inspector section with an icon and title."""
    toggled = Signal(bool)

    def __init__(self, title: str, icon_name: str = "", expanded: bool = True, ai: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.header = QToolButton()
        self.header.setObjectName("sectionHeader")
        self.header.setText(f"  {title}")
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._icon_name, self._ai = icon_name, ai
        self.header.toggled.connect(self.set_expanded)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 0, 4, theme.SPACE)
        self.body_layout.setSpacing(6)
        line = QFrame()
        line.setObjectName("sectionLine")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.body)
        lay.addWidget(line)
        self.set_expanded(expanded)

    def set_expanded(self, on: bool) -> None:
        color = theme.AI if self._ai else theme.TEXT
        name = "chevron-down" if on else "chevron-right"
        self.header.setIcon(icon(self._icon_name or name, color))
        self.header.setIconSize(QSize(16, 16))
        self.body.setVisible(on)
        if self.header.isChecked() != on:
            self.header.setChecked(on)
        self.toggled.emit(on)

    def add(self, w: QWidget) -> QWidget:
        self.body_layout.addWidget(w)
        return w

    def add_row(self, *widgets: QWidget, stretch_last: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        for i, w in enumerate(widgets):
            row.addWidget(w, 1 if (stretch_last and i == len(widgets) - 1) else 0)
        if not stretch_last:
            row.addStretch()
        self.body_layout.addLayout(row)
        return row


class SliderRow(QWidget):
    """Label · slider · numeric value · reset. Emits `previewed` while dragging and `committed`
    once per gesture (release, keyboard step or typed value), so history stays meaningful."""
    previewed = Signal(float)
    committed = Signal(float)

    def __init__(self, label: str, lo: float, hi: float, default: float, step: float = 1.0, unit: str = "",
                 tip: str = "", ai: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lo, self.hi, self.default, self.step = lo, hi, default, step
        self._scale = 1 / step
        self._loading = False
        self.label = QLabel(label)
        self.label.setMinimumWidth(92)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(lo * self._scale), round(hi * self._scale))
        if ai:
            self.slider.setProperty("role", "ai")
        self.spin = QDoubleSpinBox()
        decimals = 0 if step >= 1 else len(f"{step:.4f}".rstrip("0").split(".")[1])
        self.spin.setDecimals(decimals)
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(step)
        self.spin.setSuffix(unit)
        self.spin.setFixedWidth(68)
        # no setButtonSymbols(NoButtons): with a stylesheet it hides the text (Qt quirk); the theme
        # gives the arrow buttons zero width instead
        self.spin.setAlignment(Qt.AlignRight)
        self.reset = QToolButton()
        self.reset.setIcon(icon("refresh-ccw", theme.MUTED))
        self.reset.setIconSize(QSize(13, 13))
        self.reset.setToolTip(f"Reset {label.lower()}")
        for w in (self.label, self.slider, self.spin):
            w.setToolTip(tip)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.label)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        lay.addWidget(self.reset)
        self.slider.valueChanged.connect(self._slider_moved)
        self.slider.sliderReleased.connect(lambda: self._commit(self.slider.value() / self._scale))
        self.spin.editingFinished.connect(lambda: self._commit(self.spin.value()))
        self.reset.clicked.connect(lambda: (self.set_value(default), self._commit(default)))
        self.set_value(default)

    def value(self) -> float:
        return self.spin.value()

    def set_value(self, v: float) -> None:
        self._loading = True
        try:
            self.slider.setValue(round(v * self._scale))
            self.spin.setValue(v)
            self.reset.setEnabled(abs(v - self.default) > 1e-9)
        finally:
            self._loading = False

    def _slider_moved(self, raw: int) -> None:
        if self._loading:
            return
        v = raw / self._scale
        self._loading = True
        self.spin.setValue(v)
        self._loading = False
        if self.slider.isSliderDown():
            self.previewed.emit(v)
        else:  # click on the groove / keyboard: a complete gesture
            self._commit(v)

    def _commit(self, v: float) -> None:
        if self._loading:
            return
        self.reset.setEnabled(abs(v - self.default) > 1e-9)
        self.committed.emit(v)


class Segmented(QWidget):
    """A row of mutually exclusive toggle buttons (workspace switch, compare modes...)."""
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str, str]], parent: QWidget | None = None) -> None:
        """options: (key, label, icon name or '')."""
        super().__init__(parent)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for i, (key, label, icon_name) in enumerate(options):
            b = QToolButton()
            b.setCheckable(True)
            b.setText(label)
            b.setObjectName("segmentFirst" if i == 0 else "segmentLast" if i == len(options) - 1 else "segment")
            if icon_name:
                b.setIcon(icon(icon_name, theme.MUTED, active="#ffffff"))
                b.setIconSize(QSize(15, 15))
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if label else Qt.ToolButtonIconOnly)
            b.clicked.connect(lambda _c=False, k=key: self.changed.emit(k))
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)

    def set_current(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def current(self) -> str | None:
        return next((k for k, b in self.buttons.items() if b.isChecked()), None)


_TIME = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d*)?)\s*$")


def parse_time(text: str) -> float | None:
    """'83.5', '1:23.5' or '0:01:23.500' → seconds; None if not a time."""
    m = _TIME.match(text)
    if not m:
        return None
    a, b, c = m.groups()
    parts = [p for p in (a, b) if p is not None]
    seconds = float(c)
    if len(parts) == 2:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + seconds
    if len(parts) == 1:
        return int(parts[0]) * 60 + seconds
    return seconds


def format_time(seconds: float, precise: bool = True) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    sec = f"{s:06.3f}" if precise else f"{int(s):02d}"
    return f"{int(h)}:{int(m):02d}:{sec}" if h >= 1 else f"{int(m)}:{sec}"


class TimeEdit(QLineEdit):
    """Precise time input (m:ss.mmm). Emits `committed(seconds)` with a valid, clamped value."""
    committed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(92)
        self.setAlignment(Qt.AlignRight)
        self.setPlaceholderText("m:ss.mmm")
        self._value = 0.0
        self._max: float | None = None
        self.editingFinished.connect(self._finish)

    def set_range_max(self, mx: float | None) -> None:
        self._max = mx

    def set_seconds(self, v: float) -> None:
        self._value = v
        self.setText(format_time(v))

    def seconds(self) -> float:
        return self._value

    def _finish(self) -> None:
        v = parse_time(self.text())
        if v is None:
            self.set_seconds(self._value)
            return
        if self._max is not None:
            v = min(v, self._max)
        changed = abs(v - self._value) > 1e-6
        self.set_seconds(v)
        if changed:
            self.committed.emit(v)


def tool_button(icon_name: str, tip: str, slot: Callable | None = None, *, checkable: bool = False,
                color: str = theme.TEXT, size: int = 16) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(icon_name, color, active=theme.ACCENT))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    if slot is not None:
        b.clicked.connect(slot)
    return b


def muted(text: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(wrap)
    return lbl
