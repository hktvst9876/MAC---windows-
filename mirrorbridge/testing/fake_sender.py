"""A simulated AirPlay screen-mirroring sender, for testing without an iPhone.

It performs the same handshake an iOS device does (pair-setup, pair-verify,
fp-setup, SETUP) and then streams an encrypted H.264 test pattern and AAC
audio. Because the sender cannot produce Apple-encrypted keys, it picks a
random FairPlay message and computes the key the receiver will derive from it
with the same playfair code.

Usage:  python -m mirrorbridge.testing.fake_sender --host 127.0.0.1 --port 7100
"""

import argparse
import asyncio
import hashlib
import logging
import math
import os
import plistlib
import struct
import time
from fractions import Fraction

import av
import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..airplay.mirror import stream_cipher
from ..airplay.ntp import ntp_now
from ..airplay.playfair import playfair_decrypt

log = logging.getLogger(__name__)

RAW = serialization.Encoding.Raw
RAW_PUB = serialization.PublicFormat.Raw
BPLIST = "application/x-apple-binary-plist"


def split_annexb(data: bytes):
    """Split an Annex-B byte stream into NAL units (without start codes)."""
    nals = []
    i = 0
    n = len(data)
    starts = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0 and (data[i + 2] == 1 or (data[i + 2] == 0 and data[i + 3] == 1)):
            sc = 3 if data[i + 2] == 1 else 4
            starts.append((i, i + sc))
            i += sc
        else:
            i += 1
    for k, (s, e) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else n
        nals.append(data[e:end])
    return nals


def make_test_frame(width, height, index, fps):
    """A colourful moving test pattern with a progress bar."""
    y = np.linspace(0, 255, height, dtype=np.float32)[:, None]
    x = np.linspace(0, 255, width, dtype=np.float32)[None, :]
    t = index / fps
    r = (x + 60 * t) % 256
    g = (y + 40 * t) % 256
    b = np.full((height, width), 128 + 127 * math.sin(t), dtype=np.float32)
    img = np.stack(np.broadcast_arrays(r, g, b), axis=-1).astype(np.uint8)
    bar_w = int(width * ((index % (fps * 4)) / (fps * 4)))
    img[height - 40:height - 10, 10:10 + bar_w] = 255
    # A white square that moves around, handy for checking smoothness.
    cx = int((width - 80) * (0.5 + 0.5 * math.sin(t * 1.3)))
    cy = int((height - 120) * (0.5 + 0.5 * math.cos(t * 0.9)))
    img[cy:cy + 80, cx:cx + 80] = 255
    return av.VideoFrame.from_ndarray(img, format="rgb24")


