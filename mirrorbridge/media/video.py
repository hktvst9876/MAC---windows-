"""Video decoding (H.264 / H.265) on a background thread."""

import asyncio
import collections
import logging
import sys
import threading
import time
from fractions import Fraction

import av
import av.codec.context
import numpy as np

log = logging.getLogger(__name__)

_HW_TYPES = {"darwin": ["videotoolbox"], "win32": ["d3d11va", "dxva2", "cuda", "qsv"]}
MAX_QUEUED_PACKETS = 60
MAX_QUEUED_BYTES = 16 * 1024 * 1024


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

    ``on_frame(frame)`` receives decoded :class:`av.VideoFrame` objects even
    when more packets are waiting, so a busy decoder keeps showing progress.
    ``on_display(image, width, height)`` receives a BGRA ``numpy`` array scaled
    to fit ``display_size`` (set by the UI), or is not called if no display size
    is known.
    """

    def __init__(self, on_frame=None, on_display=None, hardware="auto", *,
                 max_queued_packets=MAX_QUEUED_PACKETS, max_queued_bytes=MAX_QUEUED_BYTES):
        if max_queued_packets < 1 or max_queued_bytes < 1:
            raise ValueError("video queue limits must be positive")
        self.on_frame = on_frame
        self.on_display = on_display
        self.hardware = hardware
        self.display_size = None  # (width, height) in device pixels
        self.latest_frame = None
        self.frames = 0
        self.fps = 0.0
        self._queue = collections.deque()
        self._condition = threading.Condition()
        self._max_queued_packets = max_queued_packets
        self._max_queued_bytes = max_queued_bytes
        self._queued_packets = 0
        self._queued_bytes = 0
        self._generation = 0
        self._input_codec = None
        self._config = b""
        self._waiting_keyframe = False
        self._prepend_config = False
        self._capacity_waiter = None
        self._async_push_active = False
        self._codec = None
        self._ctx = None
        self._thread = threading.Thread(target=self._run, name="video-decoder", daemon=True)
        self._running = True
        self._fps_count = 0
        self._fps_time = time.monotonic()
        self._thread.start()

    def _restart_locked(self, wait_for_keyframe):
        """Discard a complete dependency chain, then recreate the decoder."""
        self._generation += 1
        self._queue.clear()
        self._queued_packets = self._queued_bytes = 0
        self._waiting_keyframe = wait_for_keyframe
        self._prepend_config = True
        self._queue.append(("codec", self._input_codec, self._generation))
        self._wake_capacity_locked()
        self._condition.notify()

    def _wake_capacity_locked(self):
        waiter, self._capacity_waiter = self._capacity_waiter, None
        if waiter is None:
            return
        loop, future = waiter

        def wake():
            if not future.done():
                future.set_result(None)

        try:
            loop.call_soon_threadsafe(wake)
        except RuntimeError:
            pass  # its event loop has already closed

    def _enqueue_locked(self, data):
        if self._prepend_config:
            data = self._config + data
            self._prepend_config = False
        self._waiting_keyframe = False
        self._queue.append(("data", data, self._generation))
        self._queued_packets += 1
        self._queued_bytes += len(data)
        self._condition.notify()

    def set_codec(self, codec: str, config: bytes = b""):
        with self._condition:
            if not self._running:
                return
            if len(config) > self._max_queued_bytes:
                raise ValueError("video codec configuration exceeds queue byte limit")
            self._input_codec = codec
            self._config = bytes(config)
            self._restart_locked(wait_for_keyframe=False)

    def push(self, data: bytes, keyframe: bool = False):
        with self._condition:
            if not self._running or self._input_codec is None:
                return
            if self._waiting_keyframe and not keyframe:
                return
            size = len(data) + (len(self._config) if self._prepend_config else 0)
            if (self._queued_packets >= self._max_queued_packets or
                    self._queued_bytes + size > self._max_queued_bytes):
                # P/B frames depend on preceding compressed packets. Dropping
                # only the oldest packets would corrupt every following frame.
                # Reset the whole chain and resume at an independent frame.
                self._restart_locked(wait_for_keyframe=True)
                log.warning("video decoder overloaded; waiting for a keyframe")
                if not keyframe:
                    return
                size = len(data) + len(self._config)
            if size > self._max_queued_bytes:
                return  # even an individual keyframe must respect the bound
            self._enqueue_locked(data)

    async def push_async(self, data: bytes, keyframe: bool = False):
        """Await bounded queue capacity without dropping compressed frames.

        A stream reader should await this before reading its next packet.
        Only one asynchronous producer is allowed, so waiting packets cannot
        accumulate in an auxiliary queue. Reset/stop supersede a waiting push.
        """
        loop = asyncio.get_running_loop()
        future = None
        with self._condition:
            if self._async_push_active:
                raise RuntimeError("concurrent asynchronous video producers are not supported")
            self._async_push_active = True
            generation = self._generation
        try:
            while True:
                with self._condition:
                    if (not self._running or self._input_codec is None or
                            generation != self._generation):
                        return
                    if self._waiting_keyframe and not keyframe:
                        return
                    size = len(data) + (len(self._config) if self._prepend_config else 0)
                    if size > self._max_queued_bytes:
                        raise ValueError("video packet exceeds queue byte limit")
                    if (self._queued_packets < self._max_queued_packets and
                            self._queued_bytes + size <= self._max_queued_bytes):
                        self._enqueue_locked(data)
                        return
                    future = loop.create_future()
                    self._capacity_waiter = (loop, future)
                await future
        finally:
            with self._condition:
                self._async_push_active = False
                if self._capacity_waiter is not None and self._capacity_waiter[1] is future:
                    self._capacity_waiter = None

    def reset(self):
        with self._condition:
            if self._running:
                self._restart_locked(wait_for_keyframe=True)

    def stop(self):
        with self._condition:
            self._running = False
            self._generation += 1
            self._queue.clear()
            self._queued_packets = self._queued_bytes = 0
            self._wake_capacity_locked()
            self._condition.notify()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def _use_hardware(self, codec):
        if self.hardware == "on":
            return True
        if self.hardware == "auto":
            return codec == "hevc"
        return False

    def _run(self):
        try:
            self._decode_loop()
        finally:
            with self._condition:
                self._running = False
                self._ctx = None
                self._queue.clear()
                self._queued_packets = self._queued_bytes = 0
                self._wake_capacity_locked()

    def _decode_loop(self):
        while True:
            with self._condition:
                while self._running and not self._queue:
                    self._condition.wait()
                if not self._running:
                    self._ctx = None
                    return
                kind, value, generation = self._queue.popleft()
                if kind == "data":
                    self._queued_packets -= 1
                    self._queued_bytes -= len(value)
                    self._wake_capacity_locked()
            if kind == "codec":
                self._codec = value
                self._ctx = None
                if value is not None:
                    try:
                        self._ctx = create_decoder(value, self._use_hardware(value))
                    except Exception:
                        log.exception("cannot create %s decoder", value)
                        self._ctx = None
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
                with self._condition:
                    if generation != self._generation or not self._running:
                        break  # a stream reset superseded this in-flight decode
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
