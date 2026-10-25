"""Application-side state for one connected device.

:class:`DeviceSession` implements the AirPlay :class:`SessionHandler`
callbacks (called on the network thread) and exposes Qt signals plus actions
(screenshot, record, disconnect) for the user interface.
"""

import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..airplay.server import ClientInfo, SessionHandler
from ..media.audio import AudioDecoder, AudioPlayer, db_to_gain
from ..media.recorder import Recorder
from ..media.video import VideoDecoder, frame_to_png

log = logging.getLogger(__name__)


def _safe_filename(text: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in text).strip()
    return keep or "device"


class SessionSignals(QObject):
    video_format = Signal(str, int, int)
    frame = Signal()  # a new display frame is ready: call DeviceSession.take_frame()
    paused = Signal(bool)
    video_active = Signal(bool)
    audio_active = Signal(bool)
    volume = Signal(float)
    metadata = Signal(dict)
    cover_art = Signal(bytes)
    progress = Signal(int, int, int)
    recording = Signal(bool, str)  # active, file path or error message
    stopped = Signal()


class DeviceSession(SessionHandler):
    def __init__(self, controller, airplay_session=None):
        self.id = uuid.uuid4().hex
        self.controller = controller
        self.airplay = airplay_session
        self.config = controller.config
        self.signals = SessionSignals()
        self.info = ClientInfo()
        self.codec = None
        self.width = 0
        self.height = 0
        self.has_video = False
        self.has_audio = False
        self.audio_codec = None
        self.paused = False
        self.muted = False
        self.volume_db = 0.0
        self.metadata_items = {}
        self.connected_at = time.time()
        self.recorder = None
        self._recording_lock = threading.Lock()
        self.decoder = VideoDecoder(on_display=self._on_display, hardware=self.config.hardware_decoding)
        self._audio_decoder = None
        self._player = None
        self._ended = False
        self._frame_lock = threading.Lock()
        self._pending_frame = None

    # -- network-thread callbacks ----------------------------------------

    def started(self, info: ClientInfo):
        self.info = info
        self.controller.session_started(self)

    def video_started(self):
        self.has_video = True
        self.signals.video_active.emit(True)

    def video_format(self, codec, config, width, height):
        self.codec, self.width, self.height = codec, width, height
        self.decoder.set_codec(codec, config)
        self.signals.video_format.emit(codec, width, height)

    def video_packet(self, data, timestamp_ns, keyframe):
        return self.decoder.push_async(data, keyframe=keyframe)

    def video_paused(self, paused):
        self.paused = paused
        self.signals.paused.emit(paused)

    def video_stopped(self):
        self.has_video = False
        self.decoder.reset()
        self.signals.video_active.emit(False)

    def audio_started(self, codec, spf):
        self.audio_codec = codec
        try:
            self._audio_decoder = AudioDecoder(codec)
        except Exception:
            log.exception("cannot create %s audio decoder", codec)
            self._audio_decoder = None
        self._ensure_player()
        self.has_audio = True
        self.signals.audio_active.emit(True)

    def _ensure_player(self):
        if self.config.audio_enabled and self._player is None:
            self._player = AudioPlayer(device=self.config.audio_device or None)
            self._player.gain = db_to_gain(self.volume_db)
            self._player.muted = self.muted

    def audio_frame(self, payload, rtp_timestamp):
        if self._audio_decoder is None:
            return
        pcm = self._audio_decoder.decode(payload)
        if pcm is not None:
            self.deliver_pcm(pcm)

    def deliver_pcm(self, pcm):
        """Play and record int16 stereo 44.1 kHz PCM."""
        player = self._player
        if player is not None:
            player.write(pcm)
        recorder = self.recorder
        if recorder is not None:
            recorder.feed_audio(pcm)

    def audio_flush(self):
        if self._player is not None:
            self._player.flush()

    def audio_stopped(self):
        self.has_audio = False
        if self._player is not None:
            self._player.close()
            self._player = None
        self.signals.audio_active.emit(False)

    def volume(self, db):
        self.volume_db = db
        if self._player is not None:
            self._player.set_volume_db(db)
        self.signals.volume.emit(db)

    def metadata(self, items):
        self.metadata_items = dict(items)
        self.signals.metadata.emit(dict(items))

    def cover_art(self, data, mime):
        self.signals.cover_art.emit(bytes(data))

    def progress(self, start, current, end):
        self.signals.progress.emit(start, current, end)

    def stopped(self):
        with self._recording_lock:
            if self._ended:
                return
            self._ended = True
            recording = self.recorder is not None
        if recording:
            # Finalizing the MP4 can take a moment; keep the network thread free.
            threading.Thread(target=self.stop_recording, name="finish-recording").start()
        self.decoder.stop()
        if self._player is not None:
            self._player.close()
            self._player = None
        self.signals.stopped.emit()
        self.controller.session_ended(self)

    # -- decoding --------------------------------------------------------

    def _on_display(self, image, width, height):
        # Keep only the newest frame so a busy UI never builds up a backlog.
        with self._frame_lock:
            notify = self._pending_frame is None
            self._pending_frame = (image, width, height)
        if notify:
            self.signals.frame.emit()

    def take_frame(self):
        with self._frame_lock:
            frame, self._pending_frame = self._pending_frame, None
        return frame

    def set_display_size(self, width, height):
        self.decoder.display_size = (int(width), int(height)) if width > 0 and height > 0 else None

    @property
    def fps(self) -> float:
        return self.decoder.fps

    # -- user actions (UI thread) ----------------------------------------

    @property
    def display_name(self) -> str:
        return self.info.name or "AirPlay device"

    def set_muted(self, muted: bool):
        self.muted = muted
        if self._player is not None:
            self._player.muted = muted

    def _stamp(self):
        return datetime.now().strftime("%Y-%m-%d %H.%M.%S")

    def _reserve_capture(self, folder, extension):
        """Claim a new filename even when captures share a name and timestamp."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stem = "%s %s" % (_safe_filename(self.display_name), self._stamp())
        number = 1
        while True:
            suffix = "" if number == 1 else " (%d)" % number
            path = folder / (stem + suffix + extension)
            try:
                return path, path.open("xb")
            except FileExistsError:
                number += 1

    def screenshot(self, folder=None) -> Path:
        frame = self.decoder.latest_frame
        if frame is None:
            raise RuntimeError("There is no picture to capture yet.")
        image = frame_to_png(frame)
        path, output = self._reserve_capture(folder or self.config.screenshot_folder, ".png")
        with output:
            output.write(image)
        log.info("screenshot saved: %s", path)
        return path

    @property
    def is_recording(self) -> bool:
        return self.recorder is not None and self.recorder.running

    def start_recording(self, folder=None) -> Path:
        with self._recording_lock:
            if self._ended:
                raise RuntimeError("The device has disconnected.")
            if self.is_recording:
                return Path(self.recorder.path)
        path, output = self._reserve_capture(folder or self.config.record_folder, ".mp4")
        output.close()
        recorder = None
        try:
            recorder = Recorder(path, lambda: self.decoder.latest_frame, fps=self.config.record_fps,
                                device_audio=self.config.record_device_audio,
                                microphone=self.config.record_microphone,
                                mic_device=self.config.mic_device or None)
            recorder.start()
            with self._recording_lock:
                if self._ended:
                    raise RuntimeError("The device disconnected while recording was starting.")
                self.recorder = recorder
        except Exception:
            if recorder is not None:
                recorder.stop()
            # This path was created exclusively by us; existing captures were skipped.
            try:
                if path.stat().st_size == 0:
                    path.unlink()
            except OSError:
                pass
            raise
        self.signals.recording.emit(True, str(path))
        return path

    def stop_recording(self):
        with self._recording_lock:
            recorder, self.recorder = self.recorder, None
        if recorder is None:
            return None
        recorder.stop()
        if recorder.error:
            self.signals.recording.emit(False, "Recording failed: " + recorder.error)
        else:
            self.signals.recording.emit(False, recorder.path)
        return recorder.path

    def disconnect(self):
        self.controller.close_session(self)
