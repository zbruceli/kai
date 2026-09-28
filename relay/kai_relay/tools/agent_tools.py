"""Gemini Live's handles on the background brain: hand off slow work, and read what came back."""

from ..hermes import HermesError
from .registry import ToolContext, ToolError, ToolResult, card, tool


def _hub(ctx: ToolContext):
    if not ctx.agent or not ctx.agent.enabled:
        raise ToolError("My background brain isn't set up yet.")
    return ctx.agent


@tool(
    "ask_agent",
    "Hand a task to Kai's background brain, which can research the web, compare options and plan, taking "
    "from seconds to minutes. Use it when the owner says 'look into', 'research', 'find me', 'plan', 'later', "
    "or when a good answer needs several searches or steps. It returns immediately; the answer arrives later "
    "as an update. Pass a self-contained task: include the place, dates and what to report.",
    {"task": {"type": "STRING", "description": "The full task in plain words, self-contained."}},
    required=["task"],
)
async def ask_agent(ctx: ToolContext, task: str) -> ToolResult:
    hub = _hub(ctx)
    try:
        task_id = await hub.ask(task)
    except HermesError as e:
        raise ToolError(f"My background brain is unavailable right now ({e}).") from e
    return ToolResult(
        {"started": True, "task_id": task_id,
         "tell_user": "Say in a few words that you're on it and will report back. Don't answer the task yourself."},
        card("On it", [task[:26], "I'll report back"], icon="mic"),
    )


@tool(
    "check_inbox",
    "Read the updates waiting for the owner: finished background tasks and messages from the background "
    "brain. Use it when they ask what's new, for updates, or about a task you started earlier.",
    {},
)
async def check_inbox(ctx: ToolContext) -> ToolResult:
    hub = _hub(ctx)
    items = hub.pending()
    if not items:
        return ToolResult({"updates": [], "note": "Nothing new."}, card("No updates", ["Nothing new yet"]))
    hub.mark_delivered([i.id for i in items])
    first = items[0].card if len(items) == 1 and items[0].card else None
    return ToolResult(
        {"updates": [i.summary() for i in items],
         "tell_user": "Summarise each update in a sentence or two, newest last."},
        first or card(f"{len(items)} updates", [i.speak for i in items]),
    )
