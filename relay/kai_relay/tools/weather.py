"""Weather, marine and sun/light conditions from Open-Meteo (free, no key, worldwide)."""

from datetime import date as date_cls
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import SunDirection, blue_hour, golden_hour

from . import geo
from .registry import ToolContext, ToolResult, card, short_time, tool

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


# WMO weather codes, short enough for the card.
SKY = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "T-storms, hail", 99: "T-storms, hail",
}


def sky(code: int | None) -> str:
    return SKY.get(code, "?") if code is not None else "?"


def compass(degrees: float | None) -> str:
    return "?" if degrees is None else COMPASS[round(degrees / 22.5) % 16]


async def _forecast(ctx: ToolContext, p: geo.Place) -> dict:
    imperial = ctx.settings.imperial
    r = await ctx.http.get(
        FORECAST_URL,
        params={
            "latitude": p.lat,
            "longitude": p.lon,
            "hourly": "temperature_2m,precipitation_probability,cloud_cover,wind_speed_10m,wind_gusts_10m,wind_direction_10m",
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto",
            "forecast_days": 4,
            "wind_speed_unit": "mph" if imperial else "kmh",
            "temperature_unit": "fahrenheit" if imperial else "celsius",
        },
    )
    r.raise_for_status()
    return r.json()


async def _marine(ctx: ToolContext, p: geo.Place) -> dict | None:
    r = await ctx.http.get(
        MARINE_URL,
        params={
            "latitude": p.lat,
            "longitude": p.lon,
            "hourly": "wave_height,swell_wave_height,swell_wave_period,sea_surface_temperature",
            "timezone": "auto",
            "forecast_days": 4,
            "length_unit": "imperial" if ctx.settings.imperial else "metric",
            "temperature_unit": "fahrenheit" if ctx.settings.imperial else "celsius",
        },
    )
    if r.status_code != 200:
        return None  # inland points have no marine data
    data = r.json()
    if all(v is None for v in data.get("hourly", {}).get("wave_height", [None])):
        return None
    return data


def _window(times: list[str], tz: ZoneInfo, day: str | None) -> list[int]:
    """Indexes of hourly rows to report: that day 5am-9pm, or the next 12 hours; every 2 hours."""
    parsed = [datetime.fromisoformat(t) for t in times]
    if day:
        target = date_cls.fromisoformat(day)
        idx = [i for i, t in enumerate(parsed) if t.date() == target and 5 <= t.hour <= 21]
    else:
        now = datetime.now(tz).replace(tzinfo=None) - timedelta(hours=1)
        idx = [i for i, t in enumerate(parsed) if now <= t <= now + timedelta(hours=13)]
    return idx[::2]


def _day_index(daily_times: list[str], day: str | None, tz: ZoneInfo) -> int:
    target = day or datetime.now(tz).date().isoformat()
    return daily_times.index(target) if target in daily_times else 0


@tool(
    "get_weather",
    "Weather for a place: current temperature and sky, today's high/low, rain chance, wind and gusts, "
    "and on the coast wave height, swell and water temperature (for fishing). Hourly for the next 12 hours, "
    "or for a given day.",
    {
        "place": {"type": "STRING", "description": "Spot, beach, lake or town. Omit for home."},
        "date": {"type": "STRING", "description": "Day YYYY-MM-DD (up to 3 days ahead). Omit for the next 12 hours."},
    },
)
async def get_weather(ctx: ToolContext, place: str | None = None, date: str | None = None) -> ToolResult:
    p = await geo.resolve(ctx, place)
    fc = await _forecast(ctx, p)
    marine = await _marine(ctx, p)
    tz = ZoneInfo(fc["timezone"])
    h = fc["hourly"]
    rows = _window(h["time"], tz, date)

    mh = marine["hourly"] if marine else None
    marine_idx = {t: i for i, t in enumerate(mh["time"])} if mh else {}
    hourly = []
    for i in rows:
        row = {
            "time": h["time"][i],
            "wind": h["wind_speed_10m"][i],
            "gusts": h["wind_gusts_10m"][i],
            "wind_from": compass(h["wind_direction_10m"][i]),
            "rain_pct": h["precipitation_probability"][i],
            "air_temp": h["temperature_2m"][i],
        }
        j = marine_idx.get(h["time"][i])
        if mh is not None and j is not None:
            row |= {
                "waves": mh["wave_height"][j],
                "swell": mh["swell_wave_height"][j],
                "swell_period_s": mh["swell_wave_period"][j],
                "water_temp": mh["sea_surface_temperature"][j],
            }
        hourly.append(row)

    d = _day_index(fc["daily"]["time"], date, tz)
    sunrise = datetime.fromisoformat(fc["daily"]["sunrise"][d])
    sunset = datetime.fromisoformat(fc["daily"]["sunset"][d])

    daily = fc["daily"]
    high, low = daily["temperature_2m_max"][d], daily["temperature_2m_min"][d]
    rain_max = daily["precipitation_probability_max"][d]
    cur = fc["current"]

    # Open-Meteo labels units "mp/h" and "°F"; the Stick's font has no degree sign.
    ws, deg = ("mph", "F") if ctx.settings.imperial else ("kmh", "C")
    lu = "ft" if ctx.settings.imperial else "m"
    # Most important first: temperature and sky, then the day's range, wind, sea, light.
    if date:
        lines = [f"{datetime.fromisoformat(date):%a} {sky(daily['weather_code'][d])}"]
    else:
        lines = [f"{cur['temperature_2m']:.0f}{deg} {sky(cur['weather_code'])}"]
    lines.append(f"Hi {high:.0f}{deg}  Lo {low:.0f}{deg}  Rain {rain_max or 0}%")
    if hourly:
        winds = [r["wind"] for r in hourly]
        gusts = max(r["gusts"] for r in hourly)
        lines.append(f"Wind {min(winds):.0f}-{max(winds):.0f}{ws} {hourly[0]['wind_from']} g{gusts:.0f}")
        if hourly[0].get("waves") is not None:
            lines.append(f"Waves {hourly[0]['waves']:.1f}{lu} @{hourly[0]['swell_period_s']:.0f}s")
    lines.append(f"Sun {short_time(sunrise)}-{short_time(sunset)}")

    return ToolResult(
        {
            "place": p.name,
            "units": {"wind": ws, "temp": deg, "waves": lu},
            "now": None if date else {
                "temp": cur["temperature_2m"],
                "feels_like": cur["apparent_temperature"],
                "sky": sky(cur["weather_code"]),
            },
            "day": {"high": high, "low": low, "rain_chance_pct": rain_max, "sky": sky(daily["weather_code"][d])},
            "sunrise": sunrise.strftime("%H:%M"),
            "sunset": sunset.strftime("%H:%M"),
            "hourly": hourly,
            "coastal": marine is not None,
        },
        card(p.name, lines),
    )


