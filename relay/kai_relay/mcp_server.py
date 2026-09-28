"""The Kai MCP server: how Hermes reaches back into Kai.

Hermes can't POST to arbitrary URLs (its webhooks are inbound only), but it is an MCP client. This server
runs inside the relay's event loop on 127.0.0.1, behind a bearer token, and offers:
  - kai_notify: put an update in the owner's inbox (spoken now if a Stick is connected, badge otherwise)
  - Kai's fast data tools, read-only (tides, weather, light, notes), so Hermes reuses them instead of
    re-scraping.
"""

import hmac
import logging

import uvicorn
from mcp.server.mcpserver import MCPServer

from . import tools
from .agent import AgentHub

log = logging.getLogger("kai.mcp")


def build(ctx: tools.ToolContext, hub: AgentHub) -> MCPServer:
    mcp = MCPServer(
        name="kai",
        instructions=(
            "Tools of Kai, the owner's pocket voice assistant (an M5StickS3 with a tiny speaker and a 240x135 "
            "screen). Use kai_notify to tell the owner something through Kai. Prefer get_tides, get_weather "
            "and get_sun_times over web search for tides, weather and light: they use NOAA and Open-Meteo "
            "data for exact places and dates."
        ),
    )

    @mcp.tool(description=(
        "Send the owner an update through Kai. It is spoken aloud if Kai is awake, otherwise it waits in "
        "Kai's inbox with a badge. `summary`: one or two short spoken sentences, no markdown or URLs. "
        "`card_title` (<= 22 chars) and `card_lines` (<= 5 lines of <= 26 chars) are shown on the screen. "
        "`details_md` holds the full text. From a watch job whose condition was met, pass `watch` = the watch's "
        "name (e.g. kai-watch-3) so it stops."
    ))
    async def kai_notify(summary: str, card_title: str = "", card_lines: list[str] | None = None,
                         details_md: str = "", watch: str = "") -> str:
        card = None
        if card_title or card_lines:
            card = {"title": (card_title or "Update")[:22], "lines": [str(x)[:26] for x in (card_lines or [])][:5]}
        item = await hub.notify(summary.strip()[:400], card, details_md, source="watch" if watch else "hermes")
        if watch and ctx.scheduler:
            await ctx.scheduler.watch_fired(watch.strip())
        return f"queued as update {item.id}" + (f"; {watch} stopped" if watch else "")

    @mcp.tool(description=(
        "Replace the short brief Kai's voice starts every conversation with: at most 800 characters of plain "
        "sentences about the owner (most useful facts first). Call it after updating your memory."
    ))
    async def kai_profile_brief(brief: str) -> str:
        hub.set_brief(brief)
        return "brief updated"

    async def data(name: str, args: dict) -> dict:
        result = await tools.call(name, {k: v for k, v in args.items() if v is not None}, ctx)
        return result.data

    @mcp.tool(description="High and low tides near a US coastal place from the nearest NOAA station. "
                          "`day` in words: today, tomorrow, saturday, day after tomorrow.")
    async def get_tides(place: str | None = None, day: str | None = None) -> dict:
        return await data("get_tides", {"place": place, "day": day})

    @mcp.tool(description="Weather for a place: now, the day's high and low, rain, wind, and waves on the coast.")
    async def get_weather(place: str | None = None, day: str | None = None) -> dict:
        return await data("get_weather", {"place": place, "day": day})

    @mcp.tool(description="Sunrise, sunset, golden hour and blue hour, and cloud cover, for a place and day.")
    async def get_sun_times(place: str | None = None, day: str | None = None) -> dict:
        return await data("get_sun_times", {"place": place, "day": day})

    @mcp.tool(description="The owner's recent trip notes, optionally for one trip.")
    async def list_notes(trip: str | None = None, limit: int = 5) -> dict:
        return await data("list_notes", {"trip": trip, "limit": limit})

    return mcp


class _BearerAuth:
    """Reject any HTTP request without the shared token before it reaches the MCP app."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


async def serve(ctx: tools.ToolContext, hub: AgentHub, port: int, token: str) -> None:
    app = build(ctx, hub).streamable_http_app(host="127.0.0.1")
    config = uvicorn.Config(_BearerAuth(app, token), host="127.0.0.1", port=port, log_level="warning",
                            lifespan="on")
    log.info("Kai MCP server on http://127.0.0.1:%d/mcp", port)
    await uvicorn.Server(config).serve()
