"""Screen-mirroring video stream (AirPlay stream type 110).

The sender connects to a TCP port we announce in SETUP and sends packets made
of a 128-byte header followed by a payload:

* type 0: video data, AES-128-CTR encrypted (one continuous keystream), as
  H.264/H.265 NAL units with 4-byte big-endian length prefixes;
* type 1: codec configuration (SPS/PPS, or VPS/SPS/PPS for H.265), plain;
* type 2: heartbeat; type 5: streaming statistics (binary plist).

Video is handed on in Annex-B format (start codes instead of lengths).
"""

import asyncio
import hashlib
import inspect
import logging
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

START_CODE = b"\x00\x00\x00\x01"
HEADER_SIZE = 128
MAX_PAYLOAD = 32 * 1024 * 1024


def stream_cipher(aeskey: bytes, stream_connection_id: int):
    """AES-CTR decryptor for the video stream (keys bound to the stream id)."""
    sid = str(stream_connection_id & 0xFFFFFFFFFFFFFFFF).encode()
    key = hashlib.sha512(b"AirPlayStreamKey" + sid + aeskey).digest()[:16]
    iv = hashlib.sha512(b"AirPlayStreamIV" + sid + aeskey).digest()[:16]
    return Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()


def ntp_to_ns(ts: int) -> int:
    return (ts >> 32) * 1_000_000_000 + (((ts & 0xFFFFFFFF) * 1_000_000_000) >> 32)


def avcc_to_annexb(data: bytes):
    """Replace 4-byte NAL length prefixes with start codes.

    Returns (annexb_bytes, list_of_nal_header_bytes) or (None, None) if the
    data does not parse as length-prefixed NAL units (e.g. failed decryption).
    """
    out = bytearray(data)
    nal_headers = []
    pos = 0
    n = len(out)
    while pos < n:
        if pos + 4 > n:
            return None, None
        size = struct.unpack_from(">I", out, pos)[0]
        if size == 0 or size > n - pos - 4:
            return None, None
        out[pos:pos + 4] = START_CODE
        header = out[pos + 4]
        if header & 0x80:  # forbidden_zero_bit
            return None, None
        nal_headers.append(header)
        pos += 4 + size
    return bytes(out), nal_headers


def parse_h264_config(payload: bytes) -> bytes:
    """avcC-style SPS/PPS record -> Annex-B SPS + PPS."""
    sps_size = struct.unpack_from(">H", payload, 6)[0]
    sps = payload[8:8 + sps_size]
    pps_size = struct.unpack_from(">H", payload, 9 + sps_size)[0]
    pps = payload[11 + sps_size:11 + sps_size + pps_size]
    if len(sps) != sps_size or len(pps) != pps_size:
        raise ValueError("truncated SPS/PPS")
    return START_CODE + sps + START_CODE + pps


def parse_h265_config(payload: bytes) -> bytes:
    """hvcC-style record with VPS/SPS/PPS arrays at 0x75 -> Annex-B."""
    out = b""
    ptr = 0x75
    for marker in (b"\xa0\x00\x01\x00", b"\xa1\x00\x01\x00", b"\xa2\x00\x01\x00"):
        if payload[ptr:ptr + 4] != marker:
            raise ValueError("non-conforming HEVC parameter sets")
        size = struct.unpack_from(">H", payload, ptr + 3)[0]
        ptr += 5
        unit = payload[ptr:ptr + size]
        if len(unit) != size:
            raise ValueError("truncated HEVC parameter set")
        out += START_CODE + unit
        ptr += size
    return out


