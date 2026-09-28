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
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from .agent import AgentHub, AgentStore
from .hermes import HermesClient, HermesError

log = logging.getLogger("kai.schedule")

LOOP_S = 15
JOB_GRACE = timedelta(minutes=6)  # a scheduled Hermes run needs a few minutes before there's anything to say
QUIET_START, QUIET_END = time(22, 0), time(7, 0)

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
- If the condition is met: call kai_notify with watch="{name}", a summary of one or two short spoken
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
        n = (self.db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM kai_jobs").fetchone()[0])
        name = f"kai-{'brief' if kind == 'briefing' else 'watch'}-{n}"
        if kind == "briefing":
            prompt = BRIEFING_PROMPT.format(name=name, what=description)
        else:
            until_line = f"Stop watching after {until:%A %B %-d}.\n" if until else ""
            prompt = WATCH_PROMPT.format(name=name, condition=description, until=until_line)
        job = await self.client.create_job(name, prompt, schedule)
        with self.db:
            self.db.execute(
                "INSERT INTO kai_jobs (created_at, kind, name, hermes_id, description, schedule, until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(datetime.now()), kind, name, job["id"], description, job.get("schedule_display") or schedule,
                 _iso(until) if until else None),
            )
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

    async def watch_fired(self, name: str) -> None:
        """kai_notify said this watch's condition was met: stop it."""
        for job in self.active_jobs():
            if job["name"] == name and job["kind"] == "watch":
                await self._deactivate(job, "condition met")

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
    import re

    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS}


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat()


def _local(iso: str | None) -> datetime | None:
    """Hermes returns offset-aware ISO times; the relay works in naive local time (TZ of the service)."""
    if not iso:
        return None
    t = datetime.fromisoformat(iso)
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
