"""Gemini Live's handles on the background brain: hand off slow work, and read what came back."""

from ..hermes import HermesError
from .registry import ToolContext, ToolError, ToolResult, card, tool


def _hub(ctx: ToolContext):
    if not ctx.agent or not ctx.agent.enabled:
        raise ToolError("My background brain isn't set up yet.")
    return ctx.agent


@tool(
    "ask_agent",
    "Hand a task to Kai's background brain, which researches the web, weighs several sources and constraints, "
    "compares options and plans, usually within a minute. Use it when a good answer needs more than one "
    "lookup, or the owner asks for depth or says yes to digging deeper. It returns immediately; the answer "
    "arrives later as an update. Pass a self-contained task: include the place, dates and what to report.",
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
    await hub.mark_delivered([i.id for i in items])  # clears the Stick's badge
    first = items[0].card if len(items) == 1 and items[0].card else None
    return ToolResult(
        {"updates": [i.summary() for i in items],
         "tell_user": "Summarise each update in a sentence or two, newest last."},
        first or card(f"{len(items)} updates", [i.speak for i in items]),
    )


@tool(
    "recall",
    "Ask Kai's long-term memory about the owner's past: earlier conversations, plans, places, gear, what they "
    "said or asked before. Use it for 'what did I say about…', 'when did we…', 'what was that place…'. Not for "
    "setting reminders (use remind), trip notes "
    "(use list_notes) or current facts (use search or the data tools).",
    {"question": {"type": "STRING", "description": "The question about the past, self-contained."}},
    required=["question"],
)
async def recall(ctx: ToolContext, question: str) -> ToolResult:
    hub = _hub(ctx)
    try:
        answer = await hub.recall(question)
    except HermesError as e:
        raise ToolError(f"My memory is unavailable right now ({e}).") from e
    if answer is None:
        return ToolResult({"pending": True,
                           "tell_user": "Say you're checking your memory and will tell them shortly."},
                          card("Remembering...", [question[:26]]))
    speak, answer_card = answer
    return ToolResult({"answer": speak, "tell_user": "Tell them this in your own words."}, answer_card)
