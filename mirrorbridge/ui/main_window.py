"""Main window: receiver status, connection instructions and device list."""

import logging
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QStackedWidget, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

from ..config import APP_NAME, APP_VERSION, config_dir
from . import icons
from .mirror_window import MirrorWindow
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

STYLE = """
#Header { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1f5fd1, stop:1 #4facfe); }
#Header QLabel { color: white; }
#Title { font-size: 22px; font-weight: 700; }
#Subtitle { font-size: 13px; }
#StatusPill { border-radius: 10px; padding: 3px 10px; font-weight: 600; }
#ReceiverName { font-size: 20px; font-weight: 700; }
#Card { border: 1px solid palette(mid); border-radius: 10px; }
#Hint { color: palette(mid); }
#DeviceName { font-size: 14px; font-weight: 600; }
"""


class DeviceRow(QWidget):
    def __init__(self, window, session):
        super().__init__()
        self.window = window
        self.session = session
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        icon = QLabel()
        icon.setPixmap(icons.device_pixmap(session.info.model, 40))
        layout.addWidget(icon)
        text = QVBoxLayout()
        self.name = QLabel(session.display_name, objectName="DeviceName")
        self.detail = QLabel("", objectName="Hint")
        text.addWidget(self.name)
        text.addWidget(self.detail)
        layout.addLayout(text, 1)
        self.btn_show = QPushButton("Show")
        self.btn_shot = QPushButton("Screenshot")
        self.btn_rec = QPushButton("Record")
        self.btn_close = QPushButton("Disconnect")
        for b in (self.btn_show, self.btn_shot, self.btn_rec, self.btn_close):
            layout.addWidget(b)
        self.btn_show.clicked.connect(lambda: window.show_session(session))
        self.btn_shot.clicked.connect(self._screenshot)
        self.btn_rec.clicked.connect(self._record)
        self.btn_close.clicked.connect(session.disconnect)
        session.signals.recording.connect(lambda *_: self.refresh())
        self.refresh()

    def _screenshot(self):
        try:
            path = self.session.screenshot()
            self.window.statusBar().showMessage("Screenshot saved to %s" % path, 6000)
        except Exception as exc:
            QMessageBox.information(self, "Screenshot", str(exc))

    def _record(self):
        if self.session.is_recording:
            self.session.stop_recording()
            return
        try:
            path = self.session.start_recording()
            self.window.statusBar().showMessage("Recording to %s" % path, 6000)
        except Exception as exc:
            QMessageBox.warning(self, "Recording", "Could not start recording:\n%s" % exc)

    def refresh(self):
        s = self.session
        parts = [s.info.model or "", s.info.address]
        if s.has_video and s.width:
            parts.append("%d×%d %s %.0f fps" % (s.width, s.height, (s.codec or "").upper(), s.fps))
        elif s.has_audio:
            parts.append("audio (%s)" % s.audio_codec)
        if s.is_recording:
            parts.append("● recording")
        self.detail.setText("  ·  ".join(p for p in parts if p))
        self.btn_rec.setText("Stop recording" if s.is_recording else "Record")
        self.btn_rec.setEnabled(s.has_video)
        self.btn_shot.setEnabled(s.has_video)


