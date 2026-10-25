"""AirPlay receiver: the RTSP control connection and session lifecycle.

One :class:`AirPlaySession` exists per TCP control connection from a sender.
The application supplies a ``handler_factory(session)`` that returns a
:class:`SessionHandler`; the session calls the handler from the asyncio thread
when video/audio/metadata arrive. Handlers must not block.
"""

import asyncio
import hashlib
import logging
import os
import plistlib
import re
from dataclasses import dataclass, field

from .audio import AudioStream, CODEC_NAMES
from .fairplay import FairPlay, FairPlayError
from .mirror import MirrorStream
from .ntp import NtpClient
from .pairing import PairingError, PairingIdentity, PairingSession
from .rtsp import ProtocolError, Response, read_request

log = logging.getLogger(__name__)

SERVER_VERSION = "220.68"
MODEL = "AppleTV3,2"
PI = "2e388006-13ba-4041-9a67-25dd4a43d536"
DISPLAY_UUID = "e0ff8a27-6738-3d56-8a16-cc53aacee925"
BPLIST = "application/x-apple-binary-plist"

# Feature bits advertised to senders (see UxPlay's start_dnssd for notes).
_FEATURE_BITS = {
    0: 0,   # video (HLS) playback
    1: 1,   # photo
    2: 1,   # video protected with FairPlay
    3: 0,   # volume control for videos
    4: 0,   # HLS
    5: 1,   # slideshow
    6: 1,
    7: 1,   # screen mirroring
    8: 0,   # screen rotation
    9: 1,   # audio
    10: 1,
    11: 1,  # audio packet redundancy
    12: 1,  # FairPlay secure auth
    13: 1,  # photo preloading
    14: 1,  # FairPlay authentication
    15: 1,  # metadata: artwork
    16: 1,  # metadata: progress
    17: 1,  # metadata: text
    18: 1,  # audio formats
    19: 1,
    20: 1,
    21: 1,
    22: 1,  # authentication type 4 (FairPlay)
    23: 0,  # RSA authentication
    24: 0,
    25: 1,
    26: 0,
    27: 1,  # legacy pairing
    28: 1,
    29: 0,
    30: 1,  # RAOP
    31: 0,
}
FEATURE_SCREEN_MULTI_CODEC = 42  # allows H.265 mirroring


def compute_features(h265: bool) -> int:
    features = 0
    for bit, on in _FEATURE_BITS.items():
        if on:
            features |= 1 << bit
    if h265:
        features |= 1 << FEATURE_SCREEN_MULTI_CODEC
    return features


def features_string(features: int) -> str:
    return "0x%X,0x%X" % (features & 0xFFFFFFFF, features >> 32)


@dataclass
class ReceiverSettings:
    name: str = "MirrorBridge"
    port: int = 7100
    width: int = 1920
    height: int = 1080
    refresh_rate: int = 60
    max_fps: int = 60
    h265: bool = False
    password: str = ""
    audio_latency_ms: int = 250
    max_sessions: int = 4

    @property
    def features(self) -> int:
        return compute_features(self.h265)


@dataclass
class ClientInfo:
    name: str = "AirPlay device"
    model: str = ""
    device_id: str = ""
    os_version: str = ""
    user_agent: str = ""
    address: str = ""
    mirroring: bool = False
    extra: dict = field(default_factory=dict)


class SessionHandler:
    """Receives media and events for one session (all methods optional)."""

    def started(self, info: ClientInfo): pass
    def video_started(self): pass
    def video_format(self, codec: str, config: bytes, width: int, height: int): pass
    def video_packet(self, data: bytes, timestamp_ns: int, keyframe: bool): pass
    def video_paused(self, paused: bool): pass
    def video_stopped(self): pass
    def audio_started(self, codec: str, spf: int): pass
    def audio_frame(self, payload: bytes, rtp_timestamp: int): pass
    def audio_flush(self): pass
    def audio_stopped(self): pass
    def volume(self, db: float): pass
    def metadata(self, items: dict): pass
    def cover_art(self, data: bytes, mime: str): pass
    def progress(self, start: int, current: int, end: int): pass
    def stopped(self): pass


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _digest_params(header: str) -> dict:
    params = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', header):
        params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return params


