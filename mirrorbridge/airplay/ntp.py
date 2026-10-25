"""NTP-style timing exchange with the AirPlay sender.

In AirPlay 1 the receiver periodically sends timing requests to the sender's
timing port and the sender replies with its clock. We use the replies to
estimate the offset between the sender's clock and ours.
"""

import asyncio
import logging
import struct
import time

log = logging.getLogger(__name__)

NTP_EPOCH_OFFSET = 2208988800  # seconds from 1900 to 1970
REQUEST_INTERVAL = 3.0


def ntp_now() -> int:
    """Current local time as a 64-bit NTP timestamp (32.32 fixed point)."""
    t = time.time()
    secs = int(t)
    frac = int((t - secs) * (1 << 32)) & 0xFFFFFFFF
    return ((secs + NTP_EPOCH_OFFSET) << 32) | frac


def ntp_to_seconds(ts: int) -> float:
    return (ts >> 32) + (ts & 0xFFFFFFFF) / (1 << 32)


class NtpClient(asyncio.DatagramProtocol):
    def __init__(self, remote_ip: str, remote_port: int):
        self.remote = (remote_ip, remote_port)
        self.transport = None
        self.offset = None  # sender clock minus local clock, in seconds
        self.delay = None
        self._task = None
        self._last_t3 = 0
        self._last_recv = 0

    async def start(self, host="0.0.0.0", port=0) -> int:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        self._task = loop.create_task(self._run())
        return self.transport.get_extra_info("sockname")[1]

    async def _run(self):
        while True:
            self.send_request()
            await asyncio.sleep(REQUEST_INTERVAL)

    def send_request(self):
        if not self.transport or not self.remote[1]:
            return
        packet = struct.pack(">BBHIQQQ", 0x80, 0xD2, 0x0007, 0, self._last_t3, self._last_recv, ntp_now())
        try:
            self.transport.sendto(packet, self.remote)
        except OSError as exc:
            log.debug("ntp send failed: %s", exc)

    def datagram_received(self, data, addr):
        t4 = ntp_now()
        if len(data) < 32 or (data[1] & 0x7F) != 0x53:
            return
        t1, t2, t3 = struct.unpack(">QQQ", data[8:32])
        self._last_t3 = t3
        self._last_recv = t4
        # Remote timestamps count from the sender's boot, plus the 1900 offset.
        f1, f2, f3, f4 = (ntp_to_seconds(x) for x in (t1, t2, t3, t4))
        self.offset = ((f2 - f1) + (f3 - f4)) / 2.0
        self.delay = (f4 - f1) - (f3 - f2)

    def error_received(self, exc):
        log.debug("ntp socket error: %s", exc)

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self.transport:
            self.transport.close()
            self.transport = None
