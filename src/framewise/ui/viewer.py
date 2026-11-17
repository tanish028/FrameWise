"""FrameView: shows processed preview frames with compare modes and an interactive crop overlay."""
from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..project.edit import CropRect
from . import theme

MODES = ("edited", "original", "split", "side")
HANDLE = 7


def ratio_value(name: str) -> Fraction | None:
    if name in ("", "free"):
        return None
    a, b = name.split(":")
    return Fraction(int(a), int(b))


def fit_rect(content: QSize, area: QRect) -> QRectF:
    """Largest rectangle with content's aspect ratio centred in area."""
    if content.width() <= 0 or content.height() <= 0 or area.width() <= 0 or area.height() <= 0:
        return QRectF()
    s = min(area.width() / content.width(), area.height() / content.height())
    w, h = content.width() * s, content.height() * s
    return QRectF(area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h)


def constrain_crop(x: float, y: float, w: float, h: float, frame: tuple[int, int], ratio: Fraction | None,
                   anchor: str = "move") -> CropRect:
    """Clamp a crop to the frame, apply a ratio lock and snap to even pixels (what the export uses)."""
    fw, fh = frame
    w, h = max(2.0, min(w, fw)), max(2.0, min(h, fh))
    if ratio is not None:
        if anchor in ("l", "r"):
            h = w / float(ratio)
        elif anchor in ("t", "b"):
            w = h * float(ratio)
        elif w / h > ratio:
            w = h * float(ratio)
        else:
            h = w / float(ratio)
        if w > fw:
            w, h = fw, fw / float(ratio)
        if h > fh:
            h, w = fh, fh * float(ratio)
    x = min(max(0.0, x), fw - w)
    y = min(max(0.0, y), fh - h)
    return CropRect(int(x), int(y), int(w), int(h)).validated().fitted(fw, fh)


