"""Tide predictions from NOAA CO-OPS (free, no key, US coasts and territories)."""

import json
import time
from datetime import datetime

from . import geo
from .registry import ToolContext, ToolError, ToolResult, card, short_time, tool

STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATION_CACHE_DAYS = 30
MAX_STATION_KM = 100


async def stations(ctx: ToolContext) -> list[dict]:
    if "noaa_stations" in ctx.cache:
        return ctx.cache["noaa_stations"]
    path = ctx.settings.data_dir / "noaa_stations.json"
    if path.exists() and time.time() - path.stat().st_mtime < STATION_CACHE_DAYS * 86400:
        data = json.loads(path.read_text())
    else:
        r = await ctx.http.get(STATIONS_URL, params={"type": "tidepredictions"})
        r.raise_for_status()
        data = [
            {"id": s["id"], "name": s["name"], "lat": float(s["lat"]), "lon": float(s["lng"])}
            for s in r.json()["stations"]
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    ctx.cache["noaa_stations"] = data
    return data


def nearest_station(all_stations: list[dict], lat: float, lon: float) -> tuple[dict, float]:
    best = min(all_stations, key=lambda s: geo.distance_km(lat, lon, s["lat"], s["lon"]))
    return best, geo.distance_km(lat, lon, best["lat"], best["lon"])


@tool(
    "get_tides",
    "High and low tide times near a place, from the nearest NOAA station (US coasts only).",
    {
        "place": {"type": "STRING", "description": "Beach, harbor, pier or town. Omit for home."},
        "date": {"type": "STRING", "description": "Start date YYYY-MM-DD. Omit for today."},
        "days": {"type": "INTEGER", "description": "Number of days, 1-3. Default 2."},
    },
)
async def get_tides(ctx: ToolContext, place: str | None = None, date: str | None = None, days: int = 2) -> ToolResult:
    p = await geo.resolve(ctx, place)
    station, dist = nearest_station(await stations(ctx), p.lat, p.lon)
    if dist > MAX_STATION_KM:
        raise ToolError(f"There's no NOAA tide station within {MAX_STATION_KM} km of {p.name}. I only cover US tides so far.")

    start = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    imperial = ctx.settings.imperial
    r = await ctx.http.get(
        DATA_URL,
        params={
            "product": "predictions",
            "application": "kai",
            "station": station["id"],
            "begin_date": start.strftime("%Y%m%d"),
            "range": 24 * max(1, min(int(days), 3)),
            "datum": "MLLW",
            "interval": "hilo",
            "time_zone": "lst_ldt",
            "units": "english" if imperial else "metric",
            "format": "json",
        },
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise ToolError(f"NOAA says: {body['error'].get('message', 'no data')}")

    unit = "ft" if imperial else "m"
    tides = []
    for pred in body.get("predictions", []):
        when = datetime.strptime(pred["t"], "%Y-%m-%d %H:%M")
        tides.append({"time": when, "type": "high" if pred["type"] == "H" else "low", "height": float(pred["v"])})

    # Skip tides that already happened today so the card starts with the next one.
    now = datetime.now()
    upcoming = [t for t in tides if t["time"] >= now] if not date else tides
    lines = [f"{t['time']:%a} {short_time(t['time'])} {'High' if t['type'] == 'high' else 'Low '} {t['height']:.1f}{unit}" for t in upcoming]

    return ToolResult(
        {
            "station": station["name"],
            "station_distance_km": round(dist, 1),
            "units": unit,
            "datum": "MLLW",
            "local_times": True,
            "tides": [{**t, "time": t["time"].strftime("%a %Y-%m-%d %H:%M")} for t in upcoming],
        },
        card(f"Tides {station['name']}", lines),
    )
