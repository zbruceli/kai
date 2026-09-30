import asyncio
import contextlib
import hmac
import logging
import os
import socket

import httpx
from google import genai
from websockets.asyncio.server import ServerConnection, serve

from . import mcp_server, tls, tools
from .agent import AgentHub, AgentStore
from .schedule import Scheduler
from .config import load_settings
from .hermes import HermesClient
from .session import KaiSession

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


async def run() -> None:
    settings = load_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    notes = tools.NotesStore(settings.data_dir / "kai.db", settings.notes_dir)
    expected_auth = f"Bearer {settings.device_token}"

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "kai-relay/0.9"}) as http:
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
            if not hmac.compare_digest(ws.request.headers.get("Authorization", ""), expected_auth):
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
            if settings.device_psks:
                ctx_tls = tls.server_context(settings.device_psks)
                listeners.append(await stack.enter_async_context(serve(
                    handler, settings.host, settings.tls_port, ssl=ctx_tls, max_size=MAX_MESSAGE, ping_interval=20)))
                log.info("Kai relay listening on wss://%s:%d/ws (TLS-PSK, %d device key(s); model %s)",
                         _lan_ip(), settings.tls_port, len(settings.device_psks), settings.model)
            if settings.allow_plain:
                listeners.append(await stack.enter_async_context(serve(
                    handler, settings.host, settings.port, max_size=MAX_MESSAGE, ping_interval=20)))
                log.info("Kai relay listening on ws://%s:%d/ws (model %s)", _lan_ip(), settings.port, settings.model)
                if settings.device_psks:
                    log.warning("plain ws:// is still on (KAI_ALLOW_PLAIN_WS=1): turn it off once every device "
                                "uses the encrypted link")
                else:
                    log.warning("no KAI_DEVICE_PSKS: the link to the Stick is unencrypted (see kai-psk)")
            if settings.home_lat is None:
                log.warning("KAI_HOME_LAT/LON not set: 'near me' questions will fail")
            if not settings.maps_api_key:
                log.warning("GOOGLE_MAPS_API_KEY not set: parking search disabled")
            await asyncio.gather(*(server.serve_forever() for server in listeners))


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
