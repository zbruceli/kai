import asyncio
import contextlib
import hmac
import logging
import os
import socket
from importlib.metadata import version

import httpx
from google import genai
from websockets.asyncio.server import ServerConnection, serve

from . import discovery, mcp_server, pairing, tls, tools
from .agent import AgentHub, AgentStore
from .schedule import Scheduler
from .config import load_settings
from .hermes import HermesClient
from .session import KaiSession, close_revoked

log = logging.getLogger("kai")

MAX_CONNECTIONS = 4   # Sticks plus a simulator; each one costs a Gemini Live session
MAX_MESSAGE = 64 * 1024  # the Stick sends at most 4 KB; raw-PCM clients a few KB per frame


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outbound interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def device_authorized(ws, expected_auth: str) -> bool:
    """Over the encrypted link the TLS-PSK handshake has already proved the device's own key, which is
    what identifies it, so no shared token is needed (Sticks set up without secrets.h don't have one).
    The token still guards the plain ws:// fallback for relays without device keys."""
    if ws.transport.get_extra_info("ssl_object") is not None:
        return True
    return hmac.compare_digest(ws.request.headers.get("Authorization", ""), expected_auth)


async def run() -> None:
    settings = load_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    notes = tools.NotesStore(settings.data_dir / "kai.db", settings.notes_dir)
    expected_auth = f"Bearer {settings.device_token}"
    devices = pairing.DeviceStore(settings.data_dir / "devices.db")
    devices.import_env(settings.device_psks)

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": f"kai-relay/{version('kai-relay')}"}) as http:
        hermes = None
        if settings.hermes_url and settings.hermes_api_key:
            hermes = HermesClient(settings.hermes_url, settings.hermes_api_key, http)
        store = AgentStore(settings.data_dir / "agent.db")
        hub = AgentHub(store, hermes)
        scheduler = Scheduler(hub, store, hermes) if hermes else None
        ctx = tools.ToolContext(settings=settings, http=http, notes=notes, agent=hub, scheduler=scheduler)
        background: set[asyncio.Task] = set()
        if hermes:
            await hub.start()
            background.add(asyncio.create_task(scheduler.run()))
            log.info("background brain: Hermes at %s (%s)", settings.hermes_url,
                     "healthy" if await hermes.health() else "NOT responding yet")
            if settings.mcp_token:
                background.add(asyncio.create_task(mcp_server.serve(ctx, hub, settings.mcp_port, settings.mcp_token)))
            else:
                log.warning("KAI_MCP_TOKEN not set: Hermes can't reach back into Kai (kai_notify, data tools)")

        connections = 0

        async def handler(ws: ServerConnection) -> None:
            nonlocal connections
            if ws.request.path != "/ws":
                await ws.close(1008, "unknown path")
                return
            if not device_authorized(ws, expected_auth):
                log.warning("rejected device from %s: bad token", ws.remote_address)
                await ws.close(1008, "unauthorized")
                return
            if connections >= MAX_CONNECTIONS:
                log.warning("rejected device from %s: %d connections already", ws.remote_address, connections)
                await ws.close(1013, "too many connections")
                return
            log.info("device connected from %s", ws.remote_address[0])
            connections += 1
            try:
                await KaiSession(ws, client, ctx, settings).run()
            finally:
                connections -= 1

        async with contextlib.AsyncExitStack() as stack:
            listeners = []
            encrypted = tls.supported()
            if encrypted:
                # Keys come from the device store on every handshake, so kai-approve / kai-revoke apply at once.
                listeners.append(await stack.enter_async_context(serve(
                    handler, settings.host, settings.tls_port, ssl=tls.server_context(devices),
                    max_size=MAX_MESSAGE, ping_interval=20)))
                log.info("Kai relay listening on wss://%s:%d/ws (TLS-PSK, %d device(s); model %s)",
                         _lan_ip(), settings.tls_port, devices.approved_count(), settings.model)
                background.add(asyncio.create_task(_watch_revoked(devices)))
            # Plain ws:// (the shared token) only when asked for, or while no device has a key yet.
            if settings.allow_plain or not encrypted or devices.approved_count() == 0:
                listeners.append(await stack.enter_async_context(serve(
                    handler, settings.host, settings.port, max_size=MAX_MESSAGE, ping_interval=20)))
                log.info("Kai relay listening on ws://%s:%d/ws (model %s)", _lan_ip(), settings.port, settings.model)
                log.warning("plain ws:// is on: Sticks on it are unencrypted (pair them, then remove "
                            "KAI_ALLOW_PLAIN_WS)")
            pair_port = None
            if settings.pairing and encrypted:
                listeners.append(await stack.enter_async_context(serve(
                    pairing.PairingService(devices).handle, settings.host, settings.pair_port, max_size=4096)))
                pair_port = settings.pair_port
                log.info("pairing new Sticks on ws://%s:%d/pair (approve with kai-approve <code>)",
                         _lan_ip(), settings.pair_port)
            if settings.announce:
                ip = _lan_ip()
                if ip.startswith("127."):
                    log.warning("no LAN address: not announcing the relay; Sticks need its address set")
                else:
                    try:
                        await stack.enter_async_context(discovery.announce(
                            ip, settings.tls_port if encrypted else settings.port, encrypted, version("kai-relay"),
                            pair_port))
                    except OSError as e:
                        log.warning("couldn't announce the relay on the LAN (%s); Sticks need its address set", e)
            if settings.home_lat is None:
                log.warning("KAI_HOME_LAT/LON not set: 'near me' questions will fail")
            if not settings.maps_api_key:
                log.warning("GOOGLE_MAPS_API_KEY not set: parking search disabled")
            await asyncio.gather(*(server.serve_forever() for server in listeners))


async def _watch_revoked(devices: "pairing.DeviceStore") -> None:
    """kai-revoke runs in another process: notice it and close that Stick's connection."""
    while True:
        await asyncio.sleep(10)
        try:
            await close_revoked(lambda name: devices.status(name) == "revoked")
        except Exception:
            log.exception("revocation check failed")


def main() -> None:
    os.umask(0o077)  # notes, databases and logs the relay writes are the owner's alone
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
