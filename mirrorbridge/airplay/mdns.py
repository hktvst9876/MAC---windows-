"""Bonjour/mDNS advertisement of the receiver (_airplay._tcp and _raop._tcp)."""

import asyncio
import ipaddress
import logging

import ifaddr
from zeroconf import InterfaceChoice, IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger(__name__)

AIRPLAY_TYPE = "_airplay._tcp.local."
RAOP_TYPE = "_raop._tcp.local."


def encode_txt(records: dict) -> bytes:
    """DNS TXT record wire format (length-prefixed key=value strings)."""
    out = b""
    for key, value in records.items():
        item = ("%s=%s" % (key, value)).encode("utf-8")[:255]
        out += bytes([len(item)]) + item
    return out


def local_ipv4_addresses():
    addrs = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6
            try:
                addr = ipaddress.IPv4Address(ip.ip)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_unspecified:
                continue
            addrs.append(str(addr))
    return sorted(set(addrs))


MAX_NAME_BYTES = 50  # DNS labels hold 63 bytes; the RAOP name adds "<12 hex digits>@"


def instance_name(name: str) -> str:
    """The receiver name as it can be advertised (no dots, at most 50 UTF-8 bytes)."""
    name = name.replace(".", " ").strip() or "MirrorBridge"
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name[:-1]
    return name.strip()


class Advertiser:
    def __init__(self, server, interfaces=None):
        self.server = server
        self.interfaces = interfaces
        self._zc = None
        self._infos = []
        self._addresses = []
        self._watch = None

    @property
    def addresses(self):
        return list(self._addresses)

    def _build_infos(self):
        s = self.server.settings
        name = instance_name(s.name)
        hexid = "".join("%02X" % b for b in self.server.device_id)
        host = "mirrorbridge-%s.local." % hexid.lower()
        port = self.server.port
        airplay = ServiceInfo(
            AIRPLAY_TYPE, "%s.%s" % (name, AIRPLAY_TYPE), port=port,
            properties=self.server.airplay_txt(), server=host, parsed_addresses=self._addresses)
        raop = ServiceInfo(
            RAOP_TYPE, "%s@%s.%s" % (hexid, name, RAOP_TYPE), port=port,
            properties=self.server.raop_txt(), server=host, parsed_addresses=self._addresses)
        return [airplay, raop]

    async def start(self):
        self._addresses = local_ipv4_addresses()
        if not self._addresses:
            log.warning("no network interfaces with an IPv4 address found")
        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only, interfaces=self.interfaces or InterfaceChoice.All)
        await self._register()
        self._watch = asyncio.get_running_loop().create_task(self._watch_network())

    async def _register(self):
        self._infos = self._build_infos()
        for info in self._infos:
            await self._zc.async_register_service(info, allow_name_change=True)
        log.info("advertising '%s' on %s:%d", self.server.settings.name,
                 ", ".join(self._addresses) or "?", self.server.port)

    async def _unregister(self):
        for info in self._infos:
            try:
                await self._zc.async_unregister_service(info)
            except Exception as exc:
                log.debug("unregister failed: %s", exc)
        self._infos = []

    async def update(self):
        """Re-announce (after a name/settings change or network change)."""
        if not self._zc:
            return
        await self._unregister()
        self._addresses = local_ipv4_addresses()
        await self._register()

    async def _watch_network(self):
        while True:
            await asyncio.sleep(15)
            current = local_ipv4_addresses()
            if current != self._addresses:
                log.info("network addresses changed: %s", ", ".join(current))
                try:
                    # Recreate the Zeroconf instance so new interfaces are joined.
                    await self._unregister()
                    await self._zc.async_close()
                    self._addresses = current
                    self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only, interfaces=self.interfaces or InterfaceChoice.All)
                    await self._register()
                except Exception:
                    log.exception("re-advertising failed")

    async def stop(self):
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if self._zc:
            await self._unregister()
            await self._zc.async_close()
            self._zc = None