class FrameView(QWidget):
    crop_changed = Signal(object)    # CropRect, emitted once when a drag ends
    split_moved = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)
        self.edited: QPixmap | None = None
        self.original: QPixmap | None = None
        self.edited_size: tuple[int, int] = (0, 0)   # full-resolution size the edited pixmap represents
        self.mode = "edited"
        self.split = 0.5
        self.message = "Select media to preview it"
        self.note = ""
        self.busy = False
        self.crop_mode = False
        self.crop: CropRect | None = None
        self.ratio: Fraction | None = None
        self._drag: tuple[str, QPointF, CropRect] | None = None
        self._dragging_split = False

    # -- state --------------------------------------------------------------------------------
    def clear(self, message: str = "Select media to preview it") -> None:
        self.edited = self.original = None
        self.message, self.note, self.busy = message, "", False
        self.update()

    def set_frames(self, edited: QPixmap | None, original: QPixmap | None, edited_size: tuple[int, int],
                   note: str = "") -> None:
        self.edited, self.original, self.edited_size, self.note = edited, original, edited_size, note
        self.message, self.busy = "", False
        self.update()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in MODES else "edited"
        self.update()

    def set_crop_mode(self, on: bool, crop: CropRect | None, ratio: Fraction | None) -> None:
        self.crop_mode, self.crop, self.ratio = on, crop, ratio
        self.update()

    def has_frame(self) -> bool:
        return self.edited is not None

    def _same_shape(self) -> bool:
        a, b = self.edited, self.original
        if a is None or b is None or a.height() == 0 or b.height() == 0:
            return False
        return abs(a.width() / a.height() - b.width() / b.height()) < 0.01

    # -- geometry --------------------------------------------------------------------------------
    def _area(self) -> QRect:
        return self.rect().adjusted(12, 12, -12, -12 - (22 if self.note else 0))

    def image_rect(self) -> QRectF:
        pm = self.edited if self.mode != "original" else (self.original or self.edited)
        return fit_rect(pm.size(), self._area()) if pm is not None else QRectF()

    def _to_frame(self, p: QPointF) -> QPointF:
        r = self.image_rect()
        fw, fh = self.edited_size
        return QPointF((p.x() - r.x()) * fw / max(1.0, r.width()), (p.y() - r.y()) * fh / max(1.0, r.height()))

    def _crop_widget_rect(self) -> QRectF:
        if self.crop is None:
            return self.image_rect()
        r = self.image_rect()
        fw, fh = self.edited_size
        sx, sy = r.width() / max(1, fw), r.height() / max(1, fh)
        return QRectF(r.x() + self.crop.x * sx, r.y() + self.crop.y * sy, self.crop.w * sx, self.crop.h * sy)

    def _hit(self, p: QPointF) -> str | None:
        c = self._crop_widget_rect()
        near = lambda a, b: abs(a - b) <= HANDLE + 2  # noqa: E731
        left, right, top, bottom = near(p.x(), c.left()), near(p.x(), c.right()), near(p.y(), c.top()), near(p.y(), c.bottom())
        inside_x, inside_y = c.left() - HANDLE <= p.x() <= c.right() + HANDLE, c.top() - HANDLE <= p.y() <= c.bottom() + HANDLE
        if top and left:
            return "tl"
        if top and right:
            return "tr"
        if bottom and left:
            return "bl"
        if bottom and right:
            return "br"
        if left and inside_y:
            return "l"
        if right and inside_y:
            return "r"
        if top and inside_x:
            return "t"
        if bottom and inside_x:
            return "b"
        return "move" if c.contains(p) else None

    # -- mouse ---------------------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        p = e.position()
        if self.crop_mode and self.has_frame():
            fw, fh = self.edited_size
            current = self.crop or CropRect(0, 0, fw, fh)
            part = self._hit(p) or "new"
            if part == "new":
                f = self._to_frame(p)
                current = CropRect(int(f.x()), int(f.y()), 2, 2)
                part = "br"
            self._drag = (part, p, current)
            return
        if self.mode == "split" and self.has_frame():
            self._dragging_split = True
            self._move_split(p)

    def mouseMoveEvent(self, e) -> None:
        p = e.position()
        if self._drag is not None:
            part, start, c0 = self._drag
            f0, f1 = self._to_frame(start), self._to_frame(p)
            dx, dy = f1.x() - f0.x(), f1.y() - f0.y()
            x, y, w, h = c0.x, c0.y, c0.w, c0.h
            if part == "move":
                x, y = x + dx, y + dy
            else:
                if "l" in part:
                    x, w = x + dx, w - dx
                if "r" in part:
                    w = w + dx
                if "t" in part:
                    y, h = y + dy, h - dy
                if "b" in part:
                    h = h + dy
                if w < 0:
                    x, w = x + w, -w
                if h < 0:
                    y, h = y + h, -h
            anchor = part if part in ("l", "r", "t", "b") else "move"
            if part == "move":
                nc = constrain_crop(x, y, c0.w, c0.h, self.edited_size, None)
            else:
                nc = constrain_crop(x, y, w, h, self.edited_size, self.ratio, anchor)
            self.crop = nc
            self.update()
            return
        if self._dragging_split:
            self._move_split(p)
            return
        if self.crop_mode:
            part = self._hit(p)
            cursors = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
                       "bl": Qt.SizeBDiagCursor, "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
                       "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor, "move": Qt.SizeAllCursor}
            self.setCursor(cursors.get(part, Qt.CrossCursor))
        elif self.mode == "split":
            self.setCursor(Qt.SplitHCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag is not None:
            self._drag = None
            if self.crop is not None:
                self.crop_changed.emit(self.crop)
        self._dragging_split = False

    def _move_split(self, p: QPointF) -> None:
        r = self.image_rect()
        if r.width() > 0:
            self.split = min(1.0, max(0.0, (p.x() - r.x()) / r.width()))
            self.split_moved.emit(self.split)
            self.update()

    # -- painting ------------------------------------------------------------------------------------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(theme.VIEWER_BG))
        if self.edited is None:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Rendering preview…" if self.busy else self.message)
            p.end()
            return
        area = self._area()
        mode = self.mode
        if mode == "split" and self.original is not None and not self._same_shape():
            mode = "side"  # different geometry (crop, rotate, resize): a split would misalign
        if mode == "side" and self.original is not None:
            half_l = QRect(area.x(), area.y(), area.width() // 2 - 4, area.height())
            half_r = QRect(area.x() + area.width() // 2 + 4, area.y(), area.width() // 2 - 4, area.height())
            for pm, half, label in ((self.original, half_l, "Original"), (self.edited, half_r, "Edited")):
                r = fit_rect(pm.size(), half)
                p.drawPixmap(r, pm, QRectF(pm.rect()))
                self._label(p, r, label)
        elif mode == "original" and self.original is not None:
            r = fit_rect(self.original.size(), area)
            p.drawPixmap(r, self.original, QRectF(self.original.rect()))
            self._label(p, r, "Original")
        elif mode == "split" and self.original is not None:
            r = fit_rect(self.edited.size(), area)
            p.drawPixmap(r, self.edited, QRectF(self.edited.rect()))
            cut = r.width() * self.split
            p.drawPixmap(QRectF(r.x(), r.y(), cut, r.height()), self.original,
                         QRectF(0, 0, self.original.width() * self.split, self.original.height()))
            p.setPen(QPen(QColor(theme.TEXT), 2))
            p.drawLine(QPointF(r.x() + cut, r.top()), QPointF(r.x() + cut, r.bottom()))
            self._label(p, QRectF(r.x(), r.y(), cut, r.height()), "Original")
            self._label(p, QRectF(r.x() + cut, r.y(), r.width() - cut, r.height()), "Edited")
        else:
            r = fit_rect(self.edited.size(), area)
            p.drawPixmap(r, self.edited, QRectF(self.edited.rect()))
        if self.crop_mode:
            self._paint_crop(p)
        if self.busy:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect().adjusted(0, 4, -10, 0), Qt.AlignTop | Qt.AlignRight, "Updating…")
        if self.note:
            p.setPen(QColor(theme.WARNING))
            p.drawText(self.rect().adjusted(12, 0, -12, -6), Qt.AlignBottom | Qt.AlignHCenter, self.note)
        p.end()

    def _label(self, p: QPainter, r: QRectF, text: str) -> None:
        if r.width() < 60:
            return
        f = QFont(p.font())
        f.setPointSize(8)
        p.setFont(f)
        box = QRectF(r.x() + 8, r.y() + 8, p.fontMetrics().horizontalAdvance(text) + 12, 18)
        p.fillRect(box, QColor(0, 0, 0, 150))
        p.setPen(QColor(theme.TEXT))
        p.drawText(box, Qt.AlignCenter, text)

    def _paint_crop(self, p: QPainter) -> None:
        img = self.image_rect()
        c = self._crop_widget_rect()
        shade = QColor(0, 0, 0, 140)
        p.fillRect(QRectF(img.left(), img.top(), img.width(), c.top() - img.top()), shade)
        p.fillRect(QRectF(img.left(), c.bottom(), img.width(), img.bottom() - c.bottom()), shade)
        p.fillRect(QRectF(img.left(), c.top(), c.left() - img.left(), c.height()), shade)
        p.fillRect(QRectF(c.right(), c.top(), img.right() - c.right(), c.height()), shade)
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        p.drawRect(c)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        for i in (1, 2):
            p.drawLine(QPointF(c.left() + c.width() * i / 3, c.top()), QPointF(c.left() + c.width() * i / 3, c.bottom()))
            p.drawLine(QPointF(c.left(), c.top() + c.height() * i / 3), QPointF(c.right(), c.top() + c.height() * i / 3))
        p.setBrush(QColor(theme.TEXT))
        p.setPen(Qt.NoPen)
        for pt in (c.topLeft(), c.topRight(), c.bottomLeft(), c.bottomRight()):
            p.drawRect(QRectF(pt.x() - HANDLE / 2, pt.y() - HANDLE / 2, HANDLE, HANDLE))
        if self.crop is not None:
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(c.x(), c.bottom() + 4, max(120, c.width()), 18), Qt.AlignLeft,
                       f"{self.crop.w} × {self.crop.h}")
