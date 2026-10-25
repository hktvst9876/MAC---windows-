"""Recording the mirrored screen to MP4 (H.264 video + AAC audio).

AirPlay senders only send a key frame when a stream starts, so the incoming
video cannot simply be cut at an arbitrary point. Instead the decoded frames
are re-encoded at a constant frame rate (repeating the last frame while the
screen is static, which costs almost nothing). Audio from the device and,
optionally, the computer's microphone (for voice-over) are mixed on the same
wall-clock timeline.
"""

import collections
import logging
import sys
import threading
import time
from fractions import Fraction

import av
import numpy as np

from .audio import SAMPLE_RATE, sd
from .video import fit_size

log = logging.getLogger(__name__)

AUDIO_CHUNK = 1024


def _encoder_candidates():
    if sys.platform == "darwin":
        return ["libx264", "h264_videotoolbox", "h264", "mpeg4"]
    if sys.platform == "win32":
        return ["libx264", "h264_mf", "h264_nvenc", "h264_qsv", "h264_amf", "h264", "mpeg4"]
    return ["libx264", "h264", "h264_vaapi", "mpeg4"]


_ENCODER_OPTIONS = {
    "libx264": {"preset": "veryfast", "crf": "20"},
    "h264": {"preset": "veryfast", "crf": "20"},
    "h264_videotoolbox": {"realtime": "1"},
    "h264_mf": {"rate_control": "quality", "quality": "70"},
    "h264_nvenc": {"preset": "p4"},
}


def pick_video_encoder(width, height, fps):
    """First H.264-capable encoder that opens, as (codec name, pixel format)."""
    for name in _encoder_candidates():
        for pix_fmt in ("yuv420p", "nv12"):
            try:
                ctx = av.CodecContext.create(name, "w")
                ctx.width, ctx.height = width, height
                ctx.pix_fmt = pix_fmt
                ctx.time_base = Fraction(1, fps)
                ctx.framerate = Fraction(fps, 1)
                ctx.options = dict(_ENCODER_OPTIONS.get(name, {}))
                if name not in ("libx264", "h264"):
                    ctx.bit_rate = 8_000_000
                ctx.open()
                del ctx
                return name, pix_fmt
            except Exception:
                continue
    raise RuntimeError("no usable video encoder found")


class _Fifo:
    """Thread-safe PCM FIFO with bounded latency and start-up priming."""

    def __init__(self, prime=0.1, maximum=0.6):
        self._chunks = collections.deque()
        self._size = 0
        self._prime = int(prime * SAMPLE_RATE)
        self._max = int(maximum * SAMPLE_RATE)
        self._primed = False
        self._lock = threading.Lock()

    def put(self, pcm):
        with self._lock:
            self._chunks.append(pcm)
            self._size += len(pcm)
            while self._size > self._max and len(self._chunks) > 1:
                self._size -= len(self._chunks.popleft())

    def take(self, n):
        """Return float32 (n, 2) samples, zero-filled when not enough data."""
        out = np.zeros((n, 2), dtype=np.float32)
        with self._lock:
            if not self._primed:
                if self._size < self._prime:
                    return out, False
                self._primed = True
            pos = 0
            while pos < n and self._chunks:
                first = self._chunks[0]
                k = min(n - pos, len(first))
                out[pos:pos + k] = first[:k]
                if k == len(first):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = first[k:]
                self._size -= k
                pos += k
            if pos < n:
                self._primed = False
        return out, True


