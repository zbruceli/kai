"""The agent hub: Kai's side of the hybrid with Hermes.

Kai's voice (Gemini Live) hands slow work to Hermes with ask_agent; the hub starts a Hermes run, follows
it, and turns the result into an inbox item. Hermes can also post to the inbox itself through the Kai MCP
server (kai_notify). Connected Sticks are told immediately; a sleeping Stick sees a badge when it wakes.

The inbox belongs to Kai's single owner and is delivered to whichever Stick is connected.
"""

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .hermes import RUNNING, HermesClient, HermesError

log = logging.getLogger("kai.agent")

POLL_S = 3
RUN_TIMEOUT_S = 15 * 60

# Every task tells Hermes how its answer will be used: spoken on a tiny speaker, shown on a 240x135 card.
TASK_PROMPT = """You are the background brain of Kai, a pocket voice assistant. Kai's owner asked for this
by voice (transcribed, so allow for small transcription errors):

{task}

Work it out properly (search the web when current facts matter; today is {today}). Then reply with ONLY
one JSON object and nothing else:
{{"speak": "one or two short spoken sentences with the answer: no markdown, no URLs, no lists",
  "card": {{"title": "at most 22 characters", "lines": ["at most 26 characters each", "up to 5 lines"]}},
  "details_md": "the full answer in Markdown, with sources as links"}}"""


@dataclass
class InboxItem:
    id: int
    created_at: str
    source: str  # "task" or "hermes"
    speak: str
    card: dict | None
    details_md: str

    def summary(self) -> dict:
        return {"id": self.id, "when": self.created_at, "summary": self.speak, "details": self.details_md[:1500]}


