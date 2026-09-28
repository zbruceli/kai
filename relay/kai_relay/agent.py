"""The agent hub: Kai's side of the hybrid with Hermes.

Kai's voice (Gemini Live) hands slow work to Hermes with ask_agent; the hub starts a Hermes run, follows
it, and turns the result into an inbox item. Hermes can also post to the inbox itself through the Kai MCP
server (kai_notify). Connected Sticks are told immediately; a sleeping Stick sees a badge when it wakes.

Memory (phase 2): when a voice conversation ends, its transcript goes to Hermes as a *memory* run, which
updates Hermes's memory and sends back a short profile brief (kai_profile_brief) that Kai's voice starts
every conversation with. `recall` asks Hermes about the past, waiting a few seconds before falling back
to the inbox.

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
RECALL_BUDGET_S = 8     # how long Kai's voice waits for a memory answer before it becomes an update
BRIEF_MAX_CHARS = 800

# Every task tells Hermes how its answer will be used: spoken on a tiny speaker, shown on a 240x135 card.
TASK_PROMPT = """You are the background brain of Kai, a pocket voice assistant. Kai's owner asked for this
by voice (transcribed, so allow for small transcription errors):

{task}

Work it out properly (search the web when current facts matter; today is {today}). Then reply with ONLY
one JSON object and nothing else:
{{"speak": "one or two short spoken sentences with the answer: no markdown, no URLs, no lists",
  "card": {{"title": "at most 22 characters", "lines": ["at most 26 characters each", "up to 5 lines"]}},
  "details_md": "the full answer in Markdown, with sources as links"}}"""

# After each voice conversation: learn from it, then refresh the brief Kai's voice starts with.
MEMORY_PROMPT = """This is the transcript of a voice conversation between Kai's owner and Kai (the pocket
voice assistant you are the background brain of). It happened just now, ending {today}.

{transcript}

1. Update your memory about the owner with anything durable and useful for future conversations:
   preferences, gear, favourite places, plans and dates, people, recurring interests, how they like
   answers. Skip small talk, one-off lookups and anything already known. If the owner asked Kai to
   forget something, remove it. Never store secrets (passwords, codes, account numbers).
2. Then call kai_profile_brief with an updated brief: at most 800 characters of plain sentences with
   what Kai's voice should know at the start of every conversation (the most useful facts first).
   Skip this step if nothing changed.
Reply with one short line saying what you remembered, or "nothing new"."""

RECALL_PROMPT = """Kai's owner asked Kai, by voice, about something from the past: {question}
Answer from your memory and past sessions (use session_search), not from the web. If you don't know,
say so plainly. Today is {today}. Reply with ONLY one JSON object:
{{"speak": "one or two short spoken sentences", "card": {{"title": "at most 22 characters",
  "lines": ["at most 26 characters each", "up to 5 lines"]}}, "details_md": "what you found, in Markdown"}}"""


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
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
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
        # kind: task (answer goes to the inbox) | recall (inbox only if slow) | memory (no inbox)
        if "kind" not in {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            with self.db:
                self.db.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'")

    def add_task(self, task: str, kind: str = "task") -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO tasks (created_at, task, status, kind) VALUES (?, ?, 'starting', ?)",
                                  (_now(), task, kind))
        return cur.lastrowid

    def get(self, key: str) -> tuple[str, str] | None:
        row = self.db.execute("SELECT value, updated_at FROM kv WHERE key = ?", (key,)).fetchone()
        return (row["value"], row["updated_at"]) if row else None

    def put(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (key, value, _now()))

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

    async def _start(self, kind: str, text: str, prompt: str) -> tuple[int, str]:
        if not self.client:
            raise HermesError("the background brain isn't configured")
        task_id = self.store.add_task(text, kind)
        try:
            run_id = await self.client.start_run(prompt, idempotency_key=f"kai-{kind}-{task_id}")
        except HermesError as e:
            self.store.update_task(task_id, status="failed", error=str(e))
            raise
        self.store.update_task(task_id, run_id=run_id, status="running")
        log.info("%s %s -> Hermes run %s: %s", kind, task_id, run_id, text[:120].replace("\n", " "))
        return task_id, run_id

    async def ask(self, task: str) -> int:
        """Hand a task to Hermes. Returns the task id at once; the answer arrives later in the inbox."""
        task_id, run_id = await self._start("task", task, TASK_PROMPT.format(task=task.strip(), today=_today()))
        self._watch(task_id, run_id)
        return task_id

    async def learn(self, transcript: str) -> int:
        """Give Hermes a finished voice conversation to remember from. Nothing comes back to the inbox."""
        task_id, run_id = await self._start("memory", transcript,
                                            MEMORY_PROMPT.format(transcript=transcript, today=_today()))
        self._watch(task_id, run_id)
        return task_id

    async def recall(self, question: str, budget_s: float = RECALL_BUDGET_S) -> tuple[str, dict | None] | None:
        """Ask Hermes's memory. Returns (speak, card) if it answers within the budget; otherwise None, and
        the answer arrives later as an inbox update."""
        task_id, run_id = await self._start("recall", question,
                                            RECALL_PROMPT.format(question=question.strip(), today=_today()))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        while loop.time() < deadline:
            await asyncio.sleep(min(1.0, POLL_S))
            try:
                run = await self.client.get_run(run_id)
            except HermesError:
                continue
            if run.get("status") == "completed" and run.get("output"):
                self.store.update_task(task_id, status="done")
                speak, card, _ = parse_result(run["output"])
                return speak, card
            if run.get("status") not in RUNNING:
                break  # failed fast: let the watcher report it
        self._watch(task_id, run_id)
        return None

    def remember_conversation(self, transcript: str) -> None:
        """Hand a finished conversation to Hermes in the background. It must outlive the Stick's session,
        which usually ends because the Stick went to sleep."""
        async def go() -> None:
            try:
                await self.learn(transcript)
            except HermesError as e:
                log.warning("couldn't hand the conversation to memory: %s", e)

        t = asyncio.create_task(go())
        self._watchers.add(t)
        t.add_done_callback(self._watchers.discard)

    def brief(self) -> str | None:
        got = self.store.get("profile_brief")
        return got[0] if got else None

    def set_brief(self, text: str) -> None:
        self.store.put("profile_brief", " ".join(text.split())[:BRIEF_MAX_CHARS])
        log.info("profile brief updated (%d chars)", len(text))

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

        row = self.store.task(task_id)
        task, kind = row["task"], row["kind"]
        if kind == "memory":  # learning happens inside Hermes; nothing to tell the owner
            ok = run.get("status") == "completed"
            self.store.update_task(task_id, status="done" if ok else "failed", error=None if ok else str(run))
            log.info("memory %s %s: %s", task_id, "done" if ok else "failed", (run.get("output") or "")[:160])
            return
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


def _today() -> str:
    return datetime.now().strftime("%A %B %-d %Y, %-I:%M %p")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
