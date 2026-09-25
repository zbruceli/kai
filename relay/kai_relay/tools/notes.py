"""Trip notes: SQLite is the source of truth, each trip is mirrored to a Markdown file (Obsidian-friendly)."""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .registry import ToolContext, ToolError, ToolResult, card, tool

INBOX = "Inbox"


class NotesStore:
    def __init__(self, db_path: Path, notes_dir: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        notes_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir = notes_dir
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                trip TEXT NOT NULL,
                text TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            """
        )

    def current_trip(self) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = 'current_trip'").fetchone()
        return row["value"] if row else None

    def set_trip(self, name: str | None) -> None:
        with self.db:
            if name:
                self.db.execute("INSERT OR REPLACE INTO kv VALUES ('current_trip', ?)", (name,))
            else:
                self.db.execute("DELETE FROM kv WHERE key = 'current_trip'")

    def add(self, text: str, trip: str | None = None, tags: list[str] | None = None, now: datetime | None = None) -> dict:
        trip = trip or self.current_trip() or INBOX
        now = now or datetime.now()
        tag_str = " ".join("#" + re.sub(r"\W+", "-", t.strip().lower()).strip("-") for t in (tags or []) if t.strip())
        with self.db:
            cur = self.db.execute(
                "INSERT INTO notes (created_at, trip, text, tags) VALUES (?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), trip, text, tag_str),
            )
        self._append_markdown(trip, now, text, tag_str)
        return {"id": cur.lastrowid, "trip": trip, "text": text, "tags": tag_str}

    def list(self, trip: str | None = None, limit: int = 5) -> list[dict]:
        if trip:
            rows = self.db.execute(
                "SELECT * FROM notes WHERE trip = ? COLLATE NOCASE ORDER BY id DESC LIMIT ?", (trip, limit)
            )
        else:
            rows = self.db.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def count(self, trip: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM notes WHERE trip = ?", (trip,)).fetchone()[0]

    def markdown_path(self, trip: str) -> Path:
        slug = re.sub(r"[^\w\- ]+", "", trip).strip() or INBOX
        return self.notes_dir / f"{slug}.md"

    def _append_markdown(self, trip: str, now: datetime, text: str, tags: str) -> None:
        path = self.markdown_path(trip)
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new:
                f.write(f"# {trip}\n\n")
            f.write(f"- **{now:%Y-%m-%d %H:%M}** {text}{' ' + tags if tags else ''}\n")


@tool(
    "save_note",
    "Save a spoken note, e.g. a photo spot, camera settings, a catch, or a reminder. "
    "Goes into the current trip unless a trip is named.",
    {
        "text": {"type": "STRING", "description": "The note, lightly cleaned up. Keep numbers and settings exact."},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Optional short tags like 'sunset', 'spot'."},
        "trip": {"type": "STRING", "description": "Trip name, only if the user names one."},
    },
    required=["text"],
)
async def save_note(ctx: ToolContext, text: str, tags: list[str] | None = None, trip: str | None = None) -> ToolResult:
    note = ctx.notes.add(text, trip=trip, tags=tags)
    n = ctx.notes.count(note["trip"])
    return ToolResult(
        {"saved": True, "trip": note["trip"], "notes_in_trip": n},
        card(f"Noted #{n}", [note["trip"], *_wrap(text, 4)], icon="camera", mood="happy"),
    )


@tool(
    "list_notes",
    "Read back recent notes, optionally for one trip.",
    {
        "trip": {"type": "STRING", "description": "Trip name. Omit for the most recent notes overall."},
        "limit": {"type": "INTEGER", "description": "How many notes, default 5."},
    },
)
async def list_notes(ctx: ToolContext, trip: str | None = None, limit: int = 5) -> ToolResult:
    notes = ctx.notes.list(trip, max(1, min(int(limit), 20)))
    if not notes:
        raise ToolError(f"No notes yet{' for ' + trip if trip else ''}.")
    return ToolResult(
        {"notes": [{"when": n["created_at"], "trip": n["trip"], "text": n["text"]} for n in notes]},
        card(trip or "Recent notes", [n["text"] for n in notes], icon="camera"),
    )


@tool(
    "start_trip",
    "Start a named trip so later notes are grouped under it, e.g. 'Big Sur October'.",
    {"name": {"type": "STRING", "description": "Trip name."}},
    required=["name"],
)
async def start_trip(ctx: ToolContext, name: str) -> ToolResult:
    ctx.notes.set_trip(name)
    return ToolResult({"current_trip": name}, card("Trip started", [name, f"{datetime.now():%b %-d}"], icon="camera", mood="happy"))


@tool("end_trip", "Finish the current trip; later notes go to the Inbox.", {})
async def end_trip(ctx: ToolContext) -> ToolResult:
    trip = ctx.notes.current_trip()
    if not trip:
        raise ToolError("There's no trip in progress.")
    n = ctx.notes.count(trip)
    ctx.notes.set_trip(None)
    return ToolResult({"ended_trip": trip, "notes": n}, card("Trip wrapped", [trip, f"{n} notes saved"], icon="camera", mood="happy"))


def _wrap(text: str, max_lines: int, width: int = 26) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines[:max_lines]
