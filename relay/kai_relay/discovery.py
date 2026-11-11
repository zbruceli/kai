"""Announce the relay on the LAN, so a new Stick finds it without being told an address.

mDNS / DNS-SD service `_kai._tcp`: the Stick asks for it when it has no relay address of its own. The
record carries the port, whether that port is the encrypted link (`tls=1`), the WebSocket path and the
relay's version. Uses python-zeroconf (pure Python, so no avahi and no sudo on the server).

Discovery isn't trust: anyone on the network could announce a `_kai._tcp` service, but the TLS-PSK
handshake still needs the Stick's own key, so a fake relay learns nothing and gets nothing.
"""

import contextlib
import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger("kai.discovery")

SERVICE = "_kai._tcp.local."


def service_info(ip: str, port: int, tls: bool, version: str, hostname: str | None = None) -> ServiceInfo:
    host = (hostname or socket.gethostname()).split(".")[0] or "relay"
    return ServiceInfo(
        SERVICE,
        f"Kai relay on {host}.{SERVICE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"v": version, "tls": "1" if tls else "0", "path": "/ws"},
        server=f"{host}.local.",
    )


@contextlib.asynccontextmanager
async def announce(ip: str, port: int, tls: bool, version: str):
    """Announce while the relay runs; withdraw the record on the way out."""
    info = service_info(ip, port, tls, version)
    # Only on the LAN address: not on Docker's bridge (where Hermes runs) or other virtual interfaces.
    zc = AsyncZeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
    try:
        await zc.async_register_service(info)
        log.info("announced on the LAN as %r (%s:%d, tls=%s)", info.name.removesuffix("." + SERVICE), ip, port,
                 "1" if tls else "0")
        yield info
    finally:
        with contextlib.suppress(Exception):
            await zc.async_unregister_service(info)
        await zc.async_close()
