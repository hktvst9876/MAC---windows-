"""Icons drawn with QPainter, so the app needs no image assets."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

ACCENT = QColor("#2f80ed")
RED = QColor("#e5484d")


def _canvas(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def app_pixmap(size=256) -> QPixmap:
    pm, p = _canvas(size)
    s = size / 256.0
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QColor("#4facfe"))
    grad.setColorAt(1, QColor("#1f5fd1"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 52 * s, 52 * s)
    # A screen with a "cast" wedge underneath.
    pen = QPen(QColor("white"), 16 * s)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(84 * s, 172 * s)
    path.lineTo(60 * s, 172 * s)
    path.quadTo(48 * s, 172 * s, 48 * s, 160 * s)
    path.lineTo(48 * s, 72 * s)
    path.quadTo(48 * s, 60 * s, 60 * s, 60 * s)
    path.lineTo(196 * s, 60 * s)
    path.quadTo(208 * s, 60 * s, 208 * s, 72 * s)
    path.lineTo(208 * s, 160 * s)
    path.quadTo(208 * s, 172 * s, 196 * s, 172 * s)
    path.lineTo(172 * s, 172 * s)
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("white"))
    p.drawPolygon(QPolygonF([QPointF(128 * s, 132 * s), QPointF(176 * s, 204 * s), QPointF(80 * s, 204 * s)]))
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.addPixmap(app_pixmap(size))
    return icon


def _glyph(draw, color=QColor("#e8e8e8"), size=64) -> QIcon:
    pm, p = _canvas(size)
    pen = QPen(color, size * 0.08)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    draw(p, size / 64.0, color)
    p.end()
    return QIcon(pm)


def record_icon(active=False) -> QIcon:
    def draw(p, s, color):
        p.setPen(QPen(QColor("#e8e8e8"), 4 * s))
        p.setBrush(RED)
        if active:
            p.drawRoundedRect(QRectF(18 * s, 18 * s, 28 * s, 28 * s), 4 * s, 4 * s)
        else:
            p.drawEllipse(QRectF(14 * s, 14 * s, 36 * s, 36 * s))
    return _glyph(draw)


def camera_icon() -> QIcon:
    def draw(p, s, color):
        p.drawRoundedRect(QRectF(8 * s, 18 * s, 48 * s, 34 * s), 6 * s, 6 * s)
        p.drawEllipse(QRectF(22 * s, 24 * s, 20 * s, 20 * s))
        p.drawLine(QPointF(22 * s, 18 * s), QPointF(26 * s, 11 * s))
        p.drawLine(QPointF(26 * s, 11 * s), QPointF(38 * s, 11 * s))
        p.drawLine(QPointF(38 * s, 11 * s), QPointF(42 * s, 18 * s))
    return _glyph(draw)


def fullscreen_icon() -> QIcon:
    def draw(p, s, color):
        for (x, y, dx, dy) in ((10, 10, 1, 1), (54, 10, -1, 1), (10, 54, 1, -1), (54, 54, -1, -1)):
            p.drawLine(QPointF(x * s, y * s), QPointF((x + 14 * dx) * s, y * s))
            p.drawLine(QPointF(x * s, y * s), QPointF(x * s, (y + 14 * dy) * s))
    return _glyph(draw)


def pin_icon() -> QIcon:
    def draw(p, s, color):
        p.drawLine(QPointF(32 * s, 40 * s), QPointF(32 * s, 58 * s))
        p.drawLine(QPointF(16 * s, 40 * s), QPointF(48 * s, 40 * s))
        p.drawPolygon(QPolygonF([QPointF(22 * s, 40 * s), QPointF(25 * s, 10 * s),
                                 QPointF(39 * s, 10 * s), QPointF(42 * s, 40 * s)]))
    return _glyph(draw)


def speaker_icon(muted=False) -> QIcon:
    def draw(p, s, color):
        p.drawPolygon(QPolygonF([QPointF(10 * s, 24 * s), QPointF(22 * s, 24 * s), QPointF(34 * s, 12 * s),
                                 QPointF(34 * s, 52 * s), QPointF(22 * s, 40 * s), QPointF(10 * s, 40 * s)]))
        if muted:
            p.drawLine(QPointF(42 * s, 24 * s), QPointF(56 * s, 40 * s))
            p.drawLine(QPointF(56 * s, 24 * s), QPointF(42 * s, 40 * s))
        else:
            p.drawArc(QRectF(30 * s, 20 * s, 16 * s, 24 * s), -60 * 16, 120 * 16)
            p.drawArc(QRectF(30 * s, 12 * s, 26 * s, 40 * s), -60 * 16, 120 * 16)
    return _glyph(draw)


def info_icon() -> QIcon:
    def draw(p, s, color):
        p.drawEllipse(QRectF(8 * s, 8 * s, 48 * s, 48 * s))
        p.drawLine(QPointF(32 * s, 28 * s), QPointF(32 * s, 44 * s))
        p.drawPoint(QPointF(32 * s, 19 * s))
    return _glyph(draw)


def close_icon() -> QIcon:
    def draw(p, s, color):
        p.drawLine(QPointF(16 * s, 16 * s), QPointF(48 * s, 48 * s))
        p.drawLine(QPointF(48 * s, 16 * s), QPointF(16 * s, 48 * s))
    return _glyph(draw, QColor("#ff8a8a"))


def device_pixmap(model: str, size=48) -> QPixmap:
    """A simple phone / tablet / computer silhouette for the device list."""
    pm, p = _canvas(size)
    s = size / 48.0
    p.setPen(QPen(ACCENT, 3 * s))
    p.setBrush(QColor(47, 128, 237, 40))
    m = (model or "").lower()
    if "ipad" in m:
        p.drawRoundedRect(QRectF(9 * s, 5 * s, 30 * s, 38 * s), 4 * s, 4 * s)
    elif "mac" in m or "browser" in m:
        p.drawRoundedRect(QRectF(6 * s, 9 * s, 36 * s, 24 * s), 3 * s, 3 * s)
        p.drawLine(QPointF(3 * s, 39 * s), QPointF(45 * s, 39 * s))
    else:
        p.drawRoundedRect(QRectF(14 * s, 4 * s, 20 * s, 40 * s), 5 * s, 5 * s)
        p.drawLine(QPointF(21 * s, 39 * s), QPointF(27 * s, 39 * s))
    p.end()
    return pm