class Recorder:
    """Records frames supplied by ``frame_source()`` plus mixed audio."""

    def __init__(self, path, frame_source, fps=30, device_audio=True, microphone=False,
                 mic_device=None, mic_gain=1.0):
        self.path = str(path)
        self.frame_source = frame_source
        self.fps = fps
        self.device_audio = device_audio
        self.microphone = microphone
        self.mic_device = mic_device
        self.mic_gain = mic_gain
        self.started_at = None
        self.error = None
        self.frames_written = 0
        self._device_fifo = _Fifo()
        self._mic_fifo = _Fifo()
        self._mic_stream = None
        self._running = False
        self._thread = None
        self._canvas = None
        self._last_src = None
        self._last_converted = None

    # -- feeding ---------------------------------------------------------

    def feed_audio(self, pcm: np.ndarray):
        """Device audio as int16 (n, 2) at 44.1 kHz."""
        if self.device_audio and self._running:
            self._device_fifo.put(pcm.astype(np.float32) * (1.0 / 32768.0))

    def _mic_callback(self, indata, frames, time_info, status):
        data = indata.astype(np.float32)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        self._mic_fifo.put(data[:, :2] * self.mic_gain)

    # -- control ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def start(self, timeout=5.0):
        """Start recording; waits up to ``timeout`` seconds for a first frame."""
        deadline = time.monotonic() + timeout
        frame = self.frame_source()
        while frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
            frame = self.frame_source()
        if frame is None:
            raise RuntimeError("no video to record yet")
        width, height = frame.width & ~1, frame.height & ~1
        self._canvas = (width, height)
        codec, pix_fmt = pick_video_encoder(width, height, self.fps)
        self._pix_fmt = pix_fmt
        log.info("recording %dx%d@%d with %s to %s", width, height, self.fps, codec, self.path)

        self._out = av.open(self.path, "w")
        vs = self._out.add_stream(codec, rate=self.fps)
        vs.width, vs.height = width, height
        vs.pix_fmt = pix_fmt
        vs.codec_context.options = dict(_ENCODER_OPTIONS.get(codec, {}))
        if codec not in ("libx264", "h264"):
            vs.codec_context.bit_rate = 8_000_000
        vs.codec_context.time_base = Fraction(1, self.fps)
        self._vstream = vs
        self._astream = None
        if self.device_audio or self.microphone:
            a = self._out.add_stream("aac", rate=SAMPLE_RATE)
            a.layout = "stereo"
            a.codec_context.bit_rate = 160_000
            self._astream = a

        if self.microphone and sd is not None:
            try:
                self._mic_stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                                  device=self.mic_device, callback=self._mic_callback)
                self._mic_stream.start()
            except Exception as exc:
                log.warning("cannot open microphone: %s", exc)
                self._mic_stream = None

        self._running = True
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(10)
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

    # -- worker ----------------------------------------------------------

    def _convert(self, frame):
        """Fit a decoded frame into the fixed recording canvas."""
        if frame is self._last_src and self._last_converted is not None:
            return self._last_converted
        cw, ch = self._canvas
        if (frame.width & ~1, frame.height & ~1) == (cw, ch):
            out = frame.reformat(width=cw, height=ch, format=self._pix_fmt)
        else:
            # Rotation or resolution change: letterbox into the canvas.
            w, h = fit_size(frame.width, frame.height, cw, ch)
            src = frame.reformat(width=w, height=h, format="yuv420p").to_ndarray().reshape(-1)
            canvas = np.full(cw * ch * 3 // 2, 128, dtype=np.uint8)
            canvas[:cw * ch] = 16
            x0, y0 = ((cw - w) // 2) & ~1, ((ch - h) // 2) & ~1
            planes = ((0, 0, w, h, cw, ch, x0, y0),
                      (w * h, cw * ch, w // 2, h // 2, cw // 2, ch // 2, x0 // 2, y0 // 2),
                      (w * h + (w // 2) * (h // 2), cw * ch + (cw // 2) * (ch // 2),
                       w // 2, h // 2, cw // 2, ch // 2, x0 // 2, y0 // 2))
            for s_off, d_off, pw, ph, dw, dh, px, py in planes:
                src_plane = src[s_off:s_off + pw * ph].reshape(ph, pw)
                dst_plane = canvas[d_off:d_off + dw * dh].reshape(dh, dw)
                dst_plane[py:py + ph, px:px + pw] = src_plane
            canvas = canvas.reshape(-1, cw)
            out = av.VideoFrame.from_ndarray(canvas, format="yuv420p")
            if self._pix_fmt != "yuv420p":
                out = out.reformat(format=self._pix_fmt)
        self._last_src = frame
        self._last_converted = out
        return out

    def _encode_video(self, index):
        frame = self.frame_source()
        if frame is None:
            return
        out = self._convert(frame)
        out.pts = index
        out.time_base = Fraction(1, self.fps)
        for packet in self._vstream.encode(out):
            self._out.mux(packet)
        self.frames_written += 1

    def _encode_audio(self, sample_pos):
        mix = np.zeros((AUDIO_CHUNK, 2), dtype=np.float32)
        if self.device_audio:
            mix += self._device_fifo.take(AUDIO_CHUNK)[0]
        if self._mic_stream is not None:
            mix += self._mic_fifo.take(AUDIO_CHUNK)[0]
        np.clip(mix, -1.0, 1.0, out=mix)
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(mix.T), format="fltp", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = sample_pos
        frame.time_base = Fraction(1, SAMPLE_RATE)
        for packet in self._astream.encode(frame):
            self._out.mux(packet)

    def _run(self):
        index = 0
        samples = 0
        audio_lag = 0.15  # let audio arrive before it is mixed
        try:
            while self._running:
                now = time.monotonic() - self.started_at
                if index / self.fps <= now:
                    if now - index / self.fps > 1.0:
                        index = int(now * self.fps)  # fell behind: skip ahead
                    self._encode_video(index)
                    index += 1
                while self._astream is not None and (samples + AUDIO_CHUNK) / SAMPLE_RATE <= now - audio_lag:
                    self._encode_audio(samples)
                    samples += AUDIO_CHUNK
                wait = index / self.fps - (time.monotonic() - self.started_at)
                if wait > 0:
                    time.sleep(min(wait, 0.05))
        except Exception as exc:
            log.exception("recording failed")
            self.error = str(exc)
            self._running = False
        finally:
            self._finish()

    def _finish(self):
        try:
            for packet in self._vstream.encode(None):
                self._out.mux(packet)
            if self._astream is not None:
                for packet in self._astream.encode(None):
                    self._out.mux(packet)
        except Exception as exc:
            log.warning("flushing encoders failed: %s", exc)
        try:
            self._out.close()
        except Exception as exc:
            log.warning("closing recording failed: %s", exc)
        log.info("recording saved: %s (%d frames)", self.path, self.frames_written)
