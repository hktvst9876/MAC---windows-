"""Widget that shows the decoded video of one session."""

import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget


class VideoWidget(QWidget):
    double_clicked = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(160, 120)
        self.setMouseTracking(True)
        self._image = None
        self._array = None  # keeps the pixel buffer alive while QImage uses it
        self.message = "Waiting for video…"
        self.recording_since = None
        self.show_stats = False
        self.stats_text = ""
        self._toast = None
        self._toast_until = 0.0
        session.signals.frame.connect(self._on_frame)

    def _on_frame(self):
        frame = self.session.take_frame()
        if frame is not None:
            self.set_frame(*frame)

    def set_frame(self, array, width, height):
        image = QImage(array.data, width, height, width * 4, QImage.Format_RGB32)
        image.setDevicePixelRatio(self.devicePixelRatioF())
        self._image = image
        self._array = array
        if self.message and self.message.startswith("Waiting"):
            self.message = None
        self.update()

    def clear(self, message):
        self._image = None
        self._array = None
        self.message = message
        self.update()

    def toast(self, text, seconds=2.5):
        self._toast = text
        self._toast_until = time.monotonic() + seconds
        self.update()

    def _update_display_size(self):
        dpr = self.devicePixelRatioF()
        self.session.set_display_size(self.width() * dpr, self.height() * dpr)

    def showEvent(self, event):
        super().showEvent(event)
        self._update_display_size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_display_size()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self._image is not None:
            dpr = self._image.devicePixelRatio()
            iw, ih = self._image.width() / dpr, self._image.height() / dpr
            ww, wh = self.width(), self.height()
            if abs(iw - ww) <= 2 or abs(ih - wh) <= 2:
                # Frame already scaled for this widget size: draw 1:1.
                p.drawImage(QPointF((ww - iw) / 2, (wh - ih) / 2), self._image)
            else:
                # Window is being resized; scale until a fitted frame arrives.
                scale = min(ww / iw, wh / ih)
                tw, th = iw * scale, ih * scale
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                p.drawImage(QRectF((ww - tw) / 2, (wh - th) / 2, tw, th), self._image)
        if self.message:
            self._draw_banner(p, self.message)
        if self.recording_since is not None:
            self._draw_recording(p)
        if self.show_stats and self.stats_text:
            self._draw_stats(p)
        if self._toast and time.monotonic() < self._toast_until:
            self._draw_toast(p)
        p.end()

    def _draw_banner(self, p, text):
        font = QFont(self.font())
        font.setPointSizeF(max(11.0, min(20.0, self.width() / 40)))
        p.setFont(font)
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text) + 40
        h = fm.height() + 24
        rect = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 30, 30, 200))
        p.drawRoundedRect(rect, 12, 12)
        p.setPen(QColor(235, 235, 235))
        p.drawText(rect, Qt.AlignCenter, text)

    def _draw_recording(self, p):
        elapsed = int(time.monotonic() - self.recording_since)
        text = "REC  %02d:%02d" % (elapsed // 60, elapsed % 60)
        if elapsed >= 3600:
            text = "REC  %d:%02d:%02d" % (elapsed // 3600, elapsed // 60 % 60, elapsed % 60)
        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        fm = p.fontMetrics()
        rect = QRectF(12, 12, fm.horizontalAdvance(text) + 34, fm.height() + 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(rect, 8, 8)
        if elapsed % 2 == 0:
            p.setBrush(QColor("#e5484d"))
            p.drawEllipse(QRectF(rect.left() + 9, rect.center().y() - 5, 10, 10))
        p.setPen(QColor("white"))
        p.drawText(rect.adjusted(26, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)

    def _draw_stats(self, p):
        font = QFont("monospace")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        p.setFont(font)
        lines = self.stats_text.split("\n")
        fm = p.fontMetrics()
        w = max(fm.horizontalAdvance(line) for line in lines) + 16
        h = fm.height() * len(lines) + 12
        rect = QRectF(self.width() - w - 12, 12, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(rect, 6, 6)
        p.setPen(QPen(QColor(200, 255, 200)))
        p.drawText(rect.adjusted(8, 6, -8, -6), Qt.AlignLeft | Qt.AlignTop, self.stats_text)

    def _draw_toast(self, p):
        font = QFont(self.font())
        font.setPointSize(11)
        p.setFont(font)
        fm = p.fontMetrics()
        text = self._toast
        w = min(self.width() - 24, fm.horizontalAdvance(text) + 32)
        h = fm.height() + 18
        rect = QRectF((self.width() - w) / 2, self.height() - h - 20, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 20, 20, 220))
        p.drawRoundedRect(rect, 10, 10)
        p.setPen(QColor("white"))
        p.drawText(rect, Qt.AlignCenter, fm.elidedText(text, Qt.ElideMiddle, int(w - 20)))
