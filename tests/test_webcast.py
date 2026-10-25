import asyncio
import base64
import json
import os
import ssl

import pytest

from mirrorbridge.config import AppConfig
from mirrorbridge.webcast.websocket import OP_TEXT, accept_key, encode_frame

from .helpers import StubController, make_mkv


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = AppConfig(audio_enabled=False, record_folder=str(tmp_path / "rec"),
                    screenshot_folder=str(tmp_path / "shots"))
    cfg.ensure_identity()
    return cfg


def test_accept_key_rfc6455_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


async def _ws_connect(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(("GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
    head = await reader.readuntil(b"\r\n\r\n")
    assert b"101" in head.split(b"\r\n")[0]
    assert accept_key(key).encode() in head
    return reader, writer


async def _read_text(reader):
    b0, b1 = await reader.readexactly(2)
    n = b1 & 0x7F
    if n == 126:
        n = int.from_bytes(await reader.readexactly(2), "big")
    data = await reader.readexactly(n)
    assert b0 & 0x0F == OP_TEXT
    return json.loads(data)


def test_browser_cast_session(config):
    from mirrorbridge.webcast.server import WebCastServer

    controller = StubController(config)
    media = make_mkv(seconds=2.0)

    async def main():
        server = WebCastServer(controller, port=0)
        await server.start("127.0.0.1")

        # The page is served over HTTPS.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        r, w = await asyncio.open_connection("127.0.0.1", server.port, ssl=ctx)
        w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        page = await r.read()
        w.close()
        assert page.startswith(b"HTTP/1.1 200")
        assert b"getDisplayMedia" in page

        reader, writer = await _ws_connect(server.port)
        mask = os.urandom(4)
        hello = json.dumps({"type": "hello", "name": "Test Chromebook", "mime": "video/webm;codecs=h264"})
        writer.write(encode_frame(OP_TEXT, hello.encode(), mask))
        assert (await _read_text(reader))["type"] == "ready"
        for i in range(0, len(media), 4096):
            writer.write(encode_frame(0x2, media[i:i + 4096], os.urandom(4)))
            await writer.drain()
        await asyncio.sleep(1.5)
        sessions = list(controller.sessions.values())
        writer.write(encode_frame(0x8, b"\x03\xe8", os.urandom(4)))
        await writer.drain()
        writer.close()
        for _ in range(50):
            if controller.ended:
                break
            await asyncio.sleep(0.1)
        await server.stop()
        return sessions

    sessions = asyncio.run(main())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.display_name == "Test Chromebook"
    assert (s.codec, s.width, s.height) == ("h264", 320, 240)
    assert s.decoder.frames >= 35
    assert s.has_audio and s.audio_codec == "aac"
    assert controller.ended == [s]
    shot = s.screenshot()
    assert shot.read_bytes().startswith(b"\x89PNG")


def test_browser_cast_password(config):
    from mirrorbridge.webcast.server import WebCastServer

    config.password = "secret"
    controller = StubController(config)

    async def main():
        server = WebCastServer(controller, port=0)
        await server.start("127.0.0.1")
        reader, writer = await _ws_connect(server.port)
        hello = json.dumps({"type": "hello", "name": "x", "password": "wrong"})
        writer.write(encode_frame(OP_TEXT, hello.encode(), os.urandom(4)))
        reply = await _read_text(reader)
        writer.close()
        await server.stop()
        return reply

    reply = asyncio.run(main())
    assert reply["type"] == "error"
    assert not controller.sessions


def test_certificate_with_very_long_host_name(tmp_path, monkeypatch):
    import socket
    from mirrorbridge.webcast.tls import ensure_certificate
    monkeypatch.setattr(socket, "gethostname",
                        lambda: "sjc22-bt145-ab3c45f9-e6d9-42c7-bec0-81bbad0ff035-2E02DC2BA8BF.local")
    cert, key = ensure_certificate(tmp_path, ["192.168.1.20"])
    assert os.path.getsize(cert) > 0 and os.path.getsize(key) > 0
