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
    assert {"save_note", "list_notes", "start_trip", "end_trip", "get_tides", "get_weather",
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


def test_geocode_prefers_places_near_home():
    from kai_relay.tools.geo import _pick

    results = [
        {"name": "San Mateo", "admin1": "Calabarzon", "country": "Philippines", "country_code": "PH", "latitude": 14.70, "longitude": 121.12},
        {"name": "San Mateo", "admin1": "California", "country": "United States", "country_code": "US", "latitude": 37.56, "longitude": -122.33},
    ]
    assert _pick(results, 37.46, -122.43, "San Mateo")["lat"] == 37.56          # near home wins
    assert _pick(results, 37.46, -122.43, "San Mateo, Philippines")["lat"] == 14.70  # explicit region wins
    assert _pick(results, None, None, "San Mateo")["lat"] == 14.70              # no home: top hit


def test_note_card_is_happy_camera(ctx):
    r = asyncio.run(tools.call("save_note", {"text": "golden hour at Pigeon Point"}, ctx))
    assert r.card["icon"] == "camera" and r.card["mood"] == "happy"


def test_resolve_day():
    from datetime import date

    from kai_relay.tools.days import describe_day, resolve_day

    thu = date(2026, 9, 24)
    assert resolve_day(None, thu) == thu
    assert resolve_day("tonight", thu) == thu
    assert resolve_day("tomorrow morning", thu) == date(2026, 9, 25)
    assert resolve_day("day after tomorrow", thu) == date(2026, 9, 26)
    assert resolve_day("Saturday", thu) == date(2026, 9, 26)
    assert resolve_day("thursday", thu) == thu
    assert resolve_day("next Thursday", thu) == date(2026, 10, 1)
    assert resolve_day("2026-09-30", thu) == date(2026, 9, 30)
    with pytest.raises(tools.ToolError):
        resolve_day("whenever", thu)
    assert describe_day(date(2026, 9, 26), thu) == "Sat 9/26"
    # Regressions: substrings used to win ("sun-day after-noon", "to-day after-noon", "this saturday").
    assert resolve_day("sunday afternoon", thu) == date(2026, 9, 27)
    assert resolve_day("today afternoon", thu) == thu
    assert resolve_day("this saturday", thu) == date(2026, 9, 26)
    assert resolve_day("this evening", thu) == thu


def test_backstop_infers_tool_calls():
    from kai_relay.backstop import infer_tool_call

    assert infer_tool_call("When is high and low tide tomorrow in Half Moon Bay?", None) == (
        "get_tides", {"place": "Half Moon Bay", "day": "tomorrow"})
    assert infer_tool_call("And what about the day after?", "Half Moon Bay") is None  # no topic, no history
    assert infer_tool_call("And what about the day after?", "Half Moon Bay", "get_tides") == (
        "get_tides", {"place": "Half Moon Bay", "day": "day after"})
    assert infer_tool_call("What about Saturday?", "Pescadero", "get_weather") == (
        "get_weather", {"place": "Pescadero", "day": "saturday"})
    assert infer_tool_call("When's the next low tide?", "Pillar Point") == ("get_tides", {"place": "Pillar Point"})
    assert infer_tool_call("Tide times for Pillar Point today.", None) == (
        "get_tides", {"place": "Pillar Point", "day": "today"})
    assert infer_tool_call("What's the weather in San Mateo on Saturday?", None) == (
        "get_weather", {"place": "San Mateo", "day": "saturday"})
    assert infer_tool_call("When's golden hour at Pescadero tonight?", None) == (
        "get_sun_times", {"place": "Pescadero", "day": "tonight"})
    assert infer_tool_call("Who won the Giants game?", None) is None


def test_power_estimates(tmp_path):
    from kai_relay.power import PowerLog, Segment, mah_between, sleep_estimate_ma, soc

    assert soc(4200) == 100 and soc(3000) == 0 and 45 < soc(3830) < 50
    assert abs(mah_between(4200, 3270) - 250) < 1e-6
    assert sleep_estimate_ma(600, 3900, 3890) is None                    # under an hour: too short
    assert 0 < sleep_estimate_ma(8 * 3600, 3950, 3930) < 2               # 20 mV overnight ~ 1 mA
    seg = Segment(("d", "idle", "90", "True"), [i * 60 for i in range(31)], [3950 - i for i in range(31)])
    minutes, ma = seg.estimate()
    assert minutes == 30 and 15 < ma < 25                                # 30 mV in 30 min near 3.93 V = ~19 mA
    PowerLog(tmp_path).write("kai-1", "report", mode="idle", mv=3900, bogus=1)
    assert (tmp_path / "power.csv").read_text().splitlines()[0].startswith("ts,device,kind,mode")