def verify_digest(method: str, authorization: str, password: str, nonce: str) -> bool:
    """RFC 2617 digest check, as used by AirPlay password protection."""
    if not authorization or not authorization.startswith("Digest"):
        return False
    p = _digest_params(authorization[6:])
    if p.get("nonce") != nonce:
        return False
    ha1 = _md5_hex("%s:%s:%s" % (p.get("username", ""), p.get("realm", ""), password))
    ha2 = _md5_hex("%s:%s" % (method, p.get("uri", "")))
    if p.get("qop"):
        expected = _md5_hex("%s:%s:%s:%s:%s:%s" % (ha1, nonce, p.get("nc", ""), p.get("cnonce", ""),
                                                   p["qop"], ha2))
    else:
        expected = _md5_hex("%s:%s:%s" % (ha1, nonce, ha2))
    return expected == p.get("response", "").lower()


def parse_dmap(data: bytes) -> dict:
    """Decode the DAAP "now playing" metadata sent with SET_PARAMETER."""
    names = {b"minm": "title", b"asar": "artist", b"asal": "album", b"asgn": "genre",
             b"ascp": "composer", b"asaa": "album_artist"}
    items = {}

    def walk(buf):
        pos = 0
        while pos + 8 <= len(buf):
            tag = buf[pos:pos + 4]
            size = int.from_bytes(buf[pos + 4:pos + 8], "big")
            value = buf[pos + 8:pos + 8 + size]
            if tag == b"mlit":
                walk(value)
            elif tag in names:
                items[names[tag]] = value.decode("utf-8", "replace")
            pos += 8 + size

    walk(data)
    return items


