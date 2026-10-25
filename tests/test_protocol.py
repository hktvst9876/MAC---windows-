import asyncio

import av
import pytest

from mirrorbridge.airplay.pairing import PairingIdentity
from mirrorbridge.airplay.server import AirPlayServer, ReceiverSettings, SessionHandler, verify_digest
from mirrorbridge.testing.fake_sender import FakeSender


class Recorder(SessionHandler):
    def __init__(self):
        self.events = []
        self.packets = []
        self.audio = []
        self.formats = []

    def started(self, info):
        self.events.append(("started", info.name))

    def video_format(self, codec, config, width, height):
        self.formats.append((codec, width, height))

    def video_packet(self, data, timestamp_ns, keyframe):
        self.packets.append((data, timestamp_ns, keyframe))

    def audio_started(self, codec, spf):
        self.events.append(("audio", codec, spf))

    def audio_frame(self, payload, rtp_timestamp):
        self.audio.append(payload)

    def volume(self, db):
        self.events.append(("volume", db))

    def stopped(self):
        self.events.append(("stopped",))


async def start_server(**kw):
    settings = ReceiverSettings(port=0, **kw)
    handlers = []

    def factory(session):
        h = Recorder()
        handlers.append(h)
        return h

    server = AirPlayServer(settings, PairingIdentity(), bytes.fromhex("020000aabbcc"), factory)
    await server.start("127.0.0.1")
    return server, handlers


async def run_sender(port, seconds=1.0, password=None, **stream_kw):
    s = FakeSender("127.0.0.1", port, password=password)
    await s.connect()
    info = await s.get_info()
    await s.pair()
    ekey = await s.fp_setup(mode=2)
    status = await s.setup_session(ekey)
    if status != 200:
        s.close()
        return status, s, info
    await s.setup_video()
    await s.setup_audio()
    await s.record()
    await s.set_volume(-15.0)
    await s.stream(seconds, **stream_kw)
    await s.teardown()
    s.close()
    return status, s, info


def test_full_session_decrypts_video_and_audio():
    async def main():
        server, handlers = await start_server()
        status, sender, info = await run_sender(server.port, seconds=1.5, width=320, height=240, fps=20)
        await asyncio.sleep(0.3)
        await server.stop()
        return status, sender, info, handlers

    status, sender, info, handlers = asyncio.run(main())
    assert status == 200
    assert info["model"] == "AppleTV3,2"
    assert len(handlers) == 1
    h = handlers[0]
    assert ("started", "Test iPhone") in h.events
    assert h.formats == [("h264", 320, 240)]
    assert len(h.packets) >= 25
    assert h.packets[0][2]  # first packet is a keyframe (with SPS/PPS prepended)
    assert ("volume", -15.0) in h.events
    assert h.events[-1] == ("stopped",)
    assert sender.timing_requests >= 1

    # The decrypted stream must decode into real frames.
    dec = av.CodecContext.create("h264", "r")
    frames = 0
    for data, _, _ in h.packets:
        frames += len(dec.decode(av.Packet(data)))
    assert frames >= len(h.packets) - 2

    # Audio payloads must decrypt into valid AAC frames.
    assert len(h.audio) >= 50
    adec = av.CodecContext.create("aac", "r")
    adec.extradata = bytes.fromhex("1210")
    decoded = sum(len(adec.decode(av.Packet(p))) for p in h.audio)
    assert decoded >= len(h.audio) - 2


def test_password_protection():
    async def main():
        server, handlers = await start_server(password="1234")
        wrong = await run_sender(server.port, seconds=0.2, password="9999", width=160, height=120, fps=10)
        right = await run_sender(server.port, seconds=0.5, password="1234", width=160, height=120, fps=10)
        await server.stop()
        return wrong[0], right[0], handlers

    wrong, right, handlers = asyncio.run(main())
    assert wrong == 401
    assert right == 200
    assert len(handlers) == 1


def test_digest_with_qop():
    # RFC 2617 example values
    import hashlib
    ha1 = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()
    ha2 = hashlib.md5(b"GET:/dir/index.html").hexdigest()
    nonce = "dcd98b7102dd2f0e8b11d0f600bfb0c093"
    resp = hashlib.md5(("%s:%s:00000001:0a4f113b:auth:%s" % (ha1, nonce, ha2)).encode()).hexdigest()
    header = ('Digest username="Mufasa", realm="testrealm@host.com", nonce="%s", uri="/dir/index.html", '
              'qop=auth, nc=00000001, cnonce="0a4f113b", response="%s"' % (nonce, resp))
    assert resp == "6629fae49393a05397450978507c4ef1"
    assert verify_digest("GET", header, "Circle Of Life", nonce)
    assert not verify_digest("GET", header, "wrong", nonce)
