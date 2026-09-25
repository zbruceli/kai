"""Hit the real free APIs (NOAA, Open-Meteo). Run with: uv run pytest -m network"""
import asyncio

import pytest

from kai_relay import tools
from .test_tools import ctx  # noqa: F401

pytestmark = pytest.mark.network


def test_tides_half_moon_bay(ctx):  # noqa: F811
    r = asyncio.run(tools.call("get_tides", {"place": "Half Moon Bay"}, ctx))
    assert "tides" in r.data, r.data
    print(r.card)


def test_weather_coastal(ctx):  # noqa: F811
    r = asyncio.run(tools.call("get_weather", {}, ctx))
    assert r.data.get("coastal") is True, r.data
    print(r.card)


def test_sun_times(ctx):  # noqa: F811
    r = asyncio.run(tools.call("get_sun_times", {"place": "Pescadero"}, ctx))
    assert r.data.get("golden_hour_evening"), r.data
    print(r.card)


def test_weather_inland_named_day(ctx):  # noqa: F811
    r = asyncio.run(tools.call("get_weather", {"place": "San Mateo, CA", "day": "tomorrow"}, ctx))
    assert r.data["day"]["high"] is not None, r.data
    print(r.card)


def test_tides_tomorrow_is_one_day(ctx):  # noqa: F811
    from datetime import date, timedelta

    r = asyncio.run(tools.call("get_tides", {"place": "Half Moon Bay", "day": "tomorrow"}, ctx))
    tomorrow = date.today() + timedelta(days=1)
    assert r.card["title"] == "Tides tomorrow", r.card
    assert all(t["time"].startswith(f"{tomorrow:%a}") for t in r.data["tides"]), r.data
    print(r.card)
