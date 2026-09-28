import asyncio
from datetime import datetime, timedelta

from kai_relay import tools
from kai_relay.agent import AgentHub, AgentStore
from kai_relay.schedule import JOB_GRACE, Scheduler, after_quiet_hours, in_quiet_hours

from .test_tools import ctx  # noqa: F401


class FakeHermes:
    """Just the jobs API: jobs run hourly from now; deletions are recorded."""

    def __init__(self):
        self.jobs, self.deleted = {}, []

    async def create_job(self, name, prompt, schedule):
        jid = f"h{len(self.jobs) + 1}"
        nxt = (datetime.now() + timedelta(hours=1)).astimezone().isoformat()
        self.jobs[jid] = {"id": jid, "name": name, "prompt": prompt, "schedule_display": schedule,
                          "next_run_at": nxt, "enabled": True}
        return self.jobs[jid]

    async def list_jobs(self):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.deleted.append(job_id)
        return self.jobs.pop(job_id, None) is not None


def make(tmp_path, ctx=None):
    store = AgentStore(tmp_path / "agent.db")
    hermes = FakeHermes()
    hub = AgentHub(store, client=hermes)
    sched = Scheduler(hub, store, hermes)
    if ctx is not None:
        ctx.agent, ctx.scheduler = hub, sched
    return hub, sched, hermes


def test_quiet_hours():
    night = datetime(2026, 9, 28, 23, 30)
    assert in_quiet_hours(night) and after_quiet_hours(night) == datetime(2026, 9, 29, 7, 0)
    early = datetime(2026, 9, 29, 5, 0)
    assert after_quiet_hours(early) == datetime(2026, 9, 29, 7, 0)
    noon = datetime(2026, 9, 29, 12, 0)
    assert not in_quiet_hours(noon) and after_quiet_hours(noon) == noon


def test_remind_times(ctx, tmp_path):  # noqa: F811
    hub, sched, _ = make(tmp_path, ctx)
    r = asyncio.run(tools.call("remind", {"text": "pack the ND filters", "in_minutes": 20}, ctx))
    assert "reminder_id" in r.data and r.card["title"] == "Reminder set"
    due = datetime.fromisoformat(sched.pending_reminders()[0]["due_at"])
    assert timedelta(minutes=19) < due - datetime.now() <= timedelta(minutes=20)
    past = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M")
    asyncio.run(tools.call("remind", {"text": "stretch", "at": past}, ctx))  # already passed today -> tomorrow
    tomorrow = datetime.fromisoformat(sched.pending_reminders()[-1]["due_at"])
    assert tomorrow.date() == datetime.now().date() + timedelta(days=1)
    bad = asyncio.run(tools.call("remind", {"text": "x", "at": "25:99"}, ctx))
    assert "error" in bad.data


def test_due_reminder_becomes_update_and_next_wake(tmp_path):
    hub, sched, _ = make(tmp_path)

    async def go():
        await sched.add_reminder("check the tide", datetime.now() - timedelta(seconds=1))
        later = datetime.now() + timedelta(hours=2)
        await sched.add_reminder("golden hour", later)
        fired = await sched.fire_due()
        return fired, later

    fired, later = asyncio.run(go())
    assert fired == 1 and hub.pending()[0].speak == "Reminder: check the tide"
    assert abs((sched.next_wake() - later).total_seconds()) < 1


def test_watch_wakes_after_quiet_hours_and_stops_when_met(tmp_path):
    hub, sched, hermes = make(tmp_path)

    async def go():
        job = await sched.add_watch("wind at Half Moon Bay under 10 mph", "every 3h",
                                    datetime.now() + timedelta(days=2))
        prompt = hermes.jobs["h1"]["prompt"]
        wake = sched.next_wake()
        await sched.watch_fired(job["name"])
        return job, prompt, wake

    job, prompt, wake = asyncio.run(go())
    assert job["name"] == "kai-watch-1"
    assert f'watch="{job["name"]}"' in prompt and "[SILENT]" in prompt and "Stop watching after" in prompt
    assert wake is not None and not in_quiet_hours(wake)  # a watch never wakes the Stick at night
    assert hermes.deleted == ["h1"] and hermes.jobs == {} and sched.active_jobs() == []


def test_briefing_listing_and_cancel(ctx, tmp_path):  # noqa: F811
    hub, sched, hermes = make(tmp_path, ctx)
    r = asyncio.run(tools.call("schedule_briefing", {"what": "fishing brief for Pillar Point",
                                                     "when": "every saturday at 5:45am"}, ctx))
    assert r.data["scheduled"] == "every saturday at 5:45am" and hermes.jobs
    job = next(iter(hermes.jobs.values()))
    assert job["name"] == "kai-brief-1" and "kai_notify" in job["prompt"]
    assert abs((sched.next_wake() - (datetime.fromisoformat(job["next_run_at"]).replace(tzinfo=None) + JOB_GRACE))
               .total_seconds()) < 1
    listing = asyncio.run(tools.call("list_scheduled", {}, ctx)).data
    assert listing["scheduled"][0]["what"] == "fishing brief for Pillar Point"
    r = asyncio.run(tools.call("cancel_scheduled", {"what": "fishing"}, ctx))
    assert r.data["cancelled"] and hermes.deleted == [job["id"]] and sched.active_jobs() == []


def test_schedule_message_to_the_stick():
    from kai_relay.session import KaiSession

    sent = []
    session = KaiSession.__new__(KaiSession)

    async def send(obj):
        sent.append(obj)

    session._send = send
    asyncio.run(session._on_schedule(datetime.now() + timedelta(minutes=10)))
    asyncio.run(session._on_schedule(None))
    assert 590 <= sent[0]["wake_in_s"] <= 600 and sent[1] == {"type": "schedule", "wake_in_s": 0}
