import asyncio
import struct

import pytest

from mirrorbridge.airplay.audio import AudioStream
from mirrorbridge.airplay.mirror import HEADER_SIZE, MirrorStream, stream_cipher
from mirrorbridge.airplay.pairing import PairingIdentity
from mirrorbridge.airplay.rtsp import Request, Response
from mirrorbridge.airplay.server import AirPlayServer, AirPlaySession, ReceiverSettings, SessionHandler
from mirrorbridge.config import AppConfig

from .helpers import StubController
from .test_webcast import _read_text, _ws_connect
from mirrorbridge.webcast.websocket import OP_TEXT, encode_frame


class Writer:
    def __init__(self, ip="192.0.2.10"):
        self.ip = ip
        self.closed = False

    def get_extra_info(self, key):
        return (self.ip, 5000) if key == "peername" else None

    def close(self):
        self.closed = True


def test_mirror_rejects_foreign_and_duplicate_connections():
    async def run():
        stream = MirrorStream(bytes(16), 123, SessionHandler(), "192.0.2.10")
        stream._accepting = True
        reading = asyncio.Event()
        finish = asyncio.Event()

        async def read_loop(reader):
            reading.set()
            await finish.wait()

        stream._read_loop = read_loop
        foreign = Writer("192.0.2.66")
        await stream._on_connect(None, foreign)
        assert foreign.closed and not reading.is_set()

        legitimate = Writer()
        task = asyncio.create_task(stream._on_connect(None, legitimate))
        await reading.wait()
        for ip in ("192.0.2.66", "192.0.2.10"):
            duplicate = Writer(ip)
            await stream._on_connect(None, duplicate)
            assert duplicate.closed
            assert not legitimate.closed
        finish.set()
        await task
        stream.stop()
        late = Writer()
        await stream._on_connect(None, late)
        assert late.closed

    asyncio.run(run())


