"""A small RFC 6455 WebSocket implementation on asyncio streams."""

import base64
import hashlib
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 64 * 1024 * 1024

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(Exception):
    pass


def accept_key(client_key: str) -> str:
    return base64.b64encode(hashlib.sha1(client_key.encode() + GUID).digest()).decode()


def handshake_response(client_key: str) -> bytes:
    return ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept_key(client_key)).encode()


def encode_frame(opcode: int, payload: bytes, mask: bytes = None) -> bytes:
    """Encode one final frame (server frames are unmasked; pass mask for client frames)."""
    head = bytearray([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack(">Q", n)
    if mask:
        head += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


class WebSocket:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def _read_frame(self):
        b0, b1 = await self.reader.readexactly(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await self.reader.readexactly(8))[0]
        if n > MAX_MESSAGE:
            raise WebSocketClosed("frame too large")
        mask = await self.reader.readexactly(4) if masked else None
        data = await self.reader.readexactly(n) if n else b""
        if mask:
            # XOR with the repeating 4-byte mask, done on whole integers for speed.
            m = int.from_bytes((mask * (n // 4 + 1))[:n], "little")
            data = (int.from_bytes(data, "little") ^ m).to_bytes(n, "little")
        return fin, opcode, data

    async def receive(self):
        """Return (opcode, payload) for the next complete text/binary message."""
        message = None
        message_op = None
        while True:
            try:
                fin, opcode, data = await self._read_frame()
            except Exception as exc:
                self.closed = True
                raise WebSocketClosed(str(exc)) from exc
            if opcode == OP_CLOSE:
                await self.close()
                raise WebSocketClosed("closed by peer")
            if opcode == OP_PING:
                await self._send(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY):
                message_op, message = opcode, bytearray(data)
            elif opcode == OP_CONT and message is not None:
                message += data
                if len(message) > MAX_MESSAGE:
                    raise WebSocketClosed("message too large")
            if fin and message is not None:
                return message_op, bytes(message)

    async def _send(self, opcode, payload):
        if self.closed:
            raise WebSocketClosed("closed")
        self.writer.write(encode_frame(opcode, payload))
        await self.writer.drain()

    async def send_text(self, text: str):
        await self._send(OP_TEXT, text.encode("utf-8"))

    async def close(self, code=1000):
        if self.closed:
            return
        try:
            self.writer.write(encode_frame(OP_CLOSE, struct.pack(">H", code)))
            await self.writer.drain()
        except Exception:
            pass
        self.closed = True
        self.writer.close()
