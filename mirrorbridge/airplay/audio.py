"""RTP audio stream (AirPlay stream type 96).

Audio arrives as RTP over UDP. Each payload is AES-128-CBC encrypted with the
session key and IV (the cipher restarts for every packet, and a trailing
partial block is sent in the clear). A control port carries sync packets and
retransmissions; we ask for missing packets there.

Codecs by "ct": 2 = ALAC (352 samples/frame), 4 = AAC-LC (1024),
8 = AAC-ELD (480, used for screen mirroring). All are 44.1 kHz stereo.
"""

import asyncio
import logging
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

NO_DATA_MARKER = b"\x00\x68\x34\x00"
CODEC_NAMES = {1: "pcm", 2: "alac", 4: "aac", 8: "aac-eld"}
REORDER_WINDOW = 16  # packets held back while waiting for a missing one
SEQ_MOD = 1 << 16


def seq_diff(a: int, b: int) -> int:
    """Signed difference a - b of two 16-bit sequence numbers."""
    d = (a - b) & 0xFFFF
    return d - SEQ_MOD if d >= 0x8000 else d


class _Endpoint(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self.callback = callback
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            self.callback(data, addr)
        except Exception:
            log.exception("audio packet handling failed")

    def error_received(self, exc):
        log.debug("audio socket error: %s", exc)


class AudioStream:
    """handler methods used: audio_frame(payload, rtp_timestamp), audio_flush()."""

    def __init__(self, aeskey: bytes, aesiv: bytes, ct: int, spf: int, handler,
                 remote_ip: str, remote_control_port: int = 0):
        self.key = aeskey
        self.iv = aesiv
        self.ct = ct
        self.spf = spf
        self.handler = handler
        self.remote_ip = remote_ip
        self.remote_control_port = remote_control_port
        self._data = None
        self._control = None
        self._control_addr = None
        self._next_seq = None
        self._pending = {}
        self._resend_seq = 0
        self._requested = set()
        self.packets = 0
        self.lost = 0
        self.sync_rtp = None
        self.sync_ntp = None

    async def start(self, host="0.0.0.0", data_port=0, control_port=0):
        loop = asyncio.get_running_loop()
        try:
            dt, self._data = await loop.create_datagram_endpoint(
                lambda: _Endpoint(self._on_data), local_addr=(host, data_port))
            ct, self._control = await loop.create_datagram_endpoint(
                lambda: _Endpoint(self._on_control), local_addr=(host, control_port))
        except BaseException:
            self.stop()
            raise
        return dt.get_extra_info("sockname")[1], ct.get_extra_info("sockname")[1]

    def decrypt(self, packet: bytes) -> bytes:
        payload = packet[12:]
        n = len(payload) & ~0xF
        if not n:
            return payload
        dec = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        return dec.update(payload[:n]) + payload[n:]

    def _on_data(self, data, addr):
        if addr[0] == self.remote_ip and self._valid_rtp(data):
            self._enqueue(data)

    @staticmethod
    def _valid_rtp(data):
        return len(data) >= 12 and data[0] & 0xC0 == 0x80

    def _on_control(self, data, addr):
        if addr[0] != self.remote_ip or len(data) < 4 or data[0] & 0xC0 != 0x80:
            return
        if self.remote_control_port and addr[1] != self.remote_control_port:
            return
        if self._control_addr is not None and addr != self._control_addr:
            return
        ptype = data[1] & 0x7F
        retransmission = ptype == 0x56 and self._valid_rtp(data[4:])
        sync = ptype == 0x54 and len(data) >= 20
        if not (retransmission or sync):
            return
        # Learn an unadvertised control port only from a valid packet sent by
        # the negotiated peer. Invalid datagrams must not pin the destination.
        self._control_addr = addr
        if retransmission:
            # Retransmitted packet (RTP packet starts at offset 4).
            self._enqueue(data[4:])
        elif sync:
            self.sync_rtp = struct.unpack_from(">I", data, 4)[0]
            self.sync_ntp = struct.unpack_from(">Q", data, 8)[0]

    def _enqueue(self, packet: bytes):
        if len(packet) < 12:
            return
        if len(packet) == 12:
            return  # empty packet sent before the stream starts
        seq, rtp_ts = struct.unpack_from(">HI", packet, 2)
        if self._next_seq is None:
            self._next_seq = seq
        else:
            behind = seq_diff(seq, self._next_seq)
            if behind < -8 * REORDER_WINDOW:
                # Far behind: the sender restarted its sequence numbers.
                self._pending.clear()
                self._requested.clear()
                self._next_seq = seq
            elif behind < 0:
                return  # late duplicate or already skipped
        if seq in self._pending:
            return  # redundant copy
        if len(packet) == 16 and packet[12:16] == NO_DATA_MARKER:
            payload = None
        elif self.ct == 2 and len(packet) == 44:
            payload = None  # ALAC stream-format packet without audio
        else:
            payload = self.decrypt(packet)
        self._pending[seq] = (rtp_ts, payload)
        self._drain()

    def _drain(self):
        while self._pending:
            item = self._pending.pop(self._next_seq, None)
            if item is None:
                # Gap: wait for a retransmission unless we are too far behind.
                newest = max(self._pending, key=lambda s: seq_diff(s, self._next_seq))
                gap = seq_diff(newest, self._next_seq)
                if gap < REORDER_WINDOW:
                    self._request_resend(gap)
                    return
                # Give up on the missing packets: skip to the oldest one held.
                oldest = min(self._pending, key=lambda s: seq_diff(s, self._next_seq))
                self.lost += seq_diff(oldest, self._next_seq)
                self._next_seq = oldest
                continue
            self._requested.discard(self._next_seq)
            self._next_seq = (self._next_seq + 1) % SEQ_MOD
            rtp_ts, payload = item
            if payload:
                self.packets += 1
                self.handler.audio_frame(payload, rtp_ts)

    def _request_resend(self, gap):
        if not self._control or self._next_seq in self._requested:
            return
        addr = self._control_addr
        if addr is None and self.remote_control_port:
            addr = (self.remote_ip, self.remote_control_port)
        if addr is None:
            return
        count = 0
        while count < gap and ((self._next_seq + count) % SEQ_MOD) not in self._pending:
            count += 1
        for i in range(count):
            self._requested.add((self._next_seq + i) % SEQ_MOD)
        self._resend_seq = (self._resend_seq + 1) % SEQ_MOD
        packet = struct.pack(">BBHHH", 0x80, 0x55 | 0x80, self._resend_seq, self._next_seq, count)
        try:
            self._control.transport.sendto(packet, addr)
        except OSError as exc:
            log.debug("resend request failed: %s", exc)

    def flush(self, next_seq=None):
        self._pending.clear()
        self._requested.clear()
        self._next_seq = next_seq
        self.handler.audio_flush()

    def stop(self):
        for ep in (self._data, self._control):
            if ep and ep.transport:
                ep.transport.close()
        self._data = self._control = None
