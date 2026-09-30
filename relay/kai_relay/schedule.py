"""Phase 3 of the background brain: things that happen at a time, not when the owner asks.

- Reminders are the relay's own: exact, and no model call when they fire.
- Briefings ("every Saturday at 5:45, a fishing brief") and watches ("tell me if the wind drops under
  10 mph") are Hermes cron jobs. They report through kai_notify. A watch that has fired is removed by the
  relay, because Hermes jobs can't remove themselves while agent-managed scheduling stays off.
- The Stick deep-sleeps between uses, so the scheduler also works out when it should next wake on its own
  timer (`next_wake`) to deliver something. Watches never wake it during quiet hours.
"""

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from .agent import AgentHub, AgentStore
from .hermes import HermesClient, HermesError

log = logging.getLogger("kai.schedule")

LOOP_S = 15
JOB_GRACE = timedelta(minutes=6)  # a scheduled Hermes run needs a few minutes before there's anything to say
QUIET_START, QUIET_END = time(22, 0), time(7, 0)
# Limits: each Hermes job run is an agent run with web searches, so cost scales with them.
MAX_ACTIVE_JOBS = 10
MAX_PENDING_REMINDERS = 50
MAX_REMINDER_AHEAD = timedelta(days=31)
MAX_WATCH_DAYS = 14
MIN_INTERVAL_MIN = 60


class ScheduleLimit(ValueError):
    """A request the scheduler refuses: too many, too far ahead or too often. The message is for the owner."""


