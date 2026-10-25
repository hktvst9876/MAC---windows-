"""Settings dialog."""

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..media.audio import device_label, input_devices, output_devices

# Settings that require the receiver to be restarted (devices disconnect).
RESTART_KEYS = ("name", "port", "resolution", "max_fps", "h265", "password", "max_sessions",
                "webcast_enabled", "webcast_port", "audio_enabled", "audio_device", "hardware_decoding")


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._video_tab(), "Video")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._recording_tab(), "Recording")
        tabs.addTab(self._network_tab(), "Network")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    @staticmethod
    def _note(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(mid); font-size: 11px;")
        return label

    @staticmethod
    def _select_device(combo, selected):
        index = combo.findData(selected)
        if index < 0 and selected:
            # Keep old plain-name settings and temporarily disconnected devices.
            combo.addItem(device_label(selected), selected)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def _general_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.name = QLineEdit(self.config.name)
        self.name.setMaxLength(50)
        f.addRow("Receiver name:", self.name)
        f.addRow("", self._note("The name shown on your iPhone, iPad or Mac in the Screen Mirroring list."))
        self.password = QLineEdit(self.config.password)
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("No password")
        f.addRow("Password:", self.password)
        f.addRow("", self._note("If set, devices must enter this password before they can mirror."))
        self.max_sessions = QSpinBox(minimum=1, maximum=8, value=self.config.max_sessions)
        f.addRow("Max. devices at once:", self.max_sessions)
        self.fullscreen = QCheckBox("Open mirroring windows in full screen")
        self.fullscreen.setChecked(self.config.start_fullscreen)
        f.addRow("", self.fullscreen)
        self.on_top = QCheckBox("Keep mirroring windows on top of other windows")
        self.on_top.setChecked(self.config.always_on_top)
        f.addRow("", self.on_top)
        return w

    def _video_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.resolution = QComboBox()
        for key, label in (("720p", "720p (1280×720)"), ("1080p", "1080p Full HD (1920×1080)"),
                           ("1440p", "1440p Quad HD (2560×1440)"), ("4k", "4K Ultra HD (3840×2160, H.265)")):
            self.resolution.addItem(label, key)
        self.resolution.setCurrentIndex(max(0, self.resolution.findData(self.config.resolution)))
        f.addRow("Resolution:", self.resolution)
        self.fps = QComboBox()
        self.fps.addItem("30 fps", 30)
        self.fps.addItem("60 fps", 60)
        self.fps.setCurrentIndex(max(0, self.fps.findData(self.config.max_fps)))
        f.addRow("Frame rate:", self.fps)
        self.h265 = QCheckBox("Allow H.265 (HEVC) video")
        self.h265.setChecked(self.config.h265)
        f.addRow("", self.h265)
        f.addRow("", self._note("H.265 is used automatically for 4K. Newer devices support it; it needs a "
                                "fast computer or hardware decoding."))
        self.hw = QComboBox()
        for key, label in (("auto", "Automatic (for H.265)"), ("on", "Always"), ("off", "Never")):
            self.hw.addItem(label, key)
        self.hw.setCurrentIndex(max(0, self.hw.findData(self.config.hardware_decoding)))
        f.addRow("Hardware decoding:", self.hw)
        self.stats = QCheckBox("Show stream statistics in mirroring windows")
        self.stats.setChecked(self.config.show_stats)
        f.addRow("", self.stats)
        return w

    def _audio_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.audio = QCheckBox("Play device audio on this computer")
        self.audio.setChecked(self.config.audio_enabled)
        f.addRow("", self.audio)
        self.audio_device = QComboBox()
        self.audio_device.addItem("System default", "")
        for identifier, label in output_devices():
            self.audio_device.addItem(label, identifier)
        self._select_device(self.audio_device, self.config.audio_device)
        f.addRow("Output device:", self.audio_device)
        return w

    def _folder_row(self, value):
        edit = QLineEdit(value)
        button = QPushButton("Browse…")

        def browse():
            folder = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
            if folder:
                edit.setText(folder)

        button.clicked.connect(browse)
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(button)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        return holder, edit

    def _recording_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        row, self.record_folder = self._folder_row(self.config.record_folder)
        f.addRow("Save recordings to:", row)
        row, self.shot_folder = self._folder_row(self.config.screenshot_folder)
        f.addRow("Save screenshots to:", row)
        self.record_fps = QComboBox()
        for fps in (24, 30, 60):
            self.record_fps.addItem("%d fps" % fps, fps)
        self.record_fps.setCurrentIndex(max(0, self.record_fps.findData(self.config.record_fps)))
        f.addRow("Recording frame rate:", self.record_fps)
        self.rec_device_audio = QCheckBox("Record the device's audio")
        self.rec_device_audio.setChecked(self.config.record_device_audio)
        f.addRow("", self.rec_device_audio)
        self.rec_mic = QCheckBox("Record a voice-over from the microphone")
        self.rec_mic.setChecked(self.config.record_microphone)
        f.addRow("", self.rec_mic)
        self.mic_device = QComboBox()
        self.mic_device.addItem("System default", "")
        for identifier, label in input_devices():
            self.mic_device.addItem(label, identifier)
        self._select_device(self.mic_device, self.config.mic_device)
        f.addRow("Microphone:", self.mic_device)
        return w

    def _network_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.port = QSpinBox(minimum=1024, maximum=65535, value=self.config.port)
        f.addRow("AirPlay port:", self.port)
        f.addRow("", self._note("Default 7100. If the port is busy a free one is used automatically."))
        self.webcast = QCheckBox("Allow casting from a web browser (Android, Chromebook, PCs)")
        self.webcast.setChecked(self.config.webcast_enabled)
        f.addRow("", self.webcast)
        self.webcast_port = QSpinBox(minimum=1024, maximum=65535, value=self.config.webcast_port)
        f.addRow("Browser cast port:", self.webcast_port)
        return w

    def apply(self):
        """Copy values into the config; returns True if a restart is needed."""
        c = self.config
        before = {k: getattr(c, k) for k in RESTART_KEYS}
        c.name = self.name.text().strip() or c.name
        c.password = self.password.text()
        c.max_sessions = self.max_sessions.value()
        c.start_fullscreen = self.fullscreen.isChecked()
        c.always_on_top = self.on_top.isChecked()
        c.resolution = self.resolution.currentData()
        c.max_fps = self.fps.currentData()
        c.h265 = self.h265.isChecked()
        c.hardware_decoding = self.hw.currentData()
        c.show_stats = self.stats.isChecked()
        c.audio_enabled = self.audio.isChecked()
        c.audio_device = self.audio_device.currentData()
        c.record_folder = self.record_folder.text().strip() or c.record_folder
        c.screenshot_folder = self.shot_folder.text().strip() or c.screenshot_folder
        c.record_fps = self.record_fps.currentData()
        c.record_device_audio = self.rec_device_audio.isChecked()
        c.record_microphone = self.rec_mic.isChecked()
        c.mic_device = self.mic_device.currentData()
        c.port = self.port.value()
        c.webcast_enabled = self.webcast.isChecked()
        c.webcast_port = self.webcast_port.value()
        return any(getattr(c, k) != v for k, v in before.items())
