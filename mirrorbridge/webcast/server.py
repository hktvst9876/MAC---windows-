"""Browser casting: an HTTPS page that streams the browser's screen to us.

The page captures the screen with ``getDisplayMedia`` and sends the output of
a ``MediaRecorder`` (WebM with H.264/VP8 video and Opus audio) over a
WebSocket. Each connection becomes a :class:`WebCastSession`, which looks to
the user interface exactly like an AirPlay device.

This covers Chromebooks and desktop browsers. (Google Cast itself cannot be
implemented by third-party receivers: senders require a device certificate
signed by Google. Mobile browsers do not allow screen capture.)
"""

import asyncio
import collections
import json
import logging
import ssl
import threading

import av

from ..airplay.mdns import local_ipv4_addresses
from ..airplay.rtsp import ProtocolError, read_request
from ..airplay.server import ClientInfo
from ..config import config_dir
from ..core.session import DeviceSession
from ..media.audio import SAMPLE_RATE
from .page import render
from .tls import ensure_certificate
from .websocket import OP_BINARY, OP_TEXT, WebSocket, WebSocketClosed, handshake_response

log = logging.getLogger(__name__)


class BlockingStream:
    """File-like object fed from the network thread and read by the demuxer."""

    def __init__(self):
        self._chunks = collections.deque()
        self._cv = threading.Condition()
        self._eof = False
        self.received = 0

    def feed(self, data: bytes):
        with self._cv:
            self._chunks.append(data)
            self.received += len(data)
            self._cv.notify()

    def end(self):
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def read(self, n=-1):
        with self._cv:
            while not self._chunks and not self._eof:
                self._cv.wait()
            if not self._chunks:
                return b""
            if n is None or n < 0:
                out = b"".join(self._chunks)
                self._chunks.clear()
                return out
            out = bytearray()
            while self._chunks and len(out) < n:
                chunk = self._chunks[0]
                take = min(n - len(out), len(chunk))
                out += chunk[:take]
                if take == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
            return bytes(out)


class WebCastSession(DeviceSession):
    def __init__(self, controller, loop, ws, name, user_agent, address, mime):
        super().__init__(controller, None)
        self.loop = loop
        self.ws = ws
        self.mime = mime or ""
        self.info = ClientInfo(name=name or "Web browser", model="Browser", address=address,
                               user_agent=user_agent, mirroring=True)
        self.stream = BlockingStream()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="webcast-demux", daemon=True)

    def begin(self):
        self.controller.session_started(self)
        self._thread.start()

    def feed(self, data: bytes):
        self.stream.feed(data)

    def end_input(self):
        self.stream.end()

    def close(self):
        """Disconnect (called on the network loop)."""
        if self._closed:
            return
        self._closed = True
        self.stream.end()

        async def bye():
            try:
                await self.ws.send_text(json.dumps({"type": "bye"}))
            except Exception:
                pass
            await self.ws.close()

        asyncio.ensure_future(bye())

    def _run(self):
        container = None
        fmt = "mp4" if "mp4" in self.mime else "matroska"
        try:
            container = av.open(self.stream, mode="r", format=fmt,
                                options={"probesize": "65536", "analyzeduration": "500000"})
            for s in container.streams.video:
                s.codec_context.flags |= av.codec.context.Flags.low_delay
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
            for packet in container.demux():
                if self._closed:
                    break
                if packet.size == 0:
                    continue
                kind = packet.stream.type
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError:
                    continue
                for frame in frames:
                    if kind == "video":
                        self._video_frame(frame, packet.stream.codec_context.name)
                    elif kind == "audio":
                        self._audio_frame(frame, resampler, packet.stream.codec_context.name)
        except Exception as exc:
            if not self._closed:
                log.warning("browser cast stream ended: %s", exc)
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self.loop.call_soon_threadsafe(self.close)
            self.stopped()

    def _video_frame(self, frame, codec):
        if not self.has_video:
            self.has_video = True
            self.signals.video_active.emit(True)
        if (frame.width, frame.height) != (self.width, self.height):
            self.codec, self.width, self.height = codec, frame.width, frame.height
            self.signals.video_format.emit(codec, frame.width, frame.height)
        self.decoder.publish(frame)

    def _audio_frame(self, frame, resampler, codec):
        if not self.has_audio:
            self.has_audio = True
            self.audio_codec = codec
            self._ensure_player()
            self.signals.audio_active.emit(True)
        for out in resampler.resample(frame):
            self.deliver_pcm(out.to_ndarray().reshape(-1, 2))


