"""Lucide icons (ISC licence, see icons/LICENSE-lucide.txt), tinted to Midnight Studio colours."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

ICON_DIR = Path(__file__).parent / "icons"


@lru_cache(maxsize=512)
def _pixmap(name: str, color: str, size: int) -> QPixmap:
    path = ICON_DIR / f"{name}.svg"
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    try:
        svg = path.read_text(encoding="utf-8").replace("currentColor", color)
    except OSError:
        return pm  # missing icon: transparent rather than a crash
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pm


def icon(name: str, color: str = theme.TEXT, size: int = 32, *, active: str | None = None,
         disabled: str = theme.SUBTLE) -> QIcon:
    """An icon with normal/disabled (and optional checked/'active') colours."""
    ic = QIcon()
    ic.addPixmap(_pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    ic.addPixmap(_pixmap(name, disabled, size), QIcon.Disabled, QIcon.Off)
    if active:
        ic.addPixmap(_pixmap(name, active, size), QIcon.Normal, QIcon.On)
    return ic


def available() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))