_UNIT_MIN = {"s": 1 / 60, "sec": 1 / 60, "secs": 1 / 60, "second": 1 / 60, "seconds": 1 / 60,
             "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
             "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60}


def check_frequency(schedule: str) -> None:
    """Refuse schedules that run more often than hourly ("every 5m", "every minute", cron expressions)."""
    s = schedule.lower()
    if "*" in s or re.search(r"\b(minutely|every (other )?(second|minute)|half an? hour|every half)\b", s):
        raise ScheduleLimit("I can check at most once an hour.")
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", s):
        if unit in _UNIT_MIN and float(n) * _UNIT_MIN[unit] < MIN_INTERVAL_MIN:
            raise ScheduleLimit("I can check at most once an hour.")

BRIEFING_PROMPT = """Scheduled briefing {name} for Kai's owner (Kai is their pocket voice assistant).
Task: {what}
For tides, weather and light you must use Kai's tools (get_tides, get_weather, get_sun_times), not web
search: they are exact for the place and day. Use web search for the rest. Then call kai_notify with: summary = one or two short spoken sentences with the highlights (no
markdown, no URLs); card_title of at most 22 characters; card_lines, up to 5 lines of at most 26
characters; details_md = the full briefing. Then reply "done"."""

WATCH_PROMPT = """Watch {name} for Kai's owner (Kai is their pocket voice assistant).
Condition to watch for: {condition}
{until}Check it now. For weather, wind, waves, tides, sunrise, sunset and light you must use Kai's tools
(get_weather, get_tides, get_sun_times), not web search: they are exact for the place and day. Use web
search only for anything else.
- If the condition is met: call kai_notify with watch="{watch_key}", a summary of one or two short spoken
  sentences saying what happened, a small card and details_md. Then reply "done".
- If it is not met: reply exactly [SILENT]."""

Listener = Callable[[datetime | None], Awaitable[None]]


def in_quiet_hours(t: datetime) -> bool:
    return t.time() >= QUIET_START or t.time() < QUIET_END


def after_quiet_hours(t: datetime) -> datetime:
    """The same moment, or the end of the quiet hours it falls in."""
    if not in_quiet_hours(t):
        return t
    morning = datetime.combine(t.date(), QUIET_END)
    return morning if t.time() < QUIET_END else morning + timedelta(days=1)


class Scheduler:
    def __init__(self, hub: AgentHub, store: AgentStore, client: HermesClient | None):
        self.hub, self.store, self.client = hub, store, client
        self.db = store.db
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                text TEXT NOT NULL,
                fired_at TEXT,
                cancelled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS kai_jobs (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,            -- briefing | watch
                name TEXT NOT NULL UNIQUE,     -- kai-brief-N / kai-watch-N (also the Hermes job name)
                hermes_id TEXT NOT NULL,
                description TEXT NOT NULL,
                schedule TEXT NOT NULL,
                until TEXT,                    -- watches: stop after this date
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        # A watch's secret: only its own job knows it, so no other Hermes run can stop it via kai_notify.
        if "secret" not in {r["name"] for r in self.db.execute("PRAGMA table_info(kai_jobs)")}:
            with self.db:
                self.db.execute("ALTER TABLE kai_jobs ADD COLUMN secret TEXT")
        self._listeners: set[Listener] = set()
        self._jobs_cache: list[dict] = []
        self._jobs_fresh = False  # the cache reflects a successful listing from Hermes
        self._last_wake: datetime | None = None

    # ---- listeners: connected Sticks want to know when to wake next ----

    def watch(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def unwatch(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    async def _changed(self) -> None:
        wake = self.next_wake()
        if wake == self._last_wake:
            return
        self._last_wake = wake
        for listener in list(self._listeners):
            try:
                await listener(wake)
            except Exception:
                log.exception("schedule listener failed")

    # ---- reminders ----

    async def add_reminder(self, text: str, due: datetime) -> int:
        if due > datetime.now() + MAX_REMINDER_AHEAD:
            raise ScheduleLimit("I can set reminders up to a month ahead.")
        if len(self.pending_reminders()) >= MAX_PENDING_REMINDERS:
            raise ScheduleLimit(f"You already have {MAX_PENDING_REMINDERS} reminders waiting.")
        with self.db:
            cur = self.db.execute("INSERT INTO reminders (created_at, due_at, text) VALUES (?, ?, ?)",
                                  (_iso(datetime.now()), _iso(due), text))
        log.info("reminder %s at %s: %s", cur.lastrowid, _iso(due), text)
        await self._changed()
        return cur.lastrowid

    def pending_reminders(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM reminders WHERE fired_at IS NULL AND cancelled = 0 ORDER BY due_at")
        return [dict(r) for r in rows]

    async def fire_due(self, within_s: float = 0) -> int:
        """Turn reminders that are due (or due within `within_s`) into inbox updates."""
        limit = datetime.now() + timedelta(seconds=within_s)
        fired = 0
        for r in self.pending_reminders():
            if datetime.fromisoformat(r["due_at"]) > limit:
                break
            with self.db:
                self.db.execute("UPDATE reminders SET fired_at = ? WHERE id = ?", (_iso(datetime.now()), r["id"]))
            await self.hub.notify(f"Reminder: {r['text']}", {"title": "Reminder", "lines": _wrap(r["text"])},
                                  r["text"], source="reminder")
            fired += 1
        if fired:
            await self._changed()
        return fired

    # ---- Hermes jobs: briefings and watches ----

    async def add_briefing(self, what: str, when: str) -> dict:
        return await self._add_job("briefing", what, when, None)

    async def add_watch(self, condition: str, how_often: str, until: datetime | None) -> dict:
        return await self._add_job("watch", condition, how_often, until)

    async def _add_job(self, kind: str, description: str, schedule: str, until: datetime | None) -> dict:
        if not self.client:
            raise HermesError("the background brain isn't configured")
        check_frequency(schedule)
        if len(self.active_jobs()) >= MAX_ACTIVE_JOBS:
            raise ScheduleLimit(f"You already have {MAX_ACTIVE_JOBS} briefings and watches; cancel one first.")
        if kind == "watch":
            last = datetime.combine(datetime.now().date() + timedelta(days=MAX_WATCH_DAYS), time())
            until = min(until, last) if until else last
        # Reserve the row (and so the name) before the slow Hermes call: tool calls in one turn run in
        # parallel, and two jobs must never share a name.
        secret = secrets.token_urlsafe(9)
        with self.db:
            cur = self.db.execute(
                "INSERT INTO kai_jobs (created_at, kind, name, hermes_id, description, schedule, until, active, secret) "
                "VALUES (?, ?, ?, '', ?, ?, ?, 0, ?)",
                (_iso(datetime.now()), kind, f"pending-{secret}", description, schedule,
                 _iso(until) if until else None, secret),
            )
        row_id = cur.lastrowid
        name = f"kai-{'brief' if kind == 'briefing' else 'watch'}-{row_id}"
        if kind == "briefing":
            prompt = BRIEFING_PROMPT.format(name=name, what=description)
        else:
            until_line = f"Stop watching after {until:%A %B %-d}.\n" if until else ""
            prompt = WATCH_PROMPT.format(name=name, condition=description, until=until_line,
                                         watch_key=f"{name}.{secret}")
        try:
            job = await self.client.create_job(name, prompt, schedule)
        except Exception:
            with self.db:
                self.db.execute("DELETE FROM kai_jobs WHERE id = ?", (row_id,))
            raise
        with self.db:
            self.db.execute("UPDATE kai_jobs SET name = ?, hermes_id = ?, schedule = ?, active = 1 WHERE id = ?",
                            (name, str(job["id"]), job.get("schedule_display") or schedule, row_id))
        log.info("%s %s (Hermes job %s, %s): %s", kind, name, job["id"], job.get("schedule_display"), description)
        await self.refresh_jobs()
        return {"name": name, "schedule": job.get("schedule_display") or schedule,
                "next_run": _local(job.get("next_run_at"))}

    def active_jobs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM kai_jobs WHERE active = 1 ORDER BY id")]

    async def _deactivate(self, job: dict, why: str) -> None:
        if self.client:
            try:
                await self.client.delete_job(job["hermes_id"])
            except HermesError as e:
                log.warning("couldn't delete Hermes job %s: %s", job["hermes_id"], e)
        with self.db:
            self.db.execute("UPDATE kai_jobs SET active = 0 WHERE id = ?", (job["id"],))
        log.info("%s %s stopped (%s)", job["kind"], job["name"], why)
        await self.refresh_jobs()

    async def watch_fired(self, watch_key: str) -> bool:
        """kai_notify said this watch's condition was met: stop it. The key ("kai-watch-N.<secret>") is in
        that watch's own prompt only, so no other Hermes run can stop someone else's watch."""
        name, _, secret = watch_key.strip().rpartition(".")
        for job in self.active_jobs():
            if (job["name"] == name and job["kind"] == "watch" and job["secret"]
                    and secrets.compare_digest(job["secret"], secret)):
                await self._deactivate(job, "condition met")
                return True
        log.warning("kai_notify named an unknown watch or a wrong key: %s", name or watch_key[:40])
        return False

    async def prune(self) -> None:
        """Stop watches past their last day, and forget jobs Hermes no longer has (one-shot, finished)."""
        now = datetime.now()
        known = {j["id"] for j in self._jobs_cache}
        for job in self.active_jobs():
            if job["until"] and now > datetime.fromisoformat(job["until"]) + timedelta(days=1):
                await self._deactivate(job, "past its last day")
            elif self._jobs_fresh and job["hermes_id"] not in known:  # finished one-shot, or removed in Hermes
                with self.db:
                    self.db.execute("UPDATE kai_jobs SET active = 0 WHERE id = ?", (job["id"],))
                log.info("%s %s finished", job["kind"], job["name"])

    async def refresh_jobs(self) -> None:
        if not self.client:
            return
        try:
            self._jobs_cache = await self.client.list_jobs()
            self._jobs_fresh = True
        except HermesError as e:
            log.warning("couldn't list Hermes jobs: %s", e)
            self._jobs_fresh = False
            return
        await self._changed()

    # ---- listing, cancelling, waking ----

    def listing(self) -> dict:
        next_runs = {j["id"]: _local(j.get("next_run_at")) for j in self._jobs_cache}
        return {
            "reminders": [{"id": r["id"], "when": _say(datetime.fromisoformat(r["due_at"])), "text": r["text"]}
                          for r in self.pending_reminders()],
            "scheduled": [{"name": j["name"], "kind": j["kind"], "what": j["description"], "schedule": j["schedule"],
                           "next": _say(next_runs[j["hermes_id"]]) if next_runs.get(j["hermes_id"]) else None}
                          for j in self.active_jobs()],
        }

    async def cancel(self, what: str) -> list[str]:
        """Cancel reminders and jobs matching the owner's words ("the wind watch", "ND filters").
        Every meaningful word must appear; a kind word (reminder, briefing, watch) narrows the search."""
        words = _words(what)
        kinds = {k for k in ("reminder", "briefing", "watch") if k in words}
        words -= KIND_WORDS
        gone = []
        if not kinds or "reminder" in kinds:
            for r in self.pending_reminders():
                if words and words <= _words(r["text"]) or what.strip() == str(r["id"]):
                    with self.db:
                        self.db.execute("UPDATE reminders SET cancelled = 1 WHERE id = ?", (r["id"],))
                    gone.append(f"reminder: {r['text']}")
        for job in self.active_jobs():
            if kinds and job["kind"] not in kinds:
                continue
            if words and words <= _words(job["description"]) or what.strip() == job["name"]:
                await self._deactivate(job, "cancelled by the owner")
                gone.append(f"{job['kind']}: {job['description']}")
        await self._changed()
        return gone

    def next_wake(self) -> datetime | None:
        """When a sleeping Stick should wake on its own to deliver something, or None."""
        times = [datetime.fromisoformat(r["due_at"]) for r in self.pending_reminders()]
        kinds = {j["hermes_id"]: j["kind"] for j in self.active_jobs()}
        for j in self._jobs_cache:
            kind = kinds.get(j["id"])
            run = _local(j.get("next_run_at"))
            if not kind or not run or not j.get("enabled", True):
                continue
            t = run + JOB_GRACE
            times.append(after_quiet_hours(t) if kind == "watch" else t)
        return min(times) if times else None

    async def run(self) -> None:
        """Fire reminders on time and keep the job list fresh."""
        await self.refresh_jobs()
        ticks = 0
        while True:
            await asyncio.sleep(LOOP_S)
            try:
                await self.fire_due()
                ticks += 1
                if ticks % 4 == 0:  # every minute
                    await self.refresh_jobs()
                    await self.prune()
            except Exception:
                log.exception("scheduler tick failed")


STOP_WORDS = {"the", "a", "an", "my", "for", "at", "in", "on", "of", "to", "and", "that", "this", "about", "please"}
KIND_WORDS = {"reminder", "reminders", "briefing", "briefings", "brief", "watch", "watches", "alert"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS}


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat()


def _local(iso: str | None) -> datetime | None:
    """Hermes returns offset-aware ISO times; the relay works in naive local time (TZ of the service)."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso))
    except ValueError:
        return None  # Hermes's value, not ours: ignore what we can't read
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _say(t: datetime) -> str:
    today = datetime.now().date()
    day = "today" if t.date() == today else "tomorrow" if t.date() == today + timedelta(days=1) else f"{t:%a %b %-d}"
    hour = t.hour % 12 or 12
    return f"{day} {hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _wrap(text: str, width: int = 26, lines: int = 5) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return (out + [line] if line else out)[:lines]