class MainWindow(QMainWindow):
    def __init__(self, controller, config):
        super().__init__()
        self.controller = controller
        self.config = config
        self.mirror_windows = {}
        self.rows = {}
        self._quitting = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.setStyleSheet(STYLE)
        self.resize(760, 560)
        self._build_menu()
        self._build_ui()
        self._build_tray()

        controller.session_added.connect(self._on_session_added)
        controller.session_removed.connect(self._on_session_removed)
        controller.status_changed.connect(self._on_status)
        self._on_status(*controller.status)
        self._timer = QTimer(self, interval=1000, timeout=self._refresh)
        self._timer.start()

    # -- layout ----------------------------------------------------------

    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        act = QAction("&Settings…", self, shortcut=QKeySequence.Preferences, triggered=self.open_settings)
        act.setMenuRole(QAction.PreferencesRole)
        m.addAction(act)
        m.addAction(QAction("Open &Recordings Folder", self,
                            triggered=lambda: self._open_folder(self.config.record_folder)))
        m.addAction(QAction("Open Screens&hots Folder", self,
                            triggered=lambda: self._open_folder(self.config.screenshot_folder)))
        m.addSeparator()
        quit_act = QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.quit)
        quit_act.setMenuRole(QAction.QuitRole)
        m.addAction(quit_act)
        h = mb.addMenu("&Help")
        h.addAction(QAction("&How to Connect", self, triggered=self.show_help))
        h.addAction(QAction("Send a &Test Stream", self, triggered=self.send_test_stream))
        h.addAction(QAction("Open &Log Folder", self, triggered=lambda: self._open_folder(config_dir())))
        about = QAction("&About %s" % APP_NAME, self, triggered=self.show_about)
        about.setMenuRole(QAction.AboutRole)
        h.addAction(about)

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame(objectName="Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 16, 20, 16)
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(56))
        hl.addWidget(logo)
        titles = QVBoxLayout()
        titles.addWidget(QLabel(APP_NAME, objectName="Title"))
        titles.addWidget(QLabel("AirPlay screen mirroring receiver for your computer", objectName="Subtitle"))
        hl.addLayout(titles, 1)
        self.status_pill = QLabel("", objectName="StatusPill")
        hl.addWidget(self.status_pill, alignment=Qt.AlignTop)
        layout.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(12)
        card = QFrame(objectName="Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.addWidget(QLabel("To mirror your screen, open <b>Control Center</b> on your iPhone or iPad "
                            "(or Mac), tap <b>Screen Mirroring</b> and choose:", wordWrap=True))
        self.receiver_name = QLabel("", objectName="ReceiverName", textInteractionFlags=Qt.TextSelectableByMouse)
        cl.addWidget(self.receiver_name)
        self.network_label = QLabel("", objectName="Hint", wordWrap=True,
                                    textInteractionFlags=Qt.TextBrowserInteraction)
        self.network_label.setOpenExternalLinks(True)
        cl.addWidget(self.network_label)
        body.addWidget(card)

        body.addWidget(QLabel("<b>Connected devices</b>"))
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.empty = QLabel("No devices connected yet.\nThey will appear here as soon as they start mirroring.",
                            alignment=Qt.AlignCenter, objectName="Hint")
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.empty)
        self.list_stack.addWidget(self.list)
        body.addWidget(self.list_stack, 1)

        buttons = QHBoxLayout()
        b = QPushButton("Settings…")
        b.clicked.connect(self.open_settings)
        buttons.addWidget(b)
        b = QPushButton("Open recordings")
        b.clicked.connect(lambda: self._open_folder(self.config.record_folder))
        buttons.addWidget(b)
        buttons.addStretch(1)
        b = QPushButton("How to connect")
        b.clicked.connect(self.show_help)
        buttons.addWidget(b)
        body.addLayout(buttons)
        layout.addLayout(body, 1)
        self.setCentralWidget(root)
        self.statusBar()

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(icons.app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction(QAction("Show %s" % APP_NAME, self, triggered=self._raise))
        menu.addAction(QAction("Settings…", self, triggered=self.open_settings))
        menu.addSeparator()
        menu.addAction(QAction("Quit", self, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._raise() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    # -- status ----------------------------------------------------------

    def _on_status(self, message, ok):
        color = "#27ae60" if ok and message == "Ready" else ("#f2994a" if ok else "#e5484d")
        text = "● Ready" if ok and message == "Ready" else message
        self.status_pill.setText(text)
        self.status_pill.setStyleSheet("background: white; color: %s;" % color)
        self.status_pill.setWordWrap(not ok)
        self.status_pill.setMaximumWidth(320)
        self._refresh()

    def _refresh(self):
        self.receiver_name.setText(self.config.name)
        addrs = self.controller.addresses
        port = self.controller.port
        text = "Network: %s  ·  port %s" % (", ".join(addrs) or "not connected", port or "—")
        if self.controller.webcast and addrs:
            url = "https://%s:%d" % (addrs[0], self.controller.webcast.port)
            text += ("<br>Chromebook or another computer? Open <a href='%s'>%s</a> in Chrome or Edge "
                     "on that device to cast its screen." % (url, url))
        text += "<br>Your computer and device must be on the same Wi-Fi network."
        self.network_label.setText(text)
        for row in self.rows.values():
            row.refresh()
        if self.tray:
            n = len(self.rows)
            self.tray.setToolTip("%s — %s" % (APP_NAME, "%d device(s) connected" % n if n else "ready"))

    # -- sessions --------------------------------------------------------

    def _on_session_added(self, session):
        item = QListWidgetItem()
        row = DeviceRow(self, session)
        item.setSizeHint(row.sizeHint())
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        self.rows[session.id] = row
        row.item = item
        self.list_stack.setCurrentWidget(self.list)
        self.show_session(session)
        if self.tray:
            self.tray.showMessage(APP_NAME, "%s connected" % session.display_name, icons.app_icon(), 3000)

    def _on_session_removed(self, session):
        row = self.rows.pop(session.id, None)
        if row is not None:
            self.list.takeItem(self.list.row(row.item))
        if not self.rows:
            self.list_stack.setCurrentWidget(self.empty)
        win = self.mirror_windows.pop(session.id, None)
        if win is not None and win.isVisible():
            win._closing_from_session = True
            win.close()

    def show_session(self, session):
        win = self.mirror_windows.get(session.id)
        if win is None:
            win = MirrorWindow(session, self.config)
            self.mirror_windows[session.id] = win
            if self.config.start_fullscreen:
                win.toggle_fullscreen()
            else:
                win.show()
        else:
            win.show()
        win.raise_()
        win.activateWindow()

    # -- actions ---------------------------------------------------------

    def _raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_folder(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_settings(self):
        dlg = SettingsDialog(self.config, self)
        if not dlg.exec():
            return
        restart = dlg.apply()
        self.config.save()
        for win in self.mirror_windows.values():
            win.video.show_stats = self.config.show_stats
            win.act_stats.setChecked(self.config.show_stats)
        if restart:
            if self.rows and QMessageBox.question(
                    self, "Apply settings",
                    "Applying these settings restarts the receiver and disconnects all devices. Continue?") \
                    != QMessageBox.Yes:
                return
            self.controller.restart()
        self._refresh()

    def show_help(self):
        QMessageBox.information(self, "How to connect", HELP_TEXT % {"name": self.config.name})

    def show_about(self):
        QMessageBox.about(self, "About %s" % APP_NAME, ABOUT_TEXT % {"version": APP_VERSION})

    def send_test_stream(self):
        """Stream a test pattern to ourselves to check video, audio and recording."""
        port = self.controller.port
        if not port:
            QMessageBox.warning(self, APP_NAME, "The receiver is not running.")
            return

        def run():
            import asyncio
            from ..testing.fake_sender import run as run_sender
            try:
                asyncio.run(run_sender("127.0.0.1", port, 15, 1280, 720, 30, self.config.password or None,
                                       name="Test pattern"))
            except Exception as exc:
                log.warning("test stream failed: %s", exc)

        threading.Thread(target=run, name="test-stream", daemon=True).start()
        self.statusBar().showMessage("Sending a 15 second test stream…", 5000)

    def quit(self):
        self._quitting = True
        self.close()

    def closeEvent(self, event):
        if self.rows and not self._quitting:
            answer = QMessageBox.question(self, "Quit %s" % APP_NAME,
                                          "Devices are still connected. Quit and disconnect them?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        for win in list(self.mirror_windows.values()):
            win._closing_from_session = True
            win.close()
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()


HELP_TEXT = """<h3>iPhone or iPad</h3>
<ol><li>Connect to the same Wi-Fi network as this computer.</li>
<li>Open <b>Control Center</b> (swipe down from the top-right corner).</li>
<li>Tap <b>Screen Mirroring</b> and choose <b>%(name)s</b>.</li></ol>
<h3>Mac</h3>
<p>Click <b>Control Center</b> in the menu bar → <b>Screen Mirroring</b> → <b>%(name)s</b>.</p>
<h3>Music and audio only</h3>
<p>Tap the AirPlay icon in the Music app (or any audio app) and choose <b>%(name)s</b>.</p>
<h3>Chromebook, Windows or Linux computers</h3>
<p>Open the browser-cast address (https://…) shown in the main window in Chrome or Edge on that device,
accept the security warning once (the page uses a certificate made by this computer), and press
<b>Start casting</b>.</p>
<h3>Android phones</h3>
<p>Google Cast cannot be received by third-party apps, and phone browsers cannot share their screen. On
Windows, many Android phones can mirror with Miracast ("Smart View", "Screen cast") to Windows' built-in
<i>Wireless Display</i> (Settings → System → Projecting to this PC).</p>
<h3>Not showing up?</h3>
<ul><li>Allow %(name)s through your firewall when asked (Windows: "Private networks").</li>
<li>Some routers block devices from seeing each other ("AP/client isolation", guest networks).</li>
<li>Try restarting Wi-Fi on the phone.</li></ul>"""

ABOUT_TEXT = """<h3>MirrorBridge %(version)s</h3>
<p>An AirPlay screen-mirroring receiver for Windows and macOS: mirror iPhone, iPad and Mac screens,
play AirPlay audio, record video with optional microphone voice-over, and take screenshots.</p>
<p>Free software under the GNU General Public License v3. The AirPlay protocol support is based on the work
of the open-source UxPlay, RPiPlay and shairplay projects.</p>
<p>AirPlay, iPhone, iPad and Mac are trademarks of Apple Inc. This project is not affiliated with Apple.</p>"""