class MirrorStream:
    """Receives one mirroring stream and forwards video to a handler.

    handler methods used: video_format(codec, config, width, height),
    video_packet(data, timestamp_ns, keyframe), video_paused(bool),
    video_stopped().
    """

    def __init__(self, aeskey: bytes, stream_connection_id: int, handler, remote_ip: str):
        self.handler = handler
        self.remote_ip = remote_ip
        self._accepting = False
        self._cipher = stream_cipher(aeskey, stream_connection_id)
        self._server = None
        self._conn_task = None
        self._writer = None
        self.codec = None
        self.width = 0
        self.height = 0
        self.paused = False
        self.packets = 0
        self.bytes = 0
        self._pending_config = None

    async def start(self, host="0.0.0.0", port=0) -> int:
        self._accepting = True
        try:
            self._server = await asyncio.start_server(self._on_connect, host, port)
        except BaseException:
            self._accepting = False
            raise
        return self._server.sockets[0].getsockname()[1]

    async def _on_connect(self, reader, writer):
        peer = writer.get_extra_info("peername")
        if not self._accepting or not peer or peer[0] != self.remote_ip or self._writer is not None:
            # A data connection never authenticates itself. Bind it to the
            # negotiated RTSP peer and never let it evict an active sender.
            writer.close()
            return
        self._writer = writer
        self._conn_task = asyncio.current_task()
        try:
            await self._read_loop(reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("mirror stream connection closed")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("mirror stream error")
        finally:
            writer.close()
            if self._writer is writer:
                self._writer = None
                self._conn_task = None

    async def _read_loop(self, reader):
        while True:
            header = await reader.readexactly(HEADER_SIZE)
            size = struct.unpack_from("<I", header, 0)[0]
            if size > MAX_PAYLOAD:
                raise ValueError("mirror payload too large: %d" % size)
            payload = await reader.readexactly(size) if size else b""
            result = self.process(header, payload)
            if inspect.isawaitable(result):
                # A slow decoder pauses this stream without blocking other
                # sessions on the shared network loop or dropping references.
                await result

    def process(self, header: bytes, payload: bytes):
        ptype = header[4]
        option = header[6]
        timestamp = struct.unpack_from("<Q", header, 8)[0]
        if ptype == 0:
            return self._video(payload, ntp_to_ns(timestamp))
        elif ptype == 1:
            self._config(header, payload, option)
        elif ptype == 2:
            pass  # heartbeat
        elif ptype == 5:
            pass  # streaming report (binary plist)
        else:
            log.debug("mirror stream: unknown packet type %d (%d bytes)", ptype, len(payload))

    def _config(self, header, payload, option):
        # Option 0x56/0x5e: the sender is suspending video (screen locked or
        # asleep); 0x16/0x1e: normal (resumed).
        paused = option in (0x56, 0x5E)
        if paused != self.paused:
            self.paused = paused
            self.handler.video_paused(paused)
        width_source, height_source = struct.unpack_from("<ff", header, 40)
        width, height = struct.unpack_from("<ff", header, 56)
        if not payload:
            log.error("codec packet without payload: the sender wants a codec we did not offer")
            return
        try:
            if payload[4:8] == b"hvc1":
                codec = "hevc"
                config = parse_h265_config(payload)
            else:
                codec = "h264"
                config = parse_h264_config(payload)
        except (ValueError, struct.error) as exc:
            log.warning("bad codec configuration packet: %s", exc)
            return
        self.codec = codec
        self.width = int(width or width_source)
        self.height = int(height or height_source)
        self._pending_config = config
        log.info("mirror video: %s %dx%d", codec, self.width, self.height)
        self.handler.video_format(codec, config, self.width, self.height)

    def _video(self, payload, timestamp_ns):
        data = self._cipher.update(payload)
        self.packets += 1
        self.bytes += len(payload)
        annexb, nal_headers = avcc_to_annexb(data)
        if annexb is None:
            log.debug("discarding undecodable video packet (%d bytes)", len(payload))
            return
        if self.codec == "hevc":
            keyframe = any(16 <= ((h >> 1) & 0x3F) <= 21 for h in nal_headers)
        else:
            keyframe = any((h & 0x1F) == 5 for h in nal_headers)
        if self._pending_config is not None:
            annexb = self._pending_config + annexb
            self._pending_config = None
        return self.handler.video_packet(annexb, timestamp_ns, keyframe)

    def stop(self):
        self._accepting = False
        if self._server:
            self._server.close()
            self._server = None
        if self._writer:
            self._writer.close()
            self._writer = None
        if self._conn_task and not self._conn_task.done():
            self._conn_task.cancel()
        self._conn_task = None
