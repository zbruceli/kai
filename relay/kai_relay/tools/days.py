"""Turn the user's own words for a day ("tomorrow", "Saturday") into a date on the relay.

Gemini is unreliable at date arithmetic (it sent tomorrow's date for "today"), so tools take the day as
spoken and the relay resolves it against the local clock.
"""

import re
from datetime import date, timedelta

from .registry import ToolError

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DAY_PARAM = {
    "type": "STRING",
    "description": 'Which day, in the user\'s own words: "today", "tonight", "tomorrow", "day after tomorrow", '
    '"saturday", "next monday". Omit for today. Do not convert it to a date yourself.',
}


def resolve_day(day: str | None, today: date) -> date:
    if not day:
        return today
    d = day.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):  # tolerate a model that still sends ISO dates
        return date.fromisoformat(d)
    # Word boundaries matter: "sunday afternoon" contains "day after", "today" contains "day".
    if re.search(r"\bday after\b", d) or "overmorrow" in d:
        return today + timedelta(days=2)
    if re.match(r"(tomorrow|tmr)\b", d):
        return today + timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", d):
            delta = (i - today.weekday()) % 7
            if delta == 0 and re.search(r"\bnext\b", d):
                delta = 7
            return today + timedelta(days=delta)
    if re.match(r"(today|tonight|now|right now|this|later)\b", d):
        return today
    m = re.search(r"in (\d+) days?", d)
    if m:
        return today + timedelta(days=int(m.group(1)))
    raise ToolError(f"I'm not sure which day '{day}' means.")


def describe_day(target: date, today: date) -> str:
    """'today', 'tomorrow' or 'Sat 9/27', for card titles."""
    delta = (target - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"{target:%a} {target.month}/{target.day}"
