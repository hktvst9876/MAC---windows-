"""Runs the network services on a background asyncio loop and tracks sessions."""

import asyncio
import logging
import threading

from PySide6.QtCore import QObject, Signal

from ..airplay.mdns import Advertiser, local_ipv4_addresses
from ..airplay.pairing import PairingIdentity
from ..airplay.server import AirPlayServer, ReceiverSettings
from .session import DeviceSession

log = logging.getLogger(__name__)


class Controller(QObject):
    session_added = Signal(object)
    session_removed = Signal(object)
    status_changed = Signal(str, bool)  # message, ok

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.sessions = {}
        self.server = None
        self.advertiser = None
        self.webcast = None
        self.status = ("Starting…", True)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="network", daemon=True)
        self._lock = threading.Lock()

    # -- event loop ------------------------------------------------------

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro, timeout=None):
        """Run a coroutine on the network loop; optionally wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if timeout is not None:
            return future.result(timeout)
        return future

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()
        self.call(self._start_services())

    def _settings(self) -> ReceiverSettings:
        c = self.config
        width, height = c.display_size
        return ReceiverSettings(name=c.name, port=c.port, width=width, height=height,
                                max_fps=c.max_fps, h265=c.use_h265, password=c.password,
                                max_sessions=c.max_sessions)

    def _set_status(self, message, ok=True):
        self.status = (message, ok)
        self.status_changed.emit(message, ok)

    async def _start_services(self):
        identity = PairingIdentity.from_seed(bytes.fromhex(self.config.identity_seed))
        device_id = bytes.fromhex(self.config.device_id)
        settings = self._settings()
        self.server = AirPlayServer(settings, identity, device_id, self._make_handler)
        try:
            await self.server.start()
        except OSError as exc:
            log.warning("port %d unavailable (%s); using a free port", settings.port, exc)
            settings.port = 0
            try:
                await self.server.start()
            except OSError as exc2:
                self._set_status("Cannot start the receiver: %s" % exc2, False)
                return
        try:
            self.advertiser = Advertiser(self.server)
            await self.advertiser.start()
        except Exception as exc:
            log.exception("mDNS advertisement failed")
            self._set_status("Receiver running, but it could not be announced on the network: %s" % exc, False)
            return
        if self.config.webcast_enabled:
            await self._start_webcast()
        self._set_status("Ready", True)

    async def _start_webcast(self):
        try:
            from ..webcast.server import WebCastServer
        except Exception as exc:
            log.info("browser casting unavailable: %s", exc)
            return
        try:
            self.webcast = WebCastServer(self, port=self.config.webcast_port)
            await self.webcast.start()
        except Exception as exc:
            log.warning("browser casting could not start: %s", exc)
            self.webcast = None

    async def _stop_services(self):
        if self.webcast:
            await self.webcast.stop()
            self.webcast = None
        if self.advertiser:
            await self.advertiser.stop()
            self.advertiser = None
        if self.server:
            await self.server.stop()
            self.server = None

    def restart(self):
        """Apply changed settings (name, resolution, port, password…)."""
        async def _restart():
            await self._stop_services()
            await self._start_services()
        self._set_status("Restarting…", True)
        self.call(_restart())

    def shutdown(self):
        for session in list(self.sessions.values()):
            session.stop_recording()
        if self._thread.is_alive():
            try:
                self.call(self._stop_services(), timeout=5)
                self.call(self._cancel_tasks(), timeout=3)
            except Exception as exc:
                log.warning("shutdown: %s", exc)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(3)

    @staticmethod
    async def _cancel_tasks():
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- sessions --------------------------------------------------------

    def _make_handler(self, airplay_session):
        return DeviceSession(self, airplay_session)

    def session_started(self, session):
        with self._lock:
            self.sessions[session.id] = session
        log.info("device connected: %s (%s)", session.display_name, session.info.model)
        self.session_added.emit(session)

    def session_ended(self, session):
        with self._lock:
            self.sessions.pop(session.id, None)
        log.info("device disconnected: %s", session.display_name)
        self.session_removed.emit(session)

    def close_session(self, session):
        closer = getattr(session, "close", None)
        if closer is not None:  # non-AirPlay sessions (browser casting)
            self._loop.call_soon_threadsafe(closer)
        elif session.airplay is not None:
            self._loop.call_soon_threadsafe(session.airplay.close)

    # -- info ------------------------------------------------------------

    @property
    def port(self):
        return self.server.port if self.server else 0

    @property
    def addresses(self):
        if self.advertiser:
            return self.advertiser.addresses
        return local_ipv4_addresses()

    @property
    def advertised_name(self):
        return self.config.name