class FakeSender:
    def __init__(self, host, port, name="Test iPhone", device_id="AA:BB:CC:DD:EE:01", password=None):
        self.host = host
        self.port = port
        self.name = name
        self.device_id = device_id
        self.password = password
        self.cseq = 0
        self.reader = None
        self.writer = None
        self.aeskey = None
        self.eiv = os.urandom(16)
        self.ecdh_secret = None
        self.server_pk = None
        self.timing = None
        self.timing_requests = 0
        self.ports = {}

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def request(self, method, url, body=b"", content_type=None, headers=None):
        self.cseq += 1
        lines = ["%s %s RTSP/1.0" % (method, url), "CSeq: %d" % self.cseq,
                 "User-Agent: AirPlay/770.8.1", "DACP-ID: 1234567890ABCDEF", "Active-Remote: 123456"]
        for k, v in (headers or {}).items():
            lines.append("%s: %s" % (k, v))
        if content_type:
            lines.append("Content-Type: " + content_type)
        lines.append("Content-Length: %d" % len(body))
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await self.writer.drain()
        head = await self.reader.readuntil(b"\r\n\r\n")
        status_line, *hdr_lines = head.decode().split("\r\n")
        status = int(status_line.split(" ")[1])
        hdrs = {}
        for line in hdr_lines:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        length = int(hdrs.get("content-length", 0))
        data = await self.reader.readexactly(length) if length else b""
        return status, hdrs, data

    async def get_info(self):
        status, _, data = await self.request("GET", "/info")
        assert status == 200, status
        info = plistlib.loads(data)
        self.server_pk = info["pk"]
        return info

    async def pair(self):
        ed = Ed25519PrivateKey.generate()
        ed_pub = ed.public_key().public_bytes(RAW, RAW_PUB)
        status, _, data = await self.request("POST", "/pair-setup", ed_pub, "application/octet-stream")
        assert status == 200 and len(data) == 32, status
        self.server_pk = data
        x = X25519PrivateKey.generate()
        x_pub = x.public_key().public_bytes(RAW, RAW_PUB)
        status, _, data = await self.request("POST", "/pair-verify", b"\x01\x00\x00\x00" + x_pub + ed_pub,
                                             "application/octet-stream")
        assert status == 200 and len(data) == 96, status
        server_x = data[:32]
        secret = x.exchange(X25519PublicKey.from_public_bytes(server_x))
        key = hashlib.sha512(b"Pair-Verify-AES-Key" + secret).digest()[:16]
        iv = hashlib.sha512(b"Pair-Verify-AES-IV" + secret).digest()[:16]
        ctr = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
        server_sig = ctr.update(data[32:])
        Ed25519PublicKey.from_public_bytes(self.server_pk).verify(server_sig, server_x + x_pub)
        our_sig = ctr.update(ed.sign(x_pub + server_x))
        status, _, _ = await self.request("POST", "/pair-verify", b"\x00\x00\x00\x00" + our_sig,
                                          "application/octet-stream")
        assert status == 200, status
        self.ecdh_secret = secret

    async def fp_setup(self, mode=1):
        first = bytes([0x46, 0x50, 0x4C, 0x59, 0x03, 0x01, 0x01, 0, 0, 0, 0, 0x04, 0x02, 0x00, mode, 0xBB])
        status, _, data = await self.request("POST", "/fp-setup", first, "application/octet-stream")
        assert status == 200 and len(data) == 142, status
        message = bytearray(os.urandom(164))
        message[:4] = b"FPLY"
        message[4] = 3
        message[12] = mode
        status, _, data = await self.request("POST", "/fp-setup", bytes(message), "application/octet-stream")
        assert status == 200 and len(data) == 32, status
        ekey = os.urandom(72)
        aeskey = playfair_decrypt(bytes(message), ekey)
        self.aeskey = hashlib.sha512(aeskey + self.ecdh_secret).digest()[:16]
        return ekey

    async def _start_timing(self):
        sender = self

        class Timing(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                if len(data) >= 32 and data[1] & 0x7F == 0x52:
                    sender.timing_requests += 1
                    origin = data[24:32]
                    now = struct.pack(">Q", ntp_now())
                    reply = bytes([0x80, 0xD3, 0x00, 0x07]) + bytes(4) + origin + now + now
                    self.transport.sendto(reply, addr)

        loop = asyncio.get_running_loop()
        self.timing, _ = await loop.create_datagram_endpoint(Timing, local_addr=("0.0.0.0", 0))
        return self.timing.get_extra_info("sockname")[1]

    async def setup_session(self, ekey):
        timing_port = await self._start_timing()
        body = plistlib.dumps({
            "ekey": ekey, "eiv": self.eiv, "timingProtocol": "NTP", "timingPort": timing_port,
            "deviceID": self.device_id, "name": self.name, "model": "iPhone15,2",
            "osVersion": "18.0", "isScreenMirroringSession": True, "sessionUUID": "TEST-UUID",
            "macAddress": self.device_id,
        }, fmt=plistlib.FMT_BINARY)
        headers = {}
        status, hdrs, data = await self.request("SETUP", "rtsp://%s/1" % self.host, body, BPLIST)
        if status == 401 and self.password is not None:
            nonce = hdrs["www-authenticate"].split('nonce="')[1].split('"')[0]
            uri = "rtsp://%s/1" % self.host
            ha1 = hashlib.md5(("AirPlay:raop:%s" % self.password).encode()).hexdigest()
            ha2 = hashlib.md5(("SETUP:%s" % uri).encode()).hexdigest()
            resp = hashlib.md5(("%s:%s:%s" % (ha1, nonce, ha2)).encode()).hexdigest()
            headers["Authorization"] = ('Digest username="AirPlay", realm="raop", nonce="%s", uri="%s", '
                                        'response="%s"' % (nonce, uri, resp))
            status, hdrs, data = await self.request("SETUP", uri, body, BPLIST, headers)
        if status != 200:
            return status
        reply = plistlib.loads(data)
        self.ports["timing"] = reply.get("timingPort")
        return status

    async def setup_video(self, stream_id=0x1234567890ABCDEF):
        body = plistlib.dumps({"streams": [{"type": 110, "streamConnectionID": stream_id,
                                            "timestampInfo": []}]}, fmt=plistlib.FMT_BINARY)
        status, _, data = await self.request("SETUP", "rtsp://%s/1" % self.host, body, BPLIST)
        assert status == 200, status
        stream = plistlib.loads(data)["streams"][0]
        self.ports["video"] = stream["dataPort"]
        self.video_cipher = stream_cipher(self.aeskey, stream_id)

    async def setup_audio(self, ct=4, spf=1024):
        body = plistlib.dumps({"streams": [{"type": 96, "ct": ct, "spf": spf, "audioFormat": 0x400000,
                                            "controlPort": 0, "isMedia": False}]}, fmt=plistlib.FMT_BINARY)
        status, _, data = await self.request("SETUP", "rtsp://%s/1" % self.host, body, BPLIST)
        assert status == 200, status
        stream = plistlib.loads(data)["streams"][0]
        self.ports["audio"] = stream["dataPort"]
        self.ports["control"] = stream["controlPort"]

    async def record(self):
        status, hdrs, _ = await self.request("RECORD", "rtsp://%s/1" % self.host)
        assert status == 200, status
        return hdrs

    async def set_volume(self, db):
        return await self.request("SET_PARAMETER", "rtsp://%s/1" % self.host,
                                  b"volume: %.6f\r\n" % db, "text/parameters")

    async def teardown(self):
        try:
            await self.request("TEARDOWN", "rtsp://%s/1" % self.host,
                               plistlib.dumps({"streams": [{"type": 110}]}, fmt=plistlib.FMT_BINARY), BPLIST)
            await self.request("TEARDOWN", "rtsp://%s/1" % self.host)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    def close(self):
        if self.timing:
            self.timing.close()
        if self.writer:
            self.writer.close()

    # -- media -----------------------------------------------------------

    async def stream(self, seconds=3.0, width=640, height=360, fps=30, audio=True, rotate_at=None):
        """Stream video (and audio) for the given duration."""
        reader, writer = await asyncio.open_connection(self.host, self.ports["video"])
        audio_task = None
        if audio and "audio" in self.ports:
            audio_task = asyncio.get_running_loop().create_task(self._stream_audio(seconds))
        try:
            await self._stream_video(writer, seconds, width, height, fps, rotate_at)
        finally:
            if audio_task:
                await audio_task
            writer.close()

    def _encoder(self, width, height, fps):
        last_error = None
        for name in ("libx264", "h264_videotoolbox", "h264_mf", "h264"):
            try:
                enc = av.CodecContext.create(name, "w")
                enc.width, enc.height = width, height
                enc.pix_fmt = "yuv420p"
                enc.time_base = Fraction(1, fps)
                enc.framerate = Fraction(fps, 1)
                if name == "libx264":
                    enc.options = {"preset": "ultrafast", "tune": "zerolatency", "keyint": "100000",
                                   "bframes": "0"}
                else:
                    enc.max_b_frames = 0
                    enc.gop_size = 100000
                    enc.bit_rate = 4_000_000
                enc.open()
                return enc
            except Exception as exc:
                last_error = exc
        raise RuntimeError("no H.264 encoder available: %s" % last_error)

    async def _stream_video(self, writer, seconds, width, height, fps, rotate_at):
        enc = self._encoder(width, height, fps)
        start = time.monotonic()
        frames = int(seconds * fps)
        sent_config = False
        for i in range(frames):
            if rotate_at is not None and i == int(rotate_at * fps):
                # Simulate a device rotation: new size, new parameter sets.
                for pkt in enc.encode(None):
                    pass
                width, height = height, width
                enc = self._encoder(width, height, fps)
                sent_config = False
            frame = make_test_frame(width, height, i, fps).reformat(format="yuv420p")
            frame.pts = i
            for pkt in enc.encode(frame):
                nals = split_annexb(bytes(pkt))
                sps = [n for n in nals if n[0] & 0x1F == 7]
                pps = [n for n in nals if n[0] & 0x1F == 8]
                if sps and pps and not sent_config:
                    self._send_codec(writer, sps[0], pps[0], width, height)
                    sent_config = True
                vcl = [n for n in nals if n[0] & 0x1F not in (7, 8)]
                payload = b"".join(struct.pack(">I", len(n)) + n for n in vcl)
                ts = int((time.monotonic() - start + 1000) * (1 << 32))
                header = bytearray(128)
                struct.pack_into("<I", header, 0, len(payload))
                header[4] = 0
                header[5] = 0x10 if any(n[0] & 0x1F == 5 for n in vcl) else 0
                struct.pack_into("<Q", header, 8, ts)
                writer.write(bytes(header) + self.video_cipher.update(payload))
            await writer.drain()
            delay = start + (i + 1) / fps - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    def _send_codec(self, writer, sps, pps, width, height):
        payload = bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps \
            + bytes([1]) + struct.pack(">H", len(pps)) + pps
        header = bytearray(128)
        struct.pack_into("<I", header, 0, len(payload))
        header[4] = 1
        header[6] = 0x16
        header[7] = 0x01
        struct.pack_into("<ff", header, 16, width, height)
        struct.pack_into("<ff", header, 40, width, height)
        struct.pack_into("<ff", header, 56, width, height)
        writer.write(bytes(header) + payload)

    async def _stream_audio(self, seconds):
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=(
            self.host, self.ports["audio"]))
        enc = av.CodecContext.create("aac", "w")
        enc.sample_rate = 44100
        enc.layout = "stereo"
        enc.format = "fltp"
        enc.bit_rate = 128000
        enc.open()
        spf = enc.frame_size or 1024
        total = int(seconds * 44100)
        seq = 1000
        rtp = 0
        start = time.monotonic()
        sent = 0
        for offset in range(0, total, spf):
            t = (np.arange(spf) + offset) / 44100.0
            tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
            frame.sample_rate = 44100
            frame.pts = offset
            for pkt in enc.encode(frame):
                data = bytes(pkt)
                n = len(data) & ~0xF
                cbc = Cipher(algorithms.AES(self.aeskey), modes.CBC(self.eiv)).encryptor()
                payload = cbc.update(data[:n]) + data[n:]
                header = struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, rtp & 0xFFFFFFFF, 0)
                transport.sendto(header + payload)
                seq += 1
                rtp += spf
                sent += 1
            delay = start + (offset + spf) / 44100.0 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        transport.close()
        return sent


async def run(host, port, seconds, width, height, fps, password=None, name="Test iPhone"):
    sender = FakeSender(host, port, name=name, password=password)
    await sender.connect()
    info = await sender.get_info()
    log.info("test sender: receiver %s (%s), features 0x%X", info["name"], info["model"], info["features"])
    await sender.pair()
    ekey = await sender.fp_setup()
    status = await sender.setup_session(ekey)
    if status != 200:
        log.warning("test sender: SETUP failed with status %d", status)
        sender.close()
        return
    await sender.setup_video()
    await sender.setup_audio()
    await sender.record()
    log.info("test sender: streaming %.1fs of %dx%d@%d video + AAC audio", seconds, width, height, fps)
    await sender.stream(seconds, width, height, fps)
    await sender.teardown()
    sender.close()
    log.info("test sender: done")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7100)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--password")
    ap.add_argument("--name", default="Test iPhone")
    args = ap.parse_args()
    asyncio.run(run(args.host, args.port, args.seconds, args.width, args.height, args.fps, args.password,
                    args.name))


if __name__ == "__main__":
    main()
