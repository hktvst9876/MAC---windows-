"""Video decoding (H.264 / H.265) on a background thread."""

import logging
import queue
import sys
import threading
import time
from fractions import Fraction

import av
import av.codec.context
import numpy as np

log = logging.getLogger(__name__)

_HW_TYPES = {"darwin": ["videotoolbox"], "win32": ["d3d11va", "dxva2", "cuda", "qsv"]}


def hw_device_types():
    try:
        from av.codec.hwaccel import hwdevices_available
        available = set(hwdevices_available())
    except Exception:
        return []
    preferred = _HW_TYPES.get(sys.platform, ["vaapi", "cuda"])
    return [t for t in preferred if t in available]


def create_decoder(codec: str, hardware: bool):
    ctx = None
    if hardware:
        from av.codec.hwaccel import HWAccel
        for device_type in hw_device_types():
            try:
                ctx = av.CodecContext.create(codec, "r", hwaccel=HWAccel(device_type, allow_software_fallback=True))
                ctx.open()
                log.info("using %s hardware decoding for %s", device_type, codec)
                return ctx
            except Exception as exc:
                log.info("hardware decoder %s unavailable: %s", device_type, exc)
    ctx = av.CodecContext.create(codec, "r")
    ctx.flags |= av.codec.context.Flags.low_delay
    # Frame threading adds a frame of latency per thread; slice threading
    # does not. 4K HEVC in software needs frame threads to keep up.
    ctx.thread_type = "AUTO" if codec == "hevc" else "SLICE"
    ctx.thread_count = 0
    ctx.open()
    return ctx


def fit_size(src_w, src_h, max_w, max_h):
    """Largest even-sized (w, h) with the source aspect ratio inside max_w x max_h."""
    if src_w <= 0 or src_h <= 0 or max_w <= 0 or max_h <= 0:
        return 0, 0
    scale = min(max_w / src_w, max_h / src_h)
    w = max(2, int(src_w * scale) & ~1)
    h = max(2, int(src_h * scale) & ~1)
    return w, h


class VideoDecoder:
    """Decodes Annex-B packets and publishes frames.

    ``on_frame(frame)`` receives every decoded :class:`av.VideoFrame` that is
    current (frames already superseded by queued packets are skipped).
    ``on_display(image, width, height)`` receives a BGRA ``numpy`` array scaled
    to fit ``display_size`` (set by the UI), or is not called if no display size
    is known.
    """

    def __init__(self, on_frame=None, on_display=None, hardware="auto"):
        self.on_frame = on_frame
        self.on_display = on_display
        self.hardware = hardware
        self.display_size = None  # (width, height) in device pixels
        self.latest_frame = None
        self.frames = 0
        self.fps = 0.0
        self._queue = queue.Queue()
        self._codec = None
        self._ctx = None
        self._thread = threading.Thread(target=self._run, name="video-decoder", daemon=True)
        self._running = True
        self._fps_count = 0
        self._fps_time = time.monotonic()
        self._thread.start()

    def set_codec(self, codec: str):
        self._queue.put(("codec", codec))

    def push(self, data: bytes):
        self._queue.put(("data", data))

    def reset(self):
        self._queue.put(("reset", None))

    def stop(self):
        self._running = False
        self._queue.put(("stop", None))

    def join(self, timeout=None):
        self._thread.join(timeout)

    def _use_hardware(self, codec):
        if self.hardware == "on":
            return True
        if self.hardware == "auto":
            return codec == "hevc"
        return False

    def _run(self):
        while self._running:
            kind, value = self._queue.get()
            if kind == "stop":
                break
            if kind == "codec":
                if value != self._codec or self._ctx is None:
                    self._codec = value
                    try:
                        self._ctx = create_decoder(value, self._use_hardware(value))
                    except Exception:
                        log.exception("cannot create %s decoder", value)
                        self._ctx = None
                continue
            if kind == "reset":
                self._ctx = None
                if self._codec:
                    try:
                        self._ctx = create_decoder(self._codec, self._use_hardware(self._codec))
                    except Exception:
                        log.exception("cannot recreate decoder")
                continue
            if self._ctx is None:
                continue
            try:
                frames = self._ctx.decode(av.Packet(value))
            except av.error.InvalidDataError:
                log.debug("invalid video data")
                continue
            except Exception as exc:
                log.warning("video decode error: %s", exc)
                continue
            for frame in frames:
                self.publish(frame)

    def publish(self, frame):
        """Present a decoded frame (also used by sources that decode themselves)."""
        self.latest_frame = frame
        self.frames += 1
        self._fps_count += 1
        now = time.monotonic()
        if now - self._fps_time >= 1.0:
            self.fps = self._fps_count / (now - self._fps_time)
            self._fps_count = 0
            self._fps_time = now
        if not self._queue.empty():
            return  # a newer frame is on its way; skip presenting this one
        if self.on_frame:
            self.on_frame(frame)
        if self.on_display and self.display_size:
            w, h = fit_size(frame.width, frame.height, *self.display_size)
            if w and h:
                try:
                    img = frame.reformat(width=w, height=h, format="bgra", interpolation="BILINEAR")
                    self.on_display(np.ascontiguousarray(img.to_ndarray()), w, h)
                except Exception as exc:
                    log.warning("frame conversion failed: %s", exc)


def frame_to_png(frame: av.VideoFrame) -> bytes:
    """Encode a decoded frame as a PNG image (full resolution)."""
    enc = av.CodecContext.create("png", "w")
    enc.width = frame.width
    enc.height = frame.height
    enc.pix_fmt = "rgb24"
    enc.time_base = Fraction(1, 25)
    rgb = frame.reformat(format="rgb24")
    rgb.pts = None
    packets = enc.encode(rgb) + enc.encode(None)
    return b"".join(bytes(p) for p in packets)
