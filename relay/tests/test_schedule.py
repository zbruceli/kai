import asyncio
import re
from datetime import datetime, timedelta

import pytest

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
        key = re.search(r'watch="([^"]+)"', prompt).group(1)
        assert not await sched.watch_fired(job["name"])                # the name alone isn't enough
        assert not await sched.watch_fired(job["name"] + ".guessed")
        assert sched.active_jobs()
        await sched.watch_fired(key)
        return job, prompt, wake

    job, prompt, wake = asyncio.run(go())
    assert job["name"] == "kai-watch-1"
    assert f'watch="{job["name"]}.' in prompt and "[SILENT]" in prompt and "Stop watching after" in prompt
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


def test_timer_wake_queues_each_update_once(tmp_path):
    from kai_relay.agent import InboxItem
    from kai_relay.session import KaiSession

    hub, sched, _ = make(tmp_path)
    a = hub.store.add_item("reminder", "Reminder: move the car", None, "")
    b = hub.store.add_item("task", "Sunrise spots are ready", None, "")
    session = KaiSession.__new__(KaiSession)
    session.hub = hub
    session._delivering = [a.id]  # already being spoken (it fired as the Stick connected)
    session._to_deliver = [InboxItem(b.id, b.created_at, "task", b.speak, None, "")]
    session._queue_pending()
    session._queue_pending()
    assert [i.id for i in session._to_deliver] == [b.id]


def test_cancel_matches_meaningful_words(tmp_path):
    hub, sched, hermes = make(tmp_path)

    async def go():
        await sched.add_reminder("pack the ND filters", datetime.now() + timedelta(hours=3))
        await sched.add_watch("wind at Half Moon Bay drops below 5 mph today", "every 3h", None)
        await sched.add_briefing("weather briefing for Half Moon Bay", "every day at 7am")
        return (await sched.cancel("the wind watch"), await sched.cancel("my ND filters reminder"),
                await sched.cancel("the fishing briefing"))

    wind, filters, fishing = asyncio.run(go())
    assert wind == ["watch: wind at Half Moon Bay drops below 5 mph today"]
    assert filters == ["reminder: pack the ND filters"]
    assert fishing == []  # 'fishing' isn't in the weather briefing, so nothing is cancelled
    assert [j["kind"] for j in sched.active_jobs()] == ["briefing"]


def test_action_confirmed_once():
    """Gemini sometimes confirms a reminder twice; the second, unprompted turn is dropped."""
    from types import SimpleNamespace as NS

    from kai_relay import tools
    from kai_relay.session import KaiSession

    sent, audio = [], []
    s = KaiSession.__new__(KaiSession)
    s.device, s.hub, s._opus_down, s._live = "t", None, None, None
    s._heard = s._caption = s._utterance = ""
    s._transcript, s._to_deliver, s._delivering, s._tasks = [], [], [], set()
    s._audio_bytes, s._utterance_id, s._last_place, s._last_tool = 0, 1, None, None
    s._ptt = s._model_busy = s._drop_turn = s._acted = s._confirmed = False
    s._card_sent = True

    async def send(obj):
        sent.append(obj)

    async def send_audio(data, final=False):
        audio.append(data)

    async def respond(**_):
        pass

    async def call(name, args, ctx):
        return tools.ToolResult({"reminder_id": 4}, None)

    s._send, s._send_audio, s.ctx = send, send_audio, None
    live = NS(send_tool_response=respond)
    speech = lambda text: NS(go_away=None, tool_call=None, server_content=NS(  # noqa: E731
        interrupted=False, input_transcription=None, turn_complete=False,
        model_turn=NS(parts=[NS(inline_data=NS(data=b"\0" * 480))]), output_transcription=NS(text=text)))
    done = NS(go_away=None, tool_call=None, server_content=NS(
        interrupted=False, input_transcription=None, turn_complete=True, model_turn=None, output_transcription=None))
    remind = NS(go_away=None, server_content=None, tool_call=NS(function_calls=[
        NS(id="1", name="remind", args={"text": "check the pot roast", "in_minutes": 3})]))

    async def run():
        orig, tools.call = tools.call, call
        try:
            for msg in (remind, speech("Got it."), done, speech("And I'll remind you."), done):
                await s._on_live_message(live, msg)
        finally:
            tools.call = orig

    asyncio.run(run())
    assert len(audio) == 1  # only the first confirmation reached the Stick


def test_schedule_limits(tmp_path):
    from kai_relay.schedule import MAX_ACTIVE_JOBS, ScheduleLimit, check_frequency

    for bad in ("every 1m", "every 5 minutes", "every minute", "*/5 * * * *", "every 30m", "every half hour"):
        with pytest.raises(ScheduleLimit):
            check_frequency(bad)
    for ok in ("every 3h", "every 180m", "every 1h", "every saturday at 5:45am", "daily at 7am"):
        check_frequency(ok)

    hub, sched, hermes = make(tmp_path)

    async def go():
        with pytest.raises(ScheduleLimit):
            await sched.add_reminder("too far", datetime.now() + timedelta(days=60))
        w = await sched.add_watch("wind drops", "every 3h", datetime.now() + timedelta(days=365))
        for i in range(MAX_ACTIVE_JOBS - 1):
            await sched.add_briefing(f"brief {i}", "daily at 7am")
        with pytest.raises(ScheduleLimit):
            await sched.add_briefing("one too many", "daily at 7am")
        return w

    asyncio.run(go())
    until = datetime.fromisoformat(sched.active_jobs()[0]["until"])
    assert until <= datetime.now() + timedelta(days=15)  # watches last two weeks at most


def test_parallel_jobs_get_distinct_names(tmp_path):
    """One turn's tool calls run concurrently: two watches must not share a name (and orphan a job)."""
    hub, sched, hermes = make(tmp_path)

    async def go():
        return await asyncio.gather(sched.add_watch("wind drops", "every 3h", None),
                                    sched.add_watch("fog clears", "every 3h", None))

    a, b = asyncio.run(go())
    assert a["name"] != b["name"] and len(sched.active_jobs()) == 2 and len(hermes.jobs) == 2


def test_no_tools_while_passing_on_an_update():
    """An update is text from outside (Hermes read it on the web): if it tells Gemini to call a tool, the
    relay refuses instead of running it."""
    from types import SimpleNamespace as NS

    from kai_relay import tools
    from kai_relay.session import KaiSession

    ran, responses = [], []
    s = KaiSession.__new__(KaiSession)
    s.device, s._delivering = "t", [7]

    async def respond(function_responses):
        responses.extend(function_responses)

    async def call(name, args, ctx):
        ran.append(name)

    async def run():
        orig, tools.call = tools.call, call
        try:
            msg = NS(go_away=None, server_content=None, tool_call=NS(function_calls=[
                NS(id="1", name="cancel_scheduled", args={"what": "fishing"})]))
            await s._on_live_message(NS(send_tool_response=respond), msg)
        finally:
            tools.call = orig

    asyncio.run(run())
    assert ran == [] and "error" in responses[0].response


def test_device_names_are_sanitised():
    from kai_relay.session import device_name

    assert device_name("kai-45:14") == "kai-4514"
    assert device_name("x\nFAKE LOG LINE") == "xFAKELOGLINE"
    assert device_name(None) == "?" and len(device_name("a" * 500)) == 32
