"""The window that shows one connected device."""

import logging
import time

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence, QPixmap
from PySide6.QtWidgets import (QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QSizePolicy,
                               QStackedWidget, QToolBar, QVBoxLayout, QWidget)

from . import icons
from .video_widget import VideoWidget

log = logging.getLogger(__name__)

TOOLBAR_STYLE = """
QToolBar { background: #1e1f22; border: none; spacing: 4px; padding: 3px; }
QToolButton { color: #e8e8e8; border-radius: 6px; padding: 4px 8px; }
QToolButton:hover { background: #33353a; }
QToolButton:checked { background: #3d4250; }
"""


class NowPlaying(QWidget):
    """Shown for audio-only AirPlay (music) sessions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #111; color: #eee;")
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.art = QLabel(alignment=Qt.AlignCenter)
        self.art.setMinimumSize(200, 200)
        self.art.setPixmap(icons.app_pixmap(200))
        layout.addWidget(self.art, alignment=Qt.AlignCenter)
        self.title = QLabel("AirPlay audio", alignment=Qt.AlignCenter)
        self.title.setStyleSheet("font-size: 20px; font-weight: 600; margin-top: 16px;")
        self.title.setWordWrap(True)
        self.subtitle = QLabel("", alignment=Qt.AlignCenter)
        self.subtitle.setStyleSheet("font-size: 14px; color: #aaa;")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setMaximumWidth(360)
        self.bar.setStyleSheet("QProgressBar { background: #333; border: none; border-radius: 3px; }"
                               "QProgressBar::chunk { background: #2f80ed; border-radius: 3px; }")
        self.bar.hide()
        layout.addWidget(self.bar, alignment=Qt.AlignCenter)
        layout.addStretch(1)
        self._progress = None
        self._timer = QTimer(self, interval=500, timeout=self._tick)
        self._timer.start()

    def set_metadata(self, items):
        self.title.setText(items.get("title") or "AirPlay audio")
        parts = [items.get("artist"), items.get("album")]
        self.subtitle.setText(" — ".join(p for p in parts if p))

    def set_cover(self, data):
        pm = QPixmap()
        if pm.loadFromData(data) and not pm.isNull():
            self.art.setPixmap(pm.scaled(QSize(320, 320), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def set_progress(self, start, current, end):
        if end > start:
            # RTP timestamps at 44.1 kHz; remember when we were told.
            self._progress = (start, current, end, time.monotonic())
            self.bar.show()
            self._tick()

    def _tick(self):
        if not self._progress:
            return
        start, current, end, at = self._progress
        pos = current - start + (time.monotonic() - at) * 44100
        total = end - start
        self.bar.setMaximum(1000)
        self.bar.setValue(int(1000 * min(1.0, max(0.0, pos / total))))


class MirrorWindow(QMainWindow):
    def __init__(self, session, config, parent=None):
        super().__init__(parent)
        self.session = session
        self.config = config
        self._closing_from_session = False
        self.setWindowTitle("%s — MirrorBridge" % session.display_name)
        self.setWindowIcon(icons.app_icon())
        self.setStyleSheet("QMainWindow { background: black; }")

        self.video = VideoWidget(session)
        self.video.show_stats = config.show_stats
        self.video.double_clicked.connect(self.toggle_fullscreen)
        self.video.setContextMenuPolicy(Qt.CustomContextMenu)
        self.video.customContextMenuRequested.connect(self._context_menu)
        self.now_playing = NowPlaying()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.video)
        self.stack.addWidget(self.now_playing)
        self.setCentralWidget(self.stack)

        self._build_actions()
        self._build_toolbar()

        sig = session.signals
        sig.video_format.connect(self._on_format)
        sig.paused.connect(self._on_paused)
        sig.video_active.connect(self._on_video_active)
        sig.audio_active.connect(self._update_mode)
        sig.metadata.connect(self.now_playing.set_metadata)
        sig.cover_art.connect(self.now_playing.set_cover)
        sig.progress.connect(self.now_playing.set_progress)
        sig.recording.connect(self._on_recording)
        sig.stopped.connect(self._on_stopped)

        self._stats_timer = QTimer(self, interval=1000, timeout=self._refresh)
        self._stats_timer.start()
        self._repaint_timer = QTimer(self, interval=500, timeout=self.video.update)
        self._repaint_timer.start()

        self.resize(self._initial_size(session.width or 1280, session.height or 720))
        if config.always_on_top:
            self.act_on_top.setChecked(True)
            self._apply_on_top(True)
        self._update_mode()

    # -- construction ----------------------------------------------------

    def _action(self, text, icon, slot, shortcut=None, checkable=False):
        act = QAction(icon, text, self)
        act.triggered.connect(slot)
        act.setCheckable(checkable)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
            act.setShortcutContext(Qt.WindowShortcut)
        self.addAction(act)
        return act

    def _build_actions(self):
        self.act_record = self._action("Record", icons.record_icon(), self.toggle_recording, "R", True)
        self.act_shot = self._action("Screenshot", icons.camera_icon(), self.take_screenshot, "S")
        self.act_full = self._action("Full screen", icons.fullscreen_icon(), self.toggle_fullscreen, "F", True)
        self.act_on_top = self._action("Keep on top", icons.pin_icon(), self._apply_on_top, "T", True)
        self.act_mute = self._action("Mute", icons.speaker_icon(), self._toggle_mute, "M", True)
        self.act_stats = self._action("Stats", icons.info_icon(), self._toggle_stats, "I", True)
        self.act_stats.setChecked(self.config.show_stats)
        self.act_disconnect = self._action("Disconnect", icons.close_icon(), self.close)
        esc = QAction(self)
        esc.setShortcut(QKeySequence(Qt.Key_Escape))
        esc.triggered.connect(self._exit_fullscreen)
        self.addAction(esc)
        f11 = QAction(self)
        f11.setShortcut(QKeySequence(Qt.Key_F11))
        f11.triggered.connect(self.toggle_fullscreen)
        self.addAction(f11)

    def _build_toolbar(self):
        tb = QToolBar("Controls")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setStyleSheet(TOOLBAR_STYLE)
        for act in (self.act_record, self.act_shot, self.act_full, self.act_on_top, self.act_mute):
            tb.addAction(act)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #9aa0a6; padding-right: 8px;")
        tb.addWidget(self.status_label)
        tb.addAction(self.act_stats)
        tb.addAction(self.act_disconnect)
        self.toolbar = tb
        self.addToolBar(Qt.TopToolBarArea, tb)

    def _initial_size(self, w, h):
        screen = self.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        max_w = int((avail.width() if avail else 1600) * 0.8)
        max_h = int((avail.height() if avail else 900) * 0.8) - 40
        scale = min(max_w / w, max_h / h, 1.0)
        return QSize(max(320, int(w * scale)), max(240, int(h * scale)) + 40)

    def _context_menu(self, pos):
        menu = QMenu(self)
        for act in (self.act_record, self.act_shot, self.act_full, self.act_on_top, self.act_mute,
                    self.act_stats):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self.act_disconnect)
        menu.exec(self.video.mapToGlobal(pos))

    # -- session events --------------------------------------------------

    def _on_format(self, codec, width, height):
        if self.isFullScreen() or self.isMaximized() or not width or not height:
            return
        # Keep the window's area but follow the new aspect ratio (rotation).
        cur = self.video.size()
        if cur.width() and cur.height():
            old_landscape = cur.width() >= cur.height()
            new_landscape = width >= height
            if old_landscape != new_landscape or abs(cur.width() / cur.height() - width / height) > 0.15:
                self.resize(self._initial_size(width, height))

    def _on_paused(self, paused):
        if paused:
            self.video.message = "Paused — the device screen is locked or asleep"
        elif self.video.message and self.video.message.startswith("Paused"):
            self.video.message = None
        self.video.update()

    def _on_video_active(self, active):
        if not active:
            self.video.clear("Screen mirroring stopped")
        self._update_mode()

    def _update_mode(self, *_):
        audio_only = self.session.has_audio and not self.session.has_video
        self.stack.setCurrentWidget(self.now_playing if audio_only else self.video)
        self.act_record.setEnabled(not audio_only)
        self.act_shot.setEnabled(not audio_only)

    def _on_recording(self, active, detail):
        self.act_record.blockSignals(True)
        self.act_record.setChecked(active)
        self.act_record.blockSignals(False)
        self.act_record.setIcon(icons.record_icon(active))
        self.act_record.setText("Stop" if active else "Record")
        if active:
            self.video.recording_since = time.monotonic()
        else:
            self.video.recording_since = None
            if detail.startswith("Recording failed"):
                QMessageBox.warning(self, "Recording", detail)
            elif detail:
                self.video.toast("Saved recording: %s" % detail.replace("\\", "/").rsplit("/", 1)[-1], 4)
        self.video.update()

    def _on_stopped(self):
        self._closing_from_session = True
        self.close()

    # -- actions ---------------------------------------------------------

    def toggle_recording(self):
        if self.session.is_recording:
            self.session.stop_recording()
            return
        try:
            self.session.start_recording()
        except Exception as exc:
            self.act_record.setChecked(False)
            QMessageBox.warning(self, "Recording", "Could not start recording:\n%s" % exc)

    def take_screenshot(self):
        try:
            path = self.session.screenshot()
        except Exception as exc:
            self.video.toast(str(exc))
            return
        self.video.toast("Screenshot saved: %s" % path.name)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self._exit_fullscreen()
        else:
            self.toolbar.hide()
            self.showFullScreen()
            self.act_full.setChecked(True)

    def _exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.toolbar.show()
        self.act_full.setChecked(False)

    def _apply_on_top(self, on):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if visible:
            self.show()

    def _toggle_mute(self, muted):
        self.session.set_muted(muted)
        self.act_mute.setIcon(icons.speaker_icon(muted))
        self.act_mute.setText("Unmute" if muted else "Mute")

    def _toggle_stats(self, on):
        self.video.show_stats = on
        self.video.update()

    def open_folder(self, folder):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _refresh(self):
        s = self.session
        parts = []
        if s.has_video and s.width:
            parts.append("%d×%d %s" % (s.width, s.height, (s.codec or "").upper()))
            parts.append("%.0f fps" % s.fps)
        if s.has_audio:
            parts.append(s.audio_codec.upper() if s.audio_codec else "audio")
        self.status_label.setText("  ·  ".join(parts))
        if self.video.show_stats:
            mirror = s.airplay.mirror if s.airplay is not None else None
            lines = ["%s (%s)" % (s.display_name, s.info.model or "?"),
                     "address  %s" % s.info.address]
            if s.has_video:
                lines.append("video    %s %dx%d  %.1f fps" % (s.codec, s.width, s.height, s.fps))
                if mirror is not None:
                    lines.append("received %.1f MB" % (mirror.bytes / 1e6))
            if s.has_audio:
                audio = s.airplay.audio if s.airplay is not None else None
                lost = audio.lost if audio is not None else 0
                lines.append("audio    %s  volume %.1f dB  lost %d" % (s.audio_codec, s.volume_db, lost))
            self.video.stats_text = "\n".join(lines)

    # -- closing ---------------------------------------------------------

    def closeEvent(self, event):
        if not self._closing_from_session:
            # Closing the window ends mirroring for this device.
            self.session.stop_recording()
            self.session.disconnect()
        self._stats_timer.stop()
        self._repaint_timer.stop()
        super().closeEvent(event)
