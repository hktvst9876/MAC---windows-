"""Failure and overload paths without opening audio devices or GPU decoders."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from mirrorbridge.media import recorder as recorder_module
from mirrorbridge.media import video as video_module


class _Microphone:
    def __init__(self, *, fail_start=False, fail_stop=False):
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.starts = self.stops = self.closes = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise OSError("microphone start failed")

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise OSError("microphone stop failed")

    def close(self):
        self.closes += 1


class _Encoder:
    def __init__(self):
        self.codec_context = SimpleNamespace()

    def encode(self, frame):
        return []


class _Output:
    def __init__(self, fail_audio=False):
        self.closes = 0
        self.fail_audio = fail_audio

    def add_stream(self, codec, **kwargs):
        if self.fail_audio and codec == "aac":
            raise OSError("audio encoder unavailable")
        return _Encoder()

    def mux(self, packet):
        pass

    def close(self):
        self.closes += 1


def _mock_recording_start(monkeypatch, output, mic=None):
    monkeypatch.setattr(recorder_module, "pick_video_encoder", lambda *args: ("libx264", "yuv420p"))
    monkeypatch.setattr(recorder_module.av, "open", lambda *args: output)
    monkeypatch.setattr(recorder_module, "resolve_device", lambda device, kind: device)
    monkeypatch.setattr(recorder_module, "sd", SimpleNamespace(InputStream=lambda **kwargs: mic))
    return recorder_module.Recorder("unused.mp4", lambda: SimpleNamespace(width=320, height=180),
                                    microphone=mic is not None)


def test_recording_failure_always_closes_microphone_and_container():
    rec = recorder_module.Recorder("unused.mp4", lambda: None, microphone=True)
    mic = _Microphone(fail_stop=True)
    output = _Output()
    rec._mic_stream = mic
    rec._out = output
    rec._vstream = _Encoder()
    rec._running = True
    rec.started_at = time.monotonic()

    def full_disk(index):
        raise OSError("disk full")

    rec._encode_video = full_disk
    rec._run()
    rec.stop()
    rec.stop()
    assert rec.error == "disk full"
    assert not rec.running
    assert (mic.stops, mic.closes, output.closes) == (1, 1, 1)


def test_failed_microphone_start_closes_partially_open_stream(monkeypatch):
    mic = _Microphone(fail_start=True, fail_stop=True)
    output = _Output()
    rec = _mock_recording_start(monkeypatch, output, mic)
    monkeypatch.setattr(rec, "_run", lambda: None)
    rec.start()
    rec._thread.join(2)
    rec.stop()
    assert (mic.starts, mic.stops, mic.closes) == (1, 1, 1)
    assert rec._mic_stream is None
    assert output.closes == 1


def test_encoder_setup_failure_closes_container(monkeypatch):
    output = _Output(fail_audio=True)
    rec = _mock_recording_start(monkeypatch, output)
    with pytest.raises(OSError, match="audio encoder unavailable"):
        rec.start()
    rec.stop()
    assert not rec.running
    assert output.closes == 1


def test_worker_start_failure_closes_microphone_and_container(monkeypatch):
    class UnstartableThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

        def is_alive(self):
            return False

    mic = _Microphone()
    output = _Output()
    rec = _mock_recording_start(monkeypatch, output, mic)
    monkeypatch.setattr(recorder_module.threading, "Thread", UnstartableThread)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        rec.start()
    rec.stop()
    assert not rec.running
    assert (mic.starts, mic.stops, mic.closes, output.closes) == (1, 1, 1, 1)


def test_video_presents_frames_while_packets_remain(monkeypatch):
    started, release, presented = threading.Event(), threading.Event(), threading.Event()
    first = True

    class Decoder:
        def decode(self, packet):
            nonlocal first
            if first:
                first = False
                started.set()
                assert release.wait(2)
            return [object()]

    monkeypatch.setattr(video_module, "create_decoder", lambda *args: Decoder())
    counts_at_presentation = []

    def on_frame(frame):
        with dec._condition:
            counts_at_presentation.append(dec._queued_packets)
        presented.set()

    dec = video_module.VideoDecoder(on_frame=on_frame, hardware="off")
    try:
        dec.set_codec("h264")
        dec.push(b"key", keyframe=True)
        assert started.wait(2)
        dec.push(b"predicted")
        release.set()
        assert presented.wait(2)
        assert counts_at_presentation[0] == 1
    finally:
        release.set()
        dec.stop()
        dec.join(2)
    assert not dec._thread.is_alive()


@pytest.mark.parametrize("packet_limit,byte_limit", [(2, 100), (10, 10)])
def test_video_overload_bounds_backlog_and_recovers_at_configured_keyframe(
        monkeypatch, packet_limit, byte_limit):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    decoded, presented = [], []
    contexts = []

    class Decoder:
        def decode(self, packet):
            data = bytes(packet)
            decoded.append(data)
            if len(decoded) == 1:
                started.set()
                assert release.wait(2)
            return [data]

    def create(*args):
        ctx = Decoder()
        contexts.append(ctx)
        return ctx

    def on_frame(frame):
        presented.append(frame)
        if frame == b"p":
            finished.set()

    monkeypatch.setattr(video_module, "create_decoder", create)
    dec = video_module.VideoDecoder(on_frame=on_frame, hardware="off",
                                   max_queued_packets=packet_limit, max_queued_bytes=byte_limit)
    try:
        dec.set_codec("h264", b"cfg")
        dec.push(b"oldkey", keyframe=True)
        assert started.wait(2)
        # These exceed either the count bound or the byte bound, while the
        # first packet is still decoding. Their dependent frames must go too.
        for _ in range(3):
            dec.push(b"xxxxxx")
        dec.push(b"dependent")
        dec.push(bytes(byte_limit + 1), keyframe=True)
        with dec._condition:
            assert dec._waiting_keyframe
            assert dec._queued_packets == dec._queued_bytes == 0
        dec.push(b"key", keyframe=True)
        dec.push(b"p")
        with dec._condition:
            assert dec._queued_packets <= packet_limit
            assert dec._queued_bytes <= byte_limit
        release.set()
        assert finished.wait(2)
    finally:
        release.set()
        dec.stop()
        dec.join(2)
    assert decoded == [b"cfgoldkey", b"cfgkey", b"p"]
    assert presented == [b"cfgkey", b"p"]  # superseded in-flight frame is hidden
    assert len(contexts) == 2
    assert not dec._thread.is_alive()


def _blocked_video_decoder(monkeypatch, **limits):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    decoded, contexts = [], []

    class Decoder:
        def decode(self, packet):
            decoded.append(bytes(packet))
            if len(decoded) == 1:
                started.set()
                assert release.wait(2)
            if bytes(packet) == b"two":
                finished.set()
            return [object()]

    def create(*args):
        ctx = Decoder()
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr(video_module, "create_decoder", create)
    decoder = video_module.VideoDecoder(hardware="off", **limits)
    decoder.set_codec("h264", b"c")
    return decoder, started, release, finished, decoded, contexts


@pytest.mark.parametrize("packet_limit,byte_limit", [(1, 100), (10, 4)])
def test_async_video_backpressure_preserves_interframe_chain(monkeypatch, packet_limit, byte_limit):
    dec, started, release, finished, decoded, contexts = _blocked_video_decoder(
        monkeypatch, max_queued_packets=packet_limit, max_queued_bytes=byte_limit)

    async def produce():
        await dec.push_async(b"key", keyframe=True)
        assert started.wait(2)
        await dec.push_async(b"one")
        pending = asyncio.create_task(dec.push_async(b"two"))
        await asyncio.sleep(0)  # run the producer until it awaits queue capacity
        assert not pending.done()
        with dec._condition:
            assert dec._queued_packets <= packet_limit
            assert dec._queued_bytes <= byte_limit
            assert dec._capacity_waiter is not None
        with pytest.raises(RuntimeError, match="concurrent asynchronous"):
            await dec.push_async(b"another")
        release.set()
        await asyncio.wait_for(pending, 2)
        assert finished.wait(2)

    try:
        asyncio.run(produce())
        assert decoded == [b"ckey", b"one", b"two"]
        assert len(contexts) == 1  # no dependency reset or replacement keyframe
        assert not dec._waiting_keyframe
    finally:
        release.set()
        dec.stop()
        dec.join(2)
    assert not dec._thread.is_alive()


@pytest.mark.parametrize("action", ["stop", "reset", "cancel"])
def test_async_video_capacity_waiter_is_released(monkeypatch, action):
    dec, started, release, _, _, _ = _blocked_video_decoder(monkeypatch, max_queued_packets=1)

    async def produce():
        await dec.push_async(b"key", keyframe=True)
        assert started.wait(2)
        await dec.push_async(b"one")
        pending = asyncio.create_task(dec.push_async(b"two"))
        await asyncio.sleep(0)
        assert not pending.done()
        if action == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            getattr(dec, action)()
            await asyncio.wait_for(pending, 2)
        with dec._condition:
            assert dec._capacity_waiter is None
            assert not dec._async_push_active

    try:
        asyncio.run(produce())
    finally:
        release.set()
        dec.stop()
        dec.join(2)
    assert not dec._thread.is_alive()


def test_async_video_rejects_unqueueable_packet_without_waiting(monkeypatch):
    dec, _, release, _, _, _ = _blocked_video_decoder(monkeypatch, max_queued_bytes=4)

    async def produce():
        with pytest.raises(ValueError, match="packet exceeds queue byte limit"):
            await dec.push_async(b"oversized", keyframe=True)
        with dec._condition:
            assert dec._capacity_waiter is None
            assert not dec._async_push_active

    try:
        asyncio.run(produce())
    finally:
        release.set()
        dec.stop()
        dec.join(2)
