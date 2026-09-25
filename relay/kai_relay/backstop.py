"""Card backstop: Gemini sometimes answers a tide/weather/light/parking question from memory or search
instead of calling the tool, which leaves the Stick without a card. When that happens the relay infers
the tool call from what the user said and runs it itself.
"""

import re

DAY_PATTERN = re.compile(
    r"\b(day after tomorrow|day after|tomorrow|tonight|today|this (?:morning|afternoon|evening)|"
    r"(?:next )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)
# Transcripts capitalise place names ("Half Moon Bay", "Pillar Point"): take the capitalised run after a
# preposition, skipping day words.
PLACE_PATTERN = re.compile(r"\b(?:in|at|for|near|around|by)\s+((?:[A-Z][\w'.-]*\s?){1,4})")
NOT_PLACES = {"today", "tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday", "the", "this", "next"}

INTENTS = [  # first match wins
    ("get_tides", re.compile(r"\btides?\b|high water|low water", re.I)),
    ("get_sun_times", re.compile(r"\bsun ?(?:set|rise)\b|golden hour|blue hour|\bsunlight\b", re.I)),
    ("find_parking", re.compile(r"\bpark(?:ing)?\b", re.I)),
    ("get_weather", re.compile(r"\bweather\b|\btemperature\b|\bforecast\b|\brain(?:ing|y)?\b|\bwindy?\b|"
                               r"\bfog(?:gy)?\b|\bswell\b|\bwaves?\b|how (?:hot|cold|warm)", re.I)),
]


def extract_place(text: str) -> str | None:
    for m in PLACE_PATTERN.finditer(text):
        words = [w for w in m.group(1).split() if w.lower().strip(".,?!") not in NOT_PLACES]
        if words:
            return " ".join(words).strip(".,?! ")
    return None


FOLLOW_UP = re.compile(r"^\s*(?:and|what about|how about|and what about)\b", re.I)


def infer_tool_call(transcript: str, last_place: str | None, last_tool: str | None = None) -> tuple[str, dict] | None:
    """(tool name, args) the user's words clearly call for, or None.

    A follow-up like "and the day after?" names no topic, so it reuses the previous tool.
    """
    name = next((n for n, pattern in INTENTS if pattern.search(transcript)), None)
    day = DAY_PATTERN.search(transcript)
    place = extract_place(transcript)
    if name is None and last_tool and FOLLOW_UP.search(transcript) and (day or place):
        name = last_tool
    if name is None:
        return None
    args: dict = {}
    if place or last_place:
        args["place"] = place or last_place
    if day and name != "find_parking":
        args["day"] = day.group(1).lower()
    return name, args