def test_mirror_awaits_video_capacity_without_blocking_other_tasks():
    async def run():
        blocked, release = asyncio.Event(), asyncio.Event()
        packets = []

        class Handler(SessionHandler):
            async def video_packet(self, data, timestamp_ns, keyframe):
                packets.append((data, keyframe))
                if len(packets) == 1:
                    blocked.set()
                    await release.wait()

        key, stream_id = bytes(16), 123
        stream = MirrorStream(key, stream_id, Handler(), "127.0.0.1")
        reader = asyncio.StreamReader()
        cipher = stream_cipher(key, stream_id)
        for nal in (b"\x65\x01", b"\x41\x02"):
            payload = cipher.update(struct.pack(">I", len(nal)) + nal)
            header = bytearray(HEADER_SIZE)
            struct.pack_into("<I", header, 0, len(payload))
            reader.feed_data(header + payload)
        reader.feed_eof()
        task = asyncio.create_task(stream._read_loop(reader))
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            await asyncio.sleep(0)  # other loop work proceeds while decoding is full
            assert len(packets) == 1
            release.set()
            with pytest.raises(asyncio.IncompleteReadError):
                await asyncio.wait_for(task, 2)
            assert packets == [(b"\x00\x00\x00\x01\x65\x01", True),
                               (b"\x00\x00\x00\x01\x41\x02", False)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


class AudioHandler(SessionHandler):
    def __init__(self):
        self.received = []

    def audio_frame(self, payload, timestamp):
        self.received.append((payload, timestamp))


def rtp(seq=100):
    return struct.pack(">BBHII", 0x80, 0x60, seq, 1234, 0) + b"\x00\x01\x00\x02"


def test_audio_ignores_foreign_data_and_invalid_control_packets():
    handler = AudioHandler()
    stream = AudioStream(bytes(16), bytes(16), 1, 352, handler, "192.0.2.10")
    stream._on_data(rtp(), ("192.0.2.66", 6000))
    assert not handler.received
    assert stream._next_seq is None
    for packet in (bytes(4), b"\x80\xd6\x00\x00" + bytes(12), b"\x80\xd4\x00\x00"):
        stream._on_control(packet, ("192.0.2.10", 7000))
    stream._on_control(b"\x80\xd4" + bytes(18), ("192.0.2.66", 7000))
    assert stream._control_addr is None
    assert stream.sync_rtp is None
    stream._on_data(rtp(), ("192.0.2.10", 6000))
    assert handler.received == [(b"\x00\x01\x00\x02", 1234)]
    stream._on_control(b"\x80\xd4" + bytes(18), ("192.0.2.10", 7000))
    assert stream._control_addr == ("192.0.2.10", 7000)


def test_audio_control_uses_negotiated_port_and_accepts_retransmissions():
    handler = AudioHandler()
    stream = AudioStream(bytes(16), bytes(16), 1, 352, handler, "192.0.2.10", 7000)
    packet = b"\x80\xd6\x00\x00" + rtp()
    stream._on_control(packet, ("192.0.2.10", 7001))
    assert not handler.received and stream._control_addr is None
    stream._on_control(packet, ("192.0.2.10", 7000))
    assert len(handler.received) == 1
    assert stream._control_addr == ("192.0.2.10", 7000)


def test_concurrent_airplay_setup_reserves_capacity(monkeypatch):
    import mirrorbridge.airplay.server as module

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class Timing:
            def __init__(self, *args):
                pass

            async def start(self):
                entered.set()
                await release.wait()

            def stop(self):
                pass

        monkeypatch.setattr(module, "NtpClient", Timing)
        server = AirPlayServer(ReceiverSettings(max_sessions=1), PairingIdentity(), bytes(6),
                               lambda session: SessionHandler())
        req = Request("SETUP", "/1", "RTSP/1.0", {}, b"")

        def session_body(i):
            session = AirPlaySession(server, None, Writer())
            session.fairplay.decrypt_key = lambda key: bytes(16)
            server.sessions.add(session)
            return session, {"ekey": bytes(72), "eiv": bytes(16), "deviceID": str(i)}

        first, body = session_body(0)
        first_response = Response()
        pending = asyncio.create_task(first._setup_session(req, body, first_response))
        await entered.wait()
        for i in range(1, 6):
            other, body = session_body(i)
            response = Response()
            await other._setup_session(req, body, response)
            assert response.status == 453
        release.set()
        await pending
        assert first_response.status == 200
        assert sum(s.handler is not None for s in server.sessions) == 1
        first.handle_teardown(req, Response())
        replacement, body = session_body(6)
        response = Response()
        await replacement._setup_session(req, body, response)
        assert response.status == 200
        await server.stop()

    asyncio.run(run())


@pytest.mark.parametrize("close_during_start", [False, True])
def test_failed_or_interrupted_setup_releases_capacity(monkeypatch, close_during_start):
    import mirrorbridge.airplay.server as module

    async def run():
        timing_instances = []
        server = AirPlayServer(ReceiverSettings(max_sessions=1), PairingIdentity(), bytes(6),
                               lambda session: SessionHandler())
        session = AirPlaySession(server, None, Writer())
        session.fairplay.decrypt_key = lambda key: bytes(16)
        server.sessions.add(session)

        class Timing:
            def __init__(self, *args):
                self.stopped = False
                timing_instances.append(self)

            async def start(self):
                if close_during_start:
                    session.close()
                    await asyncio.sleep(0)
                else:
                    raise OSError("timing socket unavailable")

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(module, "NtpClient", Timing)
        req = Request("SETUP", "/1", "RTSP/1.0", {}, b"")
        with pytest.raises((OSError, ConnectionError)):
            await session._setup_session(req, {"ekey": bytes(72), "eiv": bytes(16)}, Response())
        assert session.closed
        assert all(timing.stopped for timing in timing_instances)
        assert server.admit(AirPlaySession(server, None, Writer()))

    asyncio.run(run())


def test_disabling_webcast_closes_connections_waiting_for_hello(tmp_path, monkeypatch):
    import mirrorbridge.webcast.server as module

    monkeypatch.setattr(module, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "local_ipv4_addresses", lambda: ["127.0.0.1"])

    async def run():
        config = AppConfig(audio_enabled=False)
        controller = StubController(config)
        server = module.WebCastServer(controller, port=0)
        await server.start("127.0.0.1")
        reader, writer = await _ws_connect(server.port)
        try:
            config.webcast_enabled = False
            await server.stop()
            try:
                assert await asyncio.wait_for(reader.read(), 2) == b""
            except ConnectionError:
                pass
            assert not controller.sessions
            assert not server._clients
        finally:
            writer.close()
            await server.stop()

    asyncio.run(run())


def test_airplay_admission_counts_browser_sessions():
    server = AirPlayServer(ReceiverSettings(max_sessions=1), PairingIdentity(), bytes(6),
                           lambda session: SessionHandler(), external_session_count=lambda: 1)
    assert not server.admit(AirPlaySession(server, None, Writer()))


def test_browser_admission_counts_pending_airplay_setup(tmp_path, monkeypatch):
    import json
    import mirrorbridge.webcast.server as module

    monkeypatch.setattr(module, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "local_ipv4_addresses", lambda: ["127.0.0.1"])

    async def run():
        controller = StubController(AppConfig(audio_enabled=False, max_sessions=1))
        controller.server = AirPlayServer(
            ReceiverSettings(max_sessions=1), PairingIdentity(), bytes(6), lambda session: SessionHandler())
        pending = AirPlaySession(controller.server, None, Writer())
        assert controller.server.admit(pending)
        assert pending.handler is None
        server = module.WebCastServer(controller, port=0)
        await server.start("127.0.0.1")
        reader, writer = await _ws_connect(server.port)
        try:
            hello = json.dumps({"type": "hello", "name": "browser"}).encode()
            writer.write(encode_frame(OP_TEXT, hello, b"1234"))
            await writer.drain()
            response = await asyncio.wait_for(_read_text(reader), 2)
            assert response["type"] == "error" and "busy" in response["message"]
            assert not controller.sessions
        finally:
            writer.close()
            await server.stop()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["video", "audio"])