@tool(
    "get_sun_times",
    "Photography light for a place and day: sunrise, sunset, morning/evening golden hour and blue hour, "
    "plus cloud cover around sunrise and sunset.",
    {
        "place": {"type": "STRING", "description": "Photo location. Omit for home."},
        "date": {"type": "STRING", "description": "Day YYYY-MM-DD. Omit for today."},
    },
)
async def get_sun_times(ctx: ToolContext, place: str | None = None, date: str | None = None) -> ToolResult:
    p = await geo.resolve(ctx, place)
    fc = await _forecast(ctx, p)
    tz = ZoneInfo(fc["timezone"])
    day = date_cls.fromisoformat(date) if date else datetime.now(tz).date()
    obs = Observer(latitude=p.lat, longitude=p.lon)

    def span(fn, direction):
        try:
            start, end = fn(obs, day, direction, tzinfo=tz)
            return start, end
        except ValueError:  # polar day/night: the sun never reaches that elevation
            return None

    gh_am, gh_pm = span(golden_hour, SunDirection.RISING), span(golden_hour, SunDirection.SETTING)
    bh_am, bh_pm = span(blue_hour, SunDirection.RISING), span(blue_hour, SunDirection.SETTING)

    d = _day_index(fc["daily"]["time"], day.isoformat(), tz)
    sunrise = datetime.fromisoformat(fc["daily"]["sunrise"][d])
    sunset = datetime.fromisoformat(fc["daily"]["sunset"][d])

    def cloud_at(t: datetime) -> int | None:
        key = t.replace(minute=0).strftime("%Y-%m-%dT%H:%M")
        times = fc["hourly"]["time"]
        return fc["hourly"]["cloud_cover"][times.index(key)] if key in times else None

    fmt = lambda s: f"{short_time(s[0])}-{short_time(s[1])}" if s else "none"  # noqa: E731
    lines = [
        f"Rise {short_time(sunrise)}  Set {short_time(sunset)}",
        f"Gold AM {fmt(gh_am)}",
        f"Gold PM {fmt(gh_pm)}",
        f"Blue PM {fmt(bh_pm)}",
    ]
    clouds_set = cloud_at(sunset)
    if clouds_set is not None:
        lines.append(f"Clouds at sunset {clouds_set}%")

    iso = lambda s: {"start": s[0].strftime("%H:%M"), "end": s[1].strftime("%H:%M")} if s else None  # noqa: E731
    return ToolResult(
        {
            "place": p.name,
            "date": day.isoformat(),
            "sunrise": sunrise.strftime("%H:%M"),
            "sunset": sunset.strftime("%H:%M"),
            "golden_hour_morning": iso(gh_am),
            "golden_hour_evening": iso(gh_pm),
            "blue_hour_morning": iso(bh_am),
            "blue_hour_evening": iso(bh_pm),
            "cloud_cover_pct_at_sunrise": cloud_at(sunrise),
            "cloud_cover_pct_at_sunset": clouds_set,
        },
        card(f"Light {p.name}", lines),
    )
