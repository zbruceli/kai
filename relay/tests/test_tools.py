import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from kai_relay import tools
from kai_relay.config import Settings
from kai_relay.tools import registry
from kai_relay.tools.tides import nearest_station
from kai_relay.tools.weather import compass

SETTINGS = Settings(
    gemini_api_key="x", device_token="t", model="gemini-3.8-live", voice="Puck",
    home_name="Half Moon Bay", home_lat=37.4636, home_lon=-122.4286, units="imperial",
    maps_api_key=None, host="127.0.0.1", port=8765, data_dir=Path("."), notes_dir=Path("."), idle_close_s=300,
)


@pytest.fixture
def ctx(tmp_path):
    s = replace(SETTINGS, data_dir=tmp_path, notes_dir=tmp_path / "notes")
    store = tools.NotesStore(tmp_path / "kai.db", s.notes_dir)
    return tools.ToolContext(settings=s, http=httpx.AsyncClient(), notes=store)


def test_declarations_are_well_formed():
    decls = {d["name"]: d for d in tools.declarations()}
    assert {"save_note", "list_notes", "start_trip", "end_trip", "get_tides", "get_conditions",
            "get_sun_times", "find_parking"} <= decls.keys()
    for d in decls.values():
        assert d["description"] and d["parameters"]["type"] == "OBJECT"
        for req in d["parameters"].get("required", []):
            assert req in d["parameters"]["properties"]


def test_live_config_validates():
    from kai_relay.session import KaiSession

    session = KaiSession.__new__(KaiSession)
    session.settings = SETTINGS
    cfg = session._live_config()
    assert cfg.realtime_input_config.automatic_activity_detection.disabled is True
    assert len(cfg.tools[1].function_declarations) == len(tools.declarations())


def test_card_truncates():
    c = tools.card("A very long title that will not fit", ["x" * 40] * 8)
    assert len(c["title"]) == registry.CARD_TITLE_CHARS
    assert len(c["lines"]) == registry.CARD_MAX_LINES
    assert all(len(line) == registry.CARD_LINE_CHARS for line in c["lines"])


def test_short_time():
    assert registry.short_time(datetime(2026, 9, 24, 0, 5)) == "12:05a"
    assert registry.short_time(datetime(2026, 9, 24, 16, 12)) == "4:12p"


def test_compass():
    assert compass(0) == "N" and compass(275) == "W" and compass(350) == "N" and compass(None) == "?"


def test_nearest_station():
    stations = [
        {"id": "9414290", "name": "San Francisco", "lat": 37.806, "lon": -122.465},
        {"id": "9414131", "name": "Pillar Point Harbor", "lat": 37.502, "lon": -122.482},
    ]
    st, km = nearest_station(stations, 37.4636, -122.4286)
    assert st["id"] == "9414131" and km < 10


def test_call_filters_unknown_args_and_reports_errors(ctx):
    r = asyncio.run(tools.call("list_notes", {"trip": "Nope", "bogus": 1}, ctx))
    assert "error" in r.data and r.card
    r = asyncio.run(tools.call("no_such_tool", {}, ctx))
    assert "error" in r.data


def test_notes_roundtrip(ctx):
    asyncio.run(tools.call("start_trip", {"name": "Pigeon Point Oct"}, ctx))
    r = asyncio.run(tools.call("save_note", {"text": "f/11 1/4s 10-stop ND at the lighthouse", "tags": ["Long Exposure"]}, ctx))
    assert r.data["trip"] == "Pigeon Point Oct" and r.data["notes_in_trip"] == 1
    md = ctx.notes.markdown_path("Pigeon Point Oct").read_text()
    assert md.startswith("# Pigeon Point Oct") and "10-stop ND" in md and "#long-exposure" in md
    r = asyncio.run(tools.call("end_trip", {}, ctx))
    assert r.data["notes"] == 1
    r = asyncio.run(tools.call("save_note", {"text": "buy more leader line"}, ctx))
    assert r.data["trip"] == "Inbox"


def test_screen_text_is_ascii():
    from kai_relay.session import screen_text

    assert screen_text("It’s 68°F at Año Nuevo — nice…") == "It's 68F at Ano Nuevo - nice..."
