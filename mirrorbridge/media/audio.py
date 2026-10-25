"""Audio decoding (AAC-ELD, AAC-LC, ALAC, PCM) and playback."""

import collections
import logging
import threading

import av
import numpy as np

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
except (ImportError, OSError) as exc:  # PortAudio missing
    sd = None
    log.warning("audio playback unavailable: %s", exc)

SAMPLE_RATE = 44100

# Decoder configuration per codec, as used by AirPlay senders:
#  AAC-ELD 44.1 kHz stereo, 480 samples/frame (AudioSpecificConfig)
#  AAC-LC 44.1 kHz stereo, 1024 samples/frame
#  ALAC "magic cookie": 352 samples/frame, 16 bit, stereo, 44.1 kHz
_EXTRADATA = {
    "aac-eld": bytes.fromhex("f8e85000"),
    "aac": bytes.fromhex("1210"),
    "alac": bytes.fromhex("00000024616c616300000000000001600010280a0e0200ff00000000000000000000ac44"),
}
_FFMPEG_CODEC = {"aac-eld": "aac", "aac": "aac", "alac": "alac"}


def db_to_gain(db: float) -> float:
    if db <= -144.0:
        return 0.0
    return 10.0 ** (db / 20.0)


class AudioDecoder:
    """Turns AirPlay audio payloads into int16 stereo PCM (shape (n, 2))."""

    def __init__(self, codec: str):
        self.codec = codec
        self._ctx = None
        if codec in _FFMPEG_CODEC:
            self._ctx = av.CodecContext.create(_FFMPEG_CODEC[codec], "r")
            self._ctx.extradata = _EXTRADATA[codec]
            self._ctx.open()
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
        self.errors = 0

    def decode(self, payload: bytes):
        if self.codec == "pcm":
            pcm = np.frombuffer(payload[:len(payload) & ~3], dtype=">i2").astype(np.int16)
            return pcm.reshape(-1, 2)
        if self._ctx is None:
            return None
        try:
            frames = self._ctx.decode(av.Packet(payload))
        except Exception as exc:
            self.errors += 1
            if self.errors < 5:
                log.warning("audio decode error (%s): %s", self.codec, exc)
            return None
        chunks = []
        for frame in frames:
            for out in self._resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1, 2))
        if not chunks:
            return None
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


class AudioPlayer:
    """Plays PCM through the default (or chosen) output device.

    A small jitter buffer absorbs network timing variation: playback starts
    once ``target`` seconds are buffered and the buffer is trimmed if it grows
    past ``maximum`` seconds, which bounds the added latency.
    """

    def __init__(self, device=None, target=0.06, maximum=0.35):
        self.gain = 1.0
        self.muted = False
        self._target = int(target * SAMPLE_RATE)
        self._max = int(maximum * SAMPLE_RATE)
        self._chunks = collections.deque()
        self._offset = 0  # samples already consumed from _chunks[0]
        self._buffered = 0
        self._primed = False
        self._lock = threading.Lock()
        self._stream = None
        self.underruns = 0
        if sd is None:
            return
        try:
            self._stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=2, dtype="float32",
                                           callback=self._callback, latency="low", device=device)
            self._stream.start()
        except Exception as exc:
            log.warning("cannot open audio output: %s", exc)
            self._stream = None

    @property
    def available(self) -> bool:
        return self._stream is not None

    def set_volume_db(self, db: float):
        self.gain = db_to_gain(db)

    def write(self, pcm: np.ndarray):
        if self._stream is None:
            return
        data = pcm.astype(np.float32) * (1.0 / 32768.0)
        with self._lock:
            self._chunks.append(data)
            self._buffered += len(data)
            while self._buffered > self._max and self._chunks:
                first = self._chunks[0]
                excess = self._buffered - self._target
                left = len(first) - self._offset
                if left <= excess:
                    self._chunks.popleft()
                    self._offset = 0
                    self._buffered -= left
                else:
                    self._offset += excess
                    self._buffered -= excess

    def flush(self):
        with self._lock:
            self._chunks.clear()
            self._offset = 0
            self._buffered = 0
            self._primed = False

    def _callback(self, outdata, frames, time_info, status):
        outdata.fill(0.0)
        with self._lock:
            if not self._primed:
                if self._buffered < self._target:
                    return
                self._primed = True
            pos = 0
            while pos < frames and self._chunks:
                first = self._chunks[0]
                take = min(frames - pos, len(first) - self._offset)
                outdata[pos:pos + take] = first[self._offset:self._offset + take]
                pos += take
                self._offset += take
                self._buffered -= take
                if self._offset >= len(first):
                    self._chunks.popleft()
                    self._offset = 0
            if pos < frames:
                self._primed = False
                self.underruns += 1
        gain = 0.0 if self.muted else self.gain
        if gain != 1.0:
            outdata *= gain

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


def output_devices():
    if sd is None:
        return []
    try:
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]
    except Exception:
        return []


def input_devices():
    if sd is None:
        return []
    try:
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception:
        return []