class AgentStore:
    """Tasks handed to Hermes and the owner's inbox, in SQLite next to the notes."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                task TEXT NOT NULL,
                run_id TEXT,
                status TEXT NOT NULL,          -- starting | running | done | failed
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                task_id INTEGER,
                speak TEXT NOT NULL,
                card TEXT,
                details_md TEXT NOT NULL DEFAULT '',
                delivered_at TEXT
            );
            """
        )

    def add_task(self, task: str) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO tasks (created_at, task, status) VALUES (?, ?, 'starting')",
                                  (_now(), task))
        return cur.lastrowid

    def update_task(self, task_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    def task(self, task_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def running_tasks(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM tasks WHERE status = 'running' AND run_id IS NOT NULL"))

    def add_item(self, source: str, speak: str, card: dict | None, details_md: str,
                 task_id: int | None = None) -> InboxItem:
        created = _now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO inbox (created_at, source, task_id, speak, card, details_md) VALUES (?, ?, ?, ?, ?, ?)",
                (created, source, task_id, speak, json.dumps(card) if card else None, details_md),
            )
        return InboxItem(cur.lastrowid, created, source, speak, card, details_md)

    def pending(self, limit: int = 10) -> list[InboxItem]:
        rows = self.db.execute("SELECT * FROM inbox WHERE delivered_at IS NULL ORDER BY id LIMIT ?", (limit,))
        return [InboxItem(r["id"], r["created_at"], r["source"], r["speak"],
                          json.loads(r["card"]) if r["card"] else None, r["details_md"]) for r in rows]

    def count_pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM inbox WHERE delivered_at IS NULL").fetchone()[0]

    def mark_delivered(self, ids: list[int]) -> None:
        if ids:
            with self.db:
                self.db.executemany("UPDATE inbox SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                                    [(_now(), i) for i in ids])


def parse_result(text: str) -> tuple[str, dict | None, str]:
    """(speak, card, details_md) from Hermes's reply; tolerant of prose around or instead of the JSON."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            speak = str(data.get("speak") or "").strip()
            card = data.get("card") if isinstance(data.get("card"), dict) else None
            if card:
                card = {"title": str(card.get("title") or "Update")[:22],
                        "lines": [str(x)[:26] for x in (card.get("lines") or [])][:5]}
            details = str(data.get("details_md") or "").strip()
            if speak:
                return speak, card, details or speak
        except (ValueError, AttributeError):
            pass
    plain = re.sub(r"[#*_`>\[\]]", "", (text or "").strip())
    sentences = re.split(r"(?<=[.!?])\s+", plain)
    return " ".join(sentences[:2])[:300] or "The task finished, but the answer was empty.", None, text or ""


Listener = Callable[[InboxItem], Awaitable[None]]
CountListener = Callable[[int], Awaitable[None]]


class AgentHub:
    def __init__(self, store: AgentStore, client: HermesClient | None):
        self.store = store
        self.client = client
        self._listeners: set[Listener] = set()
        self._count_listeners: set[CountListener] = set()
        self._watchers: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def subscribe(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    def watch_count(self, listener: CountListener) -> None:
        """Called with the new number of waiting updates whenever it changes (the Stick's badge)."""
        self._count_listeners.add(listener)

    def unwatch_count(self, listener: CountListener) -> None:
        self._count_listeners.discard(listener)

    async def _count_changed(self) -> None:
        n = self.count()
        for listener in list(self._count_listeners):
            try:
                await listener(n)
            except Exception:
                log.exception("inbox count listener failed")

    async def start(self) -> None:
        """Pick up runs that were in flight when the relay last stopped."""
        for row in self.store.running_tasks():
            log.info("resuming Hermes run %s (task %s)", row["run_id"], row["id"])
            self._watch(row["id"], row["run_id"])

    async def ask(self, task: str) -> int:
        """Hand a task to Hermes. Returns the task id at once; the answer arrives later in the inbox."""
        if not self.client:
            raise HermesError("the background brain isn't configured")
        task_id = self.store.add_task(task)
        prompt = TASK_PROMPT.format(task=task.strip(), today=datetime.now().strftime("%A %B %-d %Y, %-I:%M %p"))
        try:
            run_id = await self.client.start_run(prompt, idempotency_key=f"kai-task-{task_id}")
        except HermesError as e:
            self.store.update_task(task_id, status="failed", error=str(e))
            raise
        self.store.update_task(task_id, run_id=run_id, status="running")
        log.info("task %s -> Hermes run %s: %s", task_id, run_id, task[:120])
        self._watch(task_id, run_id)
        return task_id

    async def notify(self, speak: str, card: dict | None = None, details_md: str = "", source: str = "hermes",
                     task_id: int | None = None) -> InboxItem:
        item = self.store.add_item(source, speak, card, details_md, task_id)
        log.info("inbox +1 (%s): %s", source, speak[:120])
        await self._count_changed()
        for listener in list(self._listeners):
            try:
                await listener(item)
            except Exception:
                log.exception("inbox listener failed")
        return item

    def pending(self) -> list[InboxItem]:
        return self.store.pending()

    def count(self) -> int:
        return self.store.count_pending()

    async def mark_delivered(self, ids: list[int]) -> None:
        """Read out, spoken, or otherwise seen: no longer counts toward the badge."""
        self.store.mark_delivered(ids)
        await self._count_changed()

    def _watch(self, task_id: int, run_id: str) -> None:
        t = asyncio.create_task(self._follow(task_id, run_id))
        self._watchers.add(t)
        t.add_done_callback(self._watchers.discard)

    async def _follow(self, task_id: int, run_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RUN_TIMEOUT_S
        run: dict = {}
        while loop.time() < deadline:
            await asyncio.sleep(POLL_S)
            try:
                run = await self.client.get_run(run_id)
            except HermesError as e:
                log.warning("task %s: %s (retrying)", task_id, e)
                continue
            if run.get("status") not in RUNNING:
                break
        else:
            run = {"status": "timeout", "error": "took longer than 15 minutes"}

        task = self.store.task(task_id)["task"]
        if run.get("status") == "completed" and run.get("output"):
            speak, card, details = parse_result(run["output"])
            self.store.update_task(task_id, status="done")
            await self.notify(speak, card, details, source="task", task_id=task_id)
        else:
            error = run.get("error") or run.get("status") or "unknown error"
            self.store.update_task(task_id, status="failed", error=str(error))
            log.warning("task %s failed: %s", task_id, error)
            await self.notify(f"I couldn't finish the task about {task[:60]}.",
                              {"title": "Task failed", "lines": [task[:26], str(error)[:26]]},
                              f"Task: {task}\n\nError: {error}", source="task", task_id=task_id)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