def test_stream_starting_during_session_close_is_cleaned_up(monkeypatch, kind):
    import mirrorbridge.airplay.server as module

    async def run():
        server = AirPlayServer(ReceiverSettings(), PairingIdentity(), bytes(6), lambda s: SessionHandler())
        session = AirPlaySession(server, None, Writer())
        session.handler = SessionHandler()
        streams = []

        class Stream:
            def __init__(self, *args):
                self.open = False
                streams.append(self)

            async def start(self):
                session.close()
                await asyncio.sleep(0)
                self.open = True
                return 1234 if kind == "video" else (1234, 1235)

            def stop(self):
                self.open = False

        monkeypatch.setattr(module, "MirrorStream" if kind == "video" else "AudioStream", Stream)
        setup = session._setup_video if kind == "video" else session._setup_audio
        with pytest.raises(ConnectionError):
            await setup({})
        assert streams and not streams[0].open

    asyncio.run(run())


def test_stopped_airplay_server_rejects_queued_connection_callback():
    async def run():
        server = AirPlayServer(ReceiverSettings(), PairingIdentity(), bytes(6), lambda s: SessionHandler())
        writer = Writer()
        await server._on_connect(None, writer)
        assert writer.closed and not server.sessions

    asyncio.run(run())


def test_audio_partial_start_closes_first_endpoint(monkeypatch):
    async def run():
        class Transport:
            closed = False

            def close(self):
                self.closed = True

        transport = Transport()
        calls = 0

        async def endpoint(factory, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second socket unavailable")
            protocol = factory()
            protocol.connection_made(transport)
            return transport, protocol

        monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", endpoint)
        stream = AudioStream(bytes(16), bytes(16), 1, 352, SessionHandler(), "192.0.2.10")
        with pytest.raises(OSError):
            await stream.start()
        assert transport.closed

    asyncio.run(run())
