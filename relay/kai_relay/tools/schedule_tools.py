"""Gemini Live's handles on time: reminders (the relay's own) and Hermes briefings and watches."""

import re
from datetime import datetime, timedelta

from ..hermes import HermesError
from .days import DAY_PARAM, resolve_day
from .registry import ToolContext, ToolError, ToolResult, card, short_time, tool


def _scheduler(ctx: ToolContext):
    if not ctx.scheduler:
        raise ToolError("Scheduling needs my background brain, which isn't set up yet.")
    return ctx.scheduler


def _when(t: datetime) -> str:
    today = datetime.now().date()
    day = "today" if t.date() == today else "tomorrow" if t.date() == today + timedelta(days=1) else f"{t:%a %-m/%-d}"
    return f"{short_time(t)} {day}"


@tool(
    "remind",
    "Set a reminder that Kai will say out loud at the time (the Stick wakes itself if asleep). Give either "
    "in_minutes, or at (24-hour HH:MM) plus an optional day. Don't do date arithmetic yourself.",
    {
        "text": {"type": "STRING", "description": "What to remind them of, e.g. 'pack the ND filters'."},
        "in_minutes": {"type": "INTEGER", "description": "Minutes from now, for 'in 20 minutes'."},
        "at": {"type": "STRING", "description": "Clock time in 24-hour HH:MM, e.g. '17:30' for 5:30 pm."},
        "day": DAY_PARAM,
    },
    required=["text"],
)
async def remind(ctx: ToolContext, text: str, in_minutes: int | None = None, at: str | None = None,
                 day: str | None = None) -> ToolResult:
    sched = _scheduler(ctx)
    now = datetime.now()
    if in_minutes is not None:
        if not 0 < int(in_minutes) <= 60 * 24 * 30:
            raise ToolError("I can set reminders from a minute to a month ahead.")
        due = now + timedelta(minutes=int(in_minutes))
    elif at:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", at.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ToolError(f"I didn't understand the time '{at}'.")
        due = datetime.combine(resolve_day(day, now.date()), datetime.min.time()).replace(
            hour=int(m.group(1)), minute=int(m.group(2)))
        if due <= now and not day:
            due += timedelta(days=1)  # "at 7:00" when it's already past 7 means tomorrow
        if due <= now:
            raise ToolError("That time has already passed.")
    else:
        raise ToolError("When should I remind you?")
    rid = await sched.add_reminder(text.strip(), due)
    return ToolResult({"reminder_id": rid, "when": _when(due), "tell_user": "Confirm the reminder and its time briefly."},
                      card("Reminder set", [text[:26], _when(due)]))


@tool(
    "schedule_briefing",
    "Schedule a recurring or one-off briefing the background brain prepares and Kai delivers, e.g. 'every "
    "saturday at 5:45am' a fishing brief for Pillar Point. Pass `when` in the owner's words.",
    {
        "what": {"type": "STRING", "description": "What the briefing should cover, self-contained."},
        "when": {"type": "STRING", "description": "Schedule in plain words: 'every saturday at 5:45am', "
                                                  "'daily at 7am', 'weekdays at 6:30am', 'tomorrow at 6am'."},
    },
    required=["what", "when"],
)
async def schedule_briefing(ctx: ToolContext, what: str, when: str) -> ToolResult:
    sched = _scheduler(ctx)
    try:
        job = await sched.add_briefing(what.strip(), when.strip())
    except HermesError as e:
        raise ToolError(f"I couldn't schedule that ({e}).") from e
    nxt = _when(job["next_run"]) if job["next_run"] else "soon"
    return ToolResult({"scheduled": job["schedule"], "first": nxt, "tell_user": "Confirm what and when briefly."},
                      card("Briefing set", [what[:26], job["schedule"][:26], f"First: {nxt}"]))


@tool(
    "watch_for",
    "Keep an eye on a condition and tell the owner once when it happens, e.g. 'wind at Half Moon Bay under 10 "
    "mph', 'clear skies at sunset at Pescadero'. Checks every few hours until the condition is met or the "
    "last day passes.",
    {
        "condition": {"type": "STRING", "description": "The condition, self-contained, with place and timeframe."},
        "how_often": {"type": "STRING", "description": "Check interval like 'every 3h' (default) or 'every 1h'."},
        "until_day": {**DAY_PARAM, "description": "Last day to keep watching, in the owner's words. Omit for a week."},
    },
    required=["condition"],
)
async def watch_for(ctx: ToolContext, condition: str, how_often: str = "every 3h", until_day: str | None = None) -> ToolResult:
    sched = _scheduler(ctx)
    today = datetime.now().date()
    last = resolve_day(until_day, today) if until_day else today + timedelta(days=7)
    until = datetime.combine(last, datetime.min.time())
    try:
        job = await sched.add_watch(condition.strip(), how_often.strip() or "every 3h", until)
    except HermesError as e:
        raise ToolError(f"I couldn't set up that watch ({e}).") from e
    return ToolResult({"watching": condition, "checks": job["schedule"], "until": f"{last:%A %B %-d}",
                       "tell_user": "Confirm you'll keep an eye on it and tell them once it happens."},
                      card("Watching", [condition[:26], job["schedule"][:26], f"Until {last:%a %-m/%-d}"]))


@tool(
    "list_scheduled",
    "List the owner's pending reminders, scheduled briefings and active watches.",
    {},
)
async def list_scheduled(ctx: ToolContext) -> ToolResult:
    sched = _scheduler(ctx)
    items = sched.listing()
    lines = [f"{r['when'].split(' ', 1)[0]} {r['text']}" for r in items["reminders"]]
    lines += [f"{j['kind'][:5]}: {j['what']}" for j in items["scheduled"]]
    return ToolResult(items, card("Scheduled", lines or ["Nothing scheduled"]))


@tool(
    "cancel_scheduled",
    "Cancel reminders, briefings or watches matching the owner's words (e.g. 'ND filters', 'fishing brief').",
    {"what": {"type": "STRING", "description": "Words from the reminder, briefing or watch to cancel."}},
    required=["what"],
)
async def cancel_scheduled(ctx: ToolContext, what: str) -> ToolResult:
    sched = _scheduler(ctx)
    gone = await sched.cancel(what)
    if not gone:
        raise ToolError(f"I couldn't find anything scheduled matching '{what}'.")
    return ToolResult({"cancelled": gone}, card("Cancelled", gone))