def _quiet_tls_errors(loop, context):
    exc = context.get("exception")
    if isinstance(exc, (ssl.SSLError, ConnectionError)):
        return  # browsers probe/reject the self-signed certificate; not worth logging
    loop.default_exception_handler(context)


class WebCastServer:
    def __init__(self, controller, port=7180):
        self.controller = controller
        self.requested_port = port
        self._server = None
        self.sessions = set()

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1] if self._server else 0

    @property
    def url(self) -> str:
        addrs = local_ipv4_addresses()
        return "https://%s:%d" % (addrs[0] if addrs else "localhost", self.port)

    async def start(self, host="0.0.0.0"):
        cert, key = ensure_certificate(config_dir(), local_ipv4_addresses())
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_quiet_tls_errors)
        try:
            self._server = await asyncio.start_server(self._client, host, self.requested_port, ssl=ctx)
        except OSError:
            self._server = await asyncio.start_server(self._client, host, 0, ssl=ctx)
        log.info("browser casting available at %s", self.url)

    async def stop(self):
        for session in list(self.sessions):
            session.close()
        if self._server:
            self._server.close()
            self._server = None

    async def _client(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        try:
            request = await read_request(reader)
            if request is None:
                return
            if request.path == "/ws" and (request.header("Upgrade") or "").lower() == "websocket":
                writer.write(handshake_response(request.header("Sec-WebSocket-Key", "")))
                await writer.drain()
                await self._websocket(WebSocket(reader, writer), peer)
                return
            if request.method == "GET" and request.path in ("/", "/index.html"):
                c = self.controller.config
                body = render(c.name, bool(c.password))
                self._respond(writer, 200, "OK", body, "text/html; charset=utf-8")
            else:
                self._respond(writer, 404, "Not Found", b"Not found", "text/plain")
            await writer.drain()
        except (ProtocolError, ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        except Exception:
            log.exception("browser cast request failed")
        finally:
            writer.close()

    @staticmethod
    def _respond(writer, status, reason, body, ctype):
        head = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n" % (status, reason, ctype, len(body)))
        writer.write(head.encode() + body)

    async def _websocket(self, ws, peer):
        session = None
        try:
            opcode, data = await ws.receive()
            hello = json.loads(data.decode("utf-8")) if opcode == OP_TEXT else {}
            if hello.get("type") != "hello":
                return
            config = self.controller.config
            if config.password and hello.get("password") != config.password:
                await ws.send_text(json.dumps({"type": "error", "message": "Wrong password."}))
                return
            if len(self.controller.sessions) >= config.max_sessions:
                await ws.send_text(json.dumps({"type": "error",
                                               "message": "The receiver is busy with other devices."}))
                return
            session = WebCastSession(self.controller, asyncio.get_running_loop(), ws,
                                     str(hello.get("name", ""))[:40], str(hello.get("ua", "")), peer,
                                     str(hello.get("mime", "")))
            self.sessions.add(session)
            session.begin()
            await ws.send_text(json.dumps({"type": "ready"}))
            log.info("browser cast from %s (%s, %s)", session.display_name, peer, session.mime)
            while not session._closed:
                opcode, data = await ws.receive()
                if opcode == OP_BINARY:
                    session.feed(data)
        except WebSocketClosed:
            pass
        finally:
            if session is not None:
                session.end_input()
                self.sessions.discard(session)
            await ws.close()
