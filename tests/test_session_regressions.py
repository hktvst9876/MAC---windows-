import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorbridge.config import AppConfig
from mirrorbridge.core import session as session_module


class FakeDecoder:
    def __init__(self, **kwargs):
        self.latest_frame = b"new screenshot"
        self.formats = []
        self.packets = []

    def set_codec(self, codec, config=b""):
        self.formats.append((codec, config))

    async def push_async(self, data, keyframe=False):
        self.packets.append((data, keyframe))

    def stop(self):
        pass


class FixedClock:
    @staticmethod
    def now():
        return datetime(2040, 1, 2, 3, 4, 5)


class FakeRecorder:
    def __init__(self, path, frame_source, **kwargs):
        self.path = str(path)
        self.running = False
        self.error = None

    def start(self):
        assert Path(self.path).read_bytes() == b""
        Path(self.path).write_bytes(b"new recording")
        self.running = True

    def stop(self):
        self.running = False


@pytest.fixture
def make_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "VideoDecoder", FakeDecoder)
    monkeypatch.setattr(session_module, "Recorder", FakeRecorder)
    monkeypatch.setattr(session_module, "datetime", FixedClock)
    monkeypatch.setattr(session_module, "frame_to_png", lambda frame: frame)

    def make(name="Phone"):
        config = AppConfig(audio_enabled=False, screenshot_folder=str(tmp_path),
                           record_folder=str(tmp_path))
        session = session_module.DeviceSession(SimpleNamespace(config=config, session_ended=lambda session: None))
        session.info.name = name
        return session

    return make


def test_screenshots_with_same_name_and_fixed_clock_preserve_captures(tmp_path, make_session):
    existing = tmp_path / "Phone 2040-01-02 03.04.05.png"
    existing.write_bytes(b"existing screenshot")
    first, second = make_session(), make_session()
    first.decoder.latest_frame = b"first screenshot"
    second.decoder.latest_frame = b"second screenshot"

    paths = [first.screenshot(), first.screenshot(), second.screenshot()]

    assert len(set(paths)) == 3
    assert existing not in paths
    assert existing.read_bytes() == b"existing screenshot"
    assert [path.read_bytes() for path in paths] == [
        b"first screenshot", b"first screenshot", b"second screenshot",
    ]


@pytest.mark.parametrize("capture", ["screenshot", "start_recording"])
def test_capture_does_not_overwrite_file_created_during_reservation(
        tmp_path, make_session, monkeypatch, capture):
    session = make_session()
    extension = ".png" if capture == "screenshot" else ".mp4"
    collided_path = tmp_path / ("Phone 2040-01-02 03.04.05" + extension)
    original_open = Path.open
    created = False

    def competing_open(path, mode="r", *args, **kwargs):
        nonlocal created
        if path == collided_path and mode in ("wb", "xb") and not created:
            created = True
            with original_open(path, "wb") as competing_capture:
                competing_capture.write(b"concurrent capture")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_open)

    result = getattr(session, capture)()

    assert created
    assert result != collided_path
    assert collided_path.read_bytes() == b"concurrent capture"
    assert result.read_bytes() == (b"new screenshot" if capture == "screenshot" else b"new recording")
    if capture == "start_recording":
        session.stop_recording()


def test_recordings_with_same_name_and_fixed_clock_preserve_captures(tmp_path, make_session):
    existing = tmp_path / "Phone 2040-01-02 03.04.05.mp4"
    existing.write_bytes(b"existing recording")
    first, second = make_session(), make_session()
    paths = []
    for session in (first, first, second):
        paths.append(session.start_recording())
        session.stop_recording()

    assert len(set(paths)) == 3
    assert existing not in paths
    assert existing.read_bytes() == b"existing recording"
    assert all(path.read_bytes() == b"new recording" for path in paths)


@pytest.mark.parametrize("failure", ["constructor", "start"])
def test_failed_recording_start_removes_only_its_empty_reservation(
        tmp_path, make_session, monkeypatch, failure):
    existing = tmp_path / "Phone 2040-01-02 03.04.05.mp4"
    existing.write_bytes(b"existing recording")
    session = make_session()

    class FailingRecorder(FakeRecorder):
        def __init__(self, *args, **kwargs):
            if failure == "constructor":
                raise RuntimeError("cannot initialize recorder")
            super().__init__(*args, **kwargs)

        def start(self):
            assert Path(self.path).read_bytes() == b""
            raise RuntimeError("cannot start recorder")

    monkeypatch.setattr(session_module, "Recorder", FailingRecorder)
    with pytest.raises(RuntimeError, match="cannot"):
        session.start_recording()

    assert session.recorder is None
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == b"existing recording"


def test_video_configuration_and_keyframes_reach_decoder(make_session):
    session = make_session()
    session.video_format("h264", b"parameter sets", 1920, 1080)
    async def send():
        await session.video_packet(b"keyframe", 1000, True)
        await session.video_packet(b"delta frame", 2000, False)

    asyncio.run(send())

    assert session.decoder.formats == [("h264", b"parameter sets")]
    assert session.decoder.packets == [(b"keyframe", True), (b"delta frame", False)]


def test_recording_cannot_start_after_disconnect(tmp_path, make_session):
    session = make_session()
    session.stopped()
    with pytest.raises(RuntimeError, match="disconnected"):
        session.start_recording()
    assert not list(tmp_path.iterdir())


def test_disconnect_during_recording_start_stops_the_candidate(make_session, monkeypatch):
    session = make_session()
    recorders = []

    class DisconnectingRecorder(FakeRecorder):
        def start(self):
            super().start()
            recorders.append(self)
            session.stopped()

    monkeypatch.setattr(session_module, "Recorder", DisconnectingRecorder)
    with pytest.raises(RuntimeError, match="disconnected"):
        session.start_recording()
    assert session.recorder is None
    assert len(recorders) == 1 and not recorders[0].running
