import asyncio
import hmac
import logging
import socket

import httpx
from google import genai
from websockets.asyncio.server import ServerConnection, serve

from . import tools
from .config import load_settings
from .session import KaiSession

log = logging.getLogger("kai")


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

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "kai-relay/0.1"}) as http:
        ctx = tools.ToolContext(settings=settings, http=http, notes=notes)

        async def handler(ws: ServerConnection) -> None:
            if ws.request.path != "/ws":
                await ws.close(1008, "unknown path")
                return
            if not hmac.compare_digest(ws.request.headers.get("Authorization", ""), expected_auth):
                log.warning("rejected device from %s: bad token", ws.remote_address)
                await ws.close(1008, "unauthorized")
                return
            log.info("device connected from %s", ws.remote_address[0])
            await KaiSession(ws, client, ctx, settings).run()

        async with serve(handler, settings.host, settings.port, max_size=2**20, ping_interval=20) as server:
            log.info("Kai relay listening on ws://%s:%d/ws (model %s)", _lan_ip(), settings.port, settings.model)
            if settings.home_lat is None:
                log.warning("KAI_HOME_LAT/LON not set: 'near me' questions will fail")
            if not settings.maps_api_key:
                log.warning("GOOGLE_MAPS_API_KEY not set: parking search disabled")
            await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
