"""Tool registry: each tool is an async function plus a Gemini function declaration.

A tool returns a ToolResult: `data` goes back to Gemini, `card` (optional) is drawn on the Stick's screen.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger("kai.tools")

# The Stick's screen fits a title plus five lines in its card font.
CARD_TITLE_CHARS = 22
CARD_LINE_CHARS = 26
CARD_MAX_LINES = 5


class ToolError(Exception):
    """An expected failure whose message is safe to hand to the model (and to speak)."""


@dataclass
class ToolResult:
    data: dict[str, Any]
    card: dict[str, Any] | None = None


@dataclass
class ToolContext:
    settings: Settings
    http: httpx.AsyncClient
    notes: Any  # NotesStore; typed loosely to avoid an import cycle
    cache: dict[str, Any] = field(default_factory=dict)


ToolFn = Callable[..., Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn


_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None):
    """Register an async `fn(ctx, **args) -> ToolResult` as a Gemini-callable function."""
    parameters = {"type": "OBJECT", "properties": properties}
    if required:
        parameters["required"] = required

    def decorator(fn: ToolFn) -> ToolFn:
        _REGISTRY[name] = Tool(name, description, parameters, fn)
        return fn

    return decorator


def declarations() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in _REGISTRY.values()
    ]


async def call(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    t = _REGISTRY.get(name)
    if t is None:
        return ToolResult({"error": f"Unknown tool {name}"})
    # Drop anything the model invented that the function doesn't declare.
    known = t.parameters["properties"].keys()
    kwargs = {k: v for k, v in (args or {}).items() if k in known and v is not None}
    try:
        log.info("tool %s(%s)", name, kwargs)
        return await t.fn(ctx, **kwargs)
    except ToolError as e:
        return ToolResult({"error": str(e)}, card("Hmm", [str(e)]))
    except httpx.HTTPError as e:
        log.warning("tool %s HTTP failure: %s", name, e)
        return ToolResult({"error": f"The {name} service didn't respond properly."}, card("Offline?", ["Service error", name]))
    except Exception:
        log.exception("tool %s crashed", name)
        return ToolResult({"error": f"{name} failed unexpectedly."})


def card(title: str, lines: list[str]) -> dict[str, Any]:
    return {
        "title": title[:CARD_TITLE_CHARS],
        "lines": [line[:CARD_LINE_CHARS] for line in lines[:CARD_MAX_LINES]],
    }


def short_time(dt: datetime) -> str:
    """3:05p style: compact enough for the card font."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d}{'a' if dt.hour < 12 else 'p'}"