class AirPlaySession:
    def __init__(self, server: "AirPlayServer", reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.remote_ip = peer[0]
        self.pairing = PairingSession(server.identity)
        self.fairplay = FairPlay()
        self.aeskey = None
        self.aesiv = None
        self.ntp = None
        self.mirror = None
        self.audio = None
        self.handler = None
        self.info = ClientInfo(address=self.remote_ip)
        self.authenticated = not server.settings.password
        self.nonce = None
        self.volume_db = 0.0
        self.closed = False

    @property
    def settings(self) -> ReceiverSettings:
        return self.server.settings

    # -- connection loop -------------------------------------------------

    async def run(self):
        log.info("connection from %s", self.remote_ip)
        try:
            while not self.closed:
                request = await read_request(self.reader)
                if request is None:
                    break
                response = await self.dispatch(request)
                if response is None:
                    continue
                self.writer.write(response.encode())
                await self.writer.drain()
                if response.close_after:
                    break
        except (ProtocolError, ConnectionError, asyncio.IncompleteReadError) as exc:
            log.info("connection from %s ended: %s", self.remote_ip, exc)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("session error")
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._stop_video()
        self._stop_audio()
        if self.ntp:
            self.ntp.stop()
            self.ntp = None
        try:
            self.writer.close()
        except Exception:
            pass
        if self.handler:
            handler, self.handler = self.handler, None
            handler.stopped()
        self.server.session_closed(self)
        log.info("session with %s (%s) closed", self.info.name, self.remote_ip)

    async def dispatch(self, req):
        cseq = req.header("CSeq")
        if req.protocol != "RTSP/1.0" or cseq is None:
            # AirPlay video (HLS) requests use HTTP/1.1; we do not offer them.
            resp = Response(req.protocol, 501, "Not Implemented")
            return resp
        resp = Response("RTSP/1.0")
        if req.method != "RECORD":
            resp.add_header("Audio-Jack-Status", "connected; type=digital")
        try:
            handler = self._route(req)
            if handler is None:
                log.info("unhandled request %s %s", req.method, req.url)
                resp.set_status(501, "Not Implemented")
            else:
                result = handler(req, resp)
                if asyncio.iscoroutine(result):
                    await result
        except (PairingError, FairPlayError) as exc:
            log.warning("%s %s failed: %s", req.method, req.path, exc)
            resp.set_status(470, "Connection Authorization Required")
            resp.close_after = True
        except (ValueError, KeyError, plistlib.InvalidFileException) as exc:
            log.warning("bad %s %s: %s", req.method, req.path, exc)
            resp.set_status(400, "Bad Request")
        resp.add_header("Server", "AirTunes/" + SERVER_VERSION)
        resp.add_header("CSeq", cseq)
        return resp

    def _route(self, req):
        m, path = req.method, req.path
        if m == "GET" and path == "/info":
            return self.handle_info
        if m == "POST":
            return {
                "/pair-setup": self.handle_pair_setup,
                "/pair-verify": self.handle_pair_verify,
                "/fp-setup": self.handle_fp_setup,
                "/feedback": self.handle_ok,
                "/audioMode": self.handle_ok,
            }.get(path)
        return {
            "OPTIONS": self.handle_options,
            "SETUP": self.handle_setup,
            "RECORD": self.handle_record,
            "GET_PARAMETER": self.handle_get_parameter,
            "SET_PARAMETER": self.handle_set_parameter,
            "FLUSH": self.handle_flush,
            "TEARDOWN": self.handle_teardown,
        }.get(m)

    # -- simple handlers -------------------------------------------------

    def handle_ok(self, req, resp):
        pass

    def handle_options(self, req, resp):
        resp.add_header("Public", "SETUP, RECORD, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER")

    def handle_info(self, req, resp):
        qualifier = None
        if req.body and BPLIST in (req.header("Content-Type") or ""):
            q = plistlib.loads(req.body).get("qualifier")
            if isinstance(q, list) and q:
                qualifier = q[0]
        if qualifier in ("txtAirPlay", "txtRAOP"):
            txt = self.server.txt_records()[qualifier]
            resp.set_body(plistlib.dumps({qualifier: txt}, fmt=plistlib.FMT_BINARY), BPLIST)
            return
        resp.set_body(plistlib.dumps(self.server.info_dict(), fmt=plistlib.FMT_BINARY), BPLIST)

    def handle_pair_setup(self, req, resp):
        resp.set_body(self.pairing.pair_setup(req.body), "application/octet-stream")

    def handle_pair_verify(self, req, resp):
        resp.set_body(self.pairing.pair_verify(req.body), "application/octet-stream")

    def handle_fp_setup(self, req, resp):
        resp.set_body(self.fairplay.setup(req.body), "application/octet-stream")

    def handle_record(self, req, resp):
        latency = self.settings.audio_latency_ms * 44100 // 1000
        resp.add_header("Audio-Latency", latency)
        resp.add_header("Audio-Jack-Status", "connected; type=analog")

    def handle_get_parameter(self, req, resp):
        if req.body.strip() == b"volume":
            resp.set_body(b"volume: %.6f\r\n" % self.volume_db, "text/parameters")

    def handle_set_parameter(self, req, resp):
        ctype = req.header("Content-Type") or ""
        if not self.handler:
            return
        if ctype == "text/parameters":
            text = req.body.decode("utf-8", "replace")
            for line in text.splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                if key == "volume":
                    self.volume_db = max(-144.0, min(0.0, float(value)))
                    self.handler.volume(self.volume_db)
                elif key == "progress":
                    start, cur, end = (int(x) for x in value.split("/"))
                    self.handler.progress(start, cur, end)
        elif ctype in ("image/jpeg", "image/png"):
            self.handler.cover_art(req.body, ctype)
        elif ctype == "application/x-dmap-tagged":
            self.handler.metadata(parse_dmap(req.body))

    def handle_flush(self, req, resp):
        next_seq = None
        rtp_info = req.header("RTP-Info") or ""
        m = re.search(r"seq=(\d+)", rtp_info)
        if m:
            next_seq = int(m.group(1)) & 0xFFFF
        if self.audio:
            self.audio.flush(next_seq)

    def handle_teardown(self, req, resp):
        types = set()
        if req.body:
            for s in plistlib.loads(req.body).get("streams", []):
                types.add(s.get("type"))
        resp.add_header("Connection", "close")
        if 96 in types:
            self._stop_audio()
        elif 110 in types:
            self._stop_video()
        else:
            # Whole session torn down; the sender may start a new one on this
            # connection, so keep it open and let the sender close it.
            self._stop_video()
            self._stop_audio()
            if self.ntp:
                self.ntp.stop()
                self.ntp = None
            if self.handler:
                handler, self.handler = self.handler, None
                handler.stopped()

    # -- SETUP -----------------------------------------------------------

    async def handle_setup(self, req, resp):
        body = plistlib.loads(req.body) if req.body else {}
        if "ekey" in body and "eiv" in body:
            if not self._check_auth(req, resp):
                return
            await self._setup_session(req, body, resp)
            if resp.status != 200:
                return
        reply = {}
        if self.ntp is not None and "ekey" in body:
            reply["eventPort"] = 0
            reply["timingPort"] = self.ntp.transport.get_extra_info("sockname")[1]
        streams = body.get("streams")
        if isinstance(streams, list):
            if self.aeskey is None:
                raise ValueError("stream SETUP before session SETUP")
            out = []
            for stream in streams:
                stype = stream.get("type")
                if stype == 110:
                    out.append(await self._setup_video(stream))
                elif stype == 96:
                    out.append(await self._setup_audio(stream))
                else:
                    log.warning("SETUP of unknown stream type %r", stype)
            reply["streams"] = out
        resp.set_body(plistlib.dumps(reply, fmt=plistlib.FMT_BINARY), BPLIST)

    def _check_auth(self, req, resp) -> bool:
        if self.authenticated:
            return True
        password = self.settings.password
        if self.nonce and verify_digest(req.method, req.header("Authorization"), password, self.nonce):
            self.authenticated = True
            return True
        self.nonce = os.urandom(16).hex()
        resp.set_status(401, "Unauthorized")
        resp.add_header("WWW-Authenticate", 'Digest realm="raop", nonce="%s"' % self.nonce)
        return False

    async def _setup_session(self, req, body, resp):
        ekey, eiv = body["ekey"], body["eiv"]
        if len(ekey) != 72 or len(eiv) != 16:
            raise ValueError("invalid ekey/eiv sizes")
        aeskey = self.fairplay.decrypt_key(ekey)
        user_agent = req.header("User-Agent") or ""
        old_protocol = "AirMyPC" in user_agent
        if self.pairing.started and not old_protocol:
            secret = self.pairing.ecdh_secret or bytes(32)
            aeskey = hashlib.sha512(aeskey + secret).digest()[:16]
        self.aeskey = aeskey
        self.aesiv = eiv

        self.info = ClientInfo(
            name=body.get("name") or "AirPlay device",
            model=body.get("model", ""),
            device_id=body.get("deviceID", ""),
            os_version=body.get("osVersion", ""),
            user_agent=user_agent,
            address=self.remote_ip,
            mirroring=bool(body.get("isScreenMirroringSession", False)),
            extra={k: v for k, v in body.items() if isinstance(v, (str, int, float, bool))},
        )
        if not self.server.admit(self):
            resp.set_status(453, "Not Enough Bandwidth")
            resp.close_after = True
            return

        timing_port = int(body.get("timingPort", 0) or 0)
        if self.ntp:
            self.ntp.stop()
        self.ntp = NtpClient(self.remote_ip, timing_port)
        await self.ntp.start()

        if self.handler is None:
            self.handler = self.server.create_handler(self)
            self.handler.started(self.info)
        log.info("session SETUP from %s (%s, %s) mirroring=%s", self.info.name, self.info.model,
                 self.remote_ip, self.info.mirroring)

    async def _setup_video(self, stream):
        self._stop_video()
        stream_id = int(stream.get("streamConnectionID", 0))
        self.mirror = MirrorStream(self.aeskey, stream_id, self.handler)
        port = await self.mirror.start()
        self.handler.video_started()
        return {"type": 110, "dataPort": port}

    async def _setup_audio(self, stream):
        self._stop_audio()
        ct = int(stream.get("ct", 0) or 0)
        spf = int(stream.get("spf", 0) or 0)
        if not ct:
            ct = 8 if self.info.mirroring else 2
        if not spf:
            spf = {2: 352, 4: 1024, 8: 480}.get(ct, 352)
        remote_control = int(stream.get("controlPort", 0) or 0)
        self.audio = AudioStream(self.aeskey, self.aesiv, ct, spf, self.handler,
                                 self.remote_ip, remote_control)
        data_port, control_port = await self.audio.start()
        self.handler.audio_started(CODEC_NAMES.get(ct, "unknown"), spf)
        log.info("audio stream: %s spf=%d", CODEC_NAMES.get(ct, ct), spf)
        return {"type": 96, "dataPort": data_port, "controlPort": control_port}

    def _stop_video(self):
        if self.mirror:
            self.mirror.stop()
            self.mirror = None
            if self.handler:
                self.handler.video_stopped()

    def _stop_audio(self):
        if self.audio:
            self.audio.stop()
            self.audio = None
            if self.handler:
                self.handler.audio_stopped()


class AirPlayServer:
    def __init__(self, settings: ReceiverSettings, identity: PairingIdentity, device_id: bytes,
                 handler_factory):
        self.settings = settings
        self.identity = identity
        self.device_id = device_id
        self.handler_factory = handler_factory
        self.sessions = set()
        self._server = None

    @property
    def device_id_str(self) -> str:
        return ":".join("%02x" % b for b in self.device_id)

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1] if self._server else 0

    async def start(self, host="0.0.0.0"):
        self._server = await asyncio.start_server(self._on_connect, host, self.settings.port)
        log.info("AirPlay receiver listening on port %d", self.port)

    async def _on_connect(self, reader, writer):
        session = AirPlaySession(self, reader, writer)
        self.sessions.add(session)
        await session.run()

    def admit(self, session: AirPlaySession) -> bool:
        """Decide whether a newly set-up session may stream."""
        # A device reconnecting replaces its own previous session.
        for other in list(self.sessions):
            if other is not session and other.handler and other.info.device_id == session.info.device_id \
                    and session.info.device_id:
                log.info("replacing previous session of %s", session.info.name)
                other.close()
        active = [s for s in self.sessions if s is not session and s.handler]
        return len(active) < self.settings.max_sessions

    def create_handler(self, session) -> SessionHandler:
        return self.handler_factory(session)

    def session_closed(self, session):
        self.sessions.discard(session)

    async def stop(self):
        for s in list(self.sessions):
            s.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- advertisement data ---------------------------------------------

    def txt_records(self) -> dict:
        from .mdns import encode_txt
        return {
            "txtAirPlay": encode_txt(self.airplay_txt()),
            "txtRAOP": encode_txt(self.raop_txt()),
        }

    def airplay_txt(self) -> dict:
        s = self.settings
        return {
            "deviceid": self.device_id_str,
            "features": features_string(s.features),
            "flags": "0x4",
            "model": MODEL,
            "pk": self.identity.public_key.hex(),
            "pi": PI,
            "srcvers": SERVER_VERSION,
            "vv": "2",
            "pw": "true" if s.password else "false",
        }

    def raop_txt(self) -> dict:
        s = self.settings
        return {
            "ch": "2",
            "cn": "0,1,2,3",
            "da": "true",
            "et": "0,3,5",
            "vv": "2",
            "ft": features_string(s.features),
            "am": MODEL,
            "md": "0,1,2",
            "rhd": "5.6.0.0",
            "pw": "true" if s.password else "false",
            "sf": "0x84" if s.password else "0x4",
            "sr": "44100",
            "ss": "16",
            "sv": "false",
            "tp": "UDP",
            "txtvers": "1",
            "vs": SERVER_VERSION,
            "vn": "65537",
            "pk": self.identity.public_key.hex(),
        }

    def info_dict(self) -> dict:
        s = self.settings
        audio_formats = [{"type": t, "audioInputFormats": 0x3FFFFFC, "audioOutputFormats": 0x3FFFFFC}
                         for t in (100, 101)]
        audio_latencies = [{"type": t, "audioType": "default", "inputLatencyMicros": 0,
                            "outputLatencyMicros": False} for t in (100, 101)]
        return {
            "deviceID": self.device_id_str,
            "macAddress": self.device_id_str,
            "pk": self.identity.public_key,
            "features": s.features,
            "name": s.name,
            "pi": PI,
            "vv": 2,
            "statusFlags": 68,
            "keepAliveLowPower": True,
            "sourceVersion": SERVER_VERSION,
            "keepAliveSendStatsAsBody": True,
            "model": MODEL,
            "initialVolume": 0.0,
            "audioLatencies": audio_latencies,
            "audioFormats": audio_formats,
            "displays": [{
                "uuid": DISPLAY_UUID,
                "widthPhysical": 0,
                "heightPhysical": 0,
                "width": s.width,
                "height": s.height,
                "widthPixels": s.width,
                "heightPixels": s.height,
                "rotation": False,
                "refreshRate": 1.0 / s.refresh_rate,
                "maxFPS": s.max_fps,
                "overscanned": False,
                "features": 14,
            }],
        }
