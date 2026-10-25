"""Minimal RTSP/HTTP message parsing for the AirPlay control connection."""

import asyncio

MAX_HEADER_BYTES = 16 * 1024
MAX_HEADERS = 40
MAX_BODY_BYTES = 8 * 1024 * 1024


class ProtocolError(Exception):
    pass


class Request:
    def __init__(self, method, url, protocol, headers, body):
        self.method = method
        self.url = url
        self.protocol = protocol
        self.headers = headers  # lower-cased names
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    @property
    def path(self):
        return self.url.split("?", 1)[0]

    def __repr__(self):
        return "<Request %s %s %s (%d bytes)>" % (self.method, self.url, self.protocol, len(self.body))


async def read_request(reader: asyncio.StreamReader):
    """Read one request; returns None on a clean EOF."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial.strip():
            return None
        raise ProtocolError("connection closed mid-request") from exc
    except asyncio.LimitOverrunError as exc:
        raise ProtocolError("request header too large") from exc
    if len(head) > MAX_HEADER_BYTES:
        raise ProtocolError("request header too large")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ProtocolError("malformed request line: %r" % lines[0])
    method, url, protocol = parts
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ProtocolError("malformed header: %r" % line)
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
        if len(headers) > MAX_HEADERS:
            raise ProtocolError("too many headers")
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise ProtocolError("bad Content-Length") from exc
    if length < 0 or length > MAX_BODY_BYTES:
        raise ProtocolError("body too large")
    body = await reader.readexactly(length) if length else b""
    return Request(method, url, protocol, headers, body)


class Response:
    def __init__(self, protocol="RTSP/1.0", status=200, reason="OK"):
        self.protocol = protocol
        self.status = status
        self.reason = reason
        self.headers = []
        self.body = b""
        self.close_after = False

    def set_status(self, status, reason):
        self.status = status
        self.reason = reason

    def add_header(self, name, value):
        self.headers.append((name, str(value)))

    def set_body(self, body: bytes, content_type: str):
        self.body = body
        self.add_header("Content-Type", content_type)

    def encode(self) -> bytes:
        lines = ["%s %d %s" % (self.protocol, self.status, self.reason)]
        lines += ["%s: %s" % h for h in self.headers]
        if self.body or any(name.lower() == "content-type" for name, _ in self.headers):
            lines.append("Content-Length: %d" % len(self.body))
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + self.body
