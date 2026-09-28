import asyncio
import json

import httpx

from kai_relay import agent as agent_mod
from kai_relay import mcp_server, tools
from kai_relay.agent import AgentHub, AgentStore, parse_result
from kai_relay.hermes import HermesClient

from .test_tools import ctx  # noqa: F401


def test_parse_result_json_and_fallbacks():
    reply = json.dumps({"speak": "Pigeon Point at 6:40.", "details_md": "## Spots",
                        "card": {"title": "Sunrise spots and a very long title",
                                 "lines": ["Pigeon Point 6:40a", "x" * 40, "a", "b", "c", "d"]}})
    speak, card, details = parse_result(f"Sure! {reply} Hope that helps.")
    assert speak == "Pigeon Point at 6:40." and details == "## Spots"
    assert len(card["title"]) == 22 and len(card["lines"]) == 5 and all(len(line) <= 26 for line in card["lines"])
    speak, card, details = parse_result("**High tide** is at 11:53. Low at 6:10. Also more text here.")
    assert speak == "High tide is at 11:53. Low at 6:10." and card is None and details.startswith("**High")
    assert parse_result("")[0]


def test_store_inbox_roundtrip(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    tid = store.add_task("find sunrise spots")
    store.update_task(tid, run_id="run_1", status="running")
    assert [r["id"] for r in store.running_tasks()] == [tid]
    a = store.add_item("task", "One", {"title": "T", "lines": ["x"]}, "d1", tid)
    store.add_item("hermes", "Two", None, "")
    assert store.count_pending() == 2 and store.pending()[0].card == {"title": "T", "lines": ["x"]}
    store.mark_delivered([a.id])
    assert [i.speak for i in store.pending()] == ["Two"]


def fake_hermes(outputs: dict):
    """A Hermes API stand-in: every run completes on the second poll with the given output."""
    polls = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer k" and request.headers["X-Hermes-Session-Key"] == "kai:owner"
        if request.method == "POST" and request.url.path == "/v1/runs":
            prompt = json.loads(request.content)["input"]
            run_id = f"run_{len(polls)}"
            polls[run_id] = (0, next(v for k, v in outputs.items() if k in prompt))
            return httpx.Response(202, json={"run_id": run_id, "status": "queued"})
        run_id = request.url.path.rsplit("/", 1)[-1]
        n, output = polls[run_id]
        polls[run_id] = (n + 1, output)
        if n == 0:
            return httpx.Response(200, json={"run_id": run_id, "status": "running"})
        return httpx.Response(200, json={"run_id": run_id, "status": "completed", "output": output})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://hermes")


def test_hub_task_to_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "POLL_S", 0.01)
    answer = json.dumps({"speak": "Try Pigeon Point.", "card": {"title": "Sunrise", "lines": ["Pigeon Point"]},
                         "details_md": "details"})

    async def go():
        async with fake_hermes({"sunrise": answer}) as http:
            hub = AgentHub(AgentStore(tmp_path / "a.db"), HermesClient("http://hermes", "k", http))
            got = []

            async def listener(item):
                got.append(item)

            hub.subscribe(listener)
            tid = await hub.ask("find sunrise spots")
            for _ in range(200):
                if got:
                    break
                await asyncio.sleep(0.01)
            return hub, tid, got

    hub, tid, got = asyncio.run(go())
    assert got and got[0].speak == "Try Pigeon Point." and got[0].card["title"] == "Sunrise"
    assert hub.store.task(tid)["status"] == "done" and hub.count() == 1


def test_agent_tools_without_hermes(ctx):  # noqa: F811
    r = asyncio.run(tools.call("ask_agent", {"task": "research"}, ctx))
    assert "error" in r.data and "isn't set up" in r.data["error"]
    names = {d["name"] for d in tools.declarations(with_agent=False)}
    assert "ask_agent" not in names and "get_tides" in names


def test_check_inbox_marks_delivered(ctx, tmp_path):  # noqa: F811
    hub = AgentHub(AgentStore(tmp_path / "a.db"), client=object())  # any client counts as enabled
    ctx.agent = hub
    badge = []

    async def on_count(n):
        badge.append(n)

    hub.watch_count(on_count)
    asyncio.run(hub.notify("Wind drops under 10 mph Saturday."))
    r = asyncio.run(tools.call("check_inbox", {}, ctx))
    assert r.data["updates"][0]["summary"].startswith("Wind") and hub.count() == 0
    assert badge == [1, 0]  # the Stick's badge showed 1, then cleared once the update was read out
    r = asyncio.run(tools.call("check_inbox", {}, ctx))
    assert r.data["updates"] == []


def test_mcp_auth_and_notify(ctx, tmp_path):  # noqa: F811
    hub = AgentHub(AgentStore(tmp_path / "a.db"), client=None)
    server = mcp_server.build(ctx, hub)
    result = asyncio.run(server.call_tool("kai_notify", {"summary": "Golden hour starts at 6:29.",
                                                         "card_title": "Golden hour", "card_lines": ["6:29p-7:19p"]}))
    assert "queued" in str(result) and hub.store.pending()[0].card == {"title": "Golden hour", "lines": ["6:29p-7:19p"]}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def request(token):
        guarded = mcp_server._BearerAuth(app, "secret-token")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(guarded), base_url="http://t") as c:
            return (await c.get("/mcp", headers=headers)).status_code

    assert asyncio.run(request("secret-token")) == 200
    assert asyncio.run(request("wrong")) == 401 and asyncio.run(request(None)) == 401
