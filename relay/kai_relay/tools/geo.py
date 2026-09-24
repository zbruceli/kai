import math
from dataclasses import dataclass

from .registry import ToolContext, ToolError

HERE_WORDS = {"here", "home", "near me", "nearby", "current location", "my location", "around here"}


@dataclass
class Place:
    name: str
    lat: float
    lon: float


async def resolve(ctx: ToolContext, place: str | None) -> Place:
    """Turn a spoken place name into coordinates. Empty or 'here' means the configured home."""
    s = ctx.settings
    if not place or place.strip().lower() in HERE_WORDS:
        if s.home_lat is None or s.home_lon is None:
            raise ToolError("I don't know where home is yet. Set KAI_HOME_LAT and KAI_HOME_LON on the relay.")
        return Place(s.home_name, s.home_lat, s.home_lon)

    if s.maps_api_key:
        # Places Text Search handles landmarks and spots ("Pigeon Point Lighthouse") far better than city geocoders.
        body: dict = {"textQuery": place, "maxResultCount": 1}
        if s.home_lat is not None:
            body["locationBias"] = {
                "circle": {"center": {"latitude": s.home_lat, "longitude": s.home_lon}, "radius": 50000.0}
            }
        r = await ctx.http.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={"X-Goog-Api-Key": s.maps_api_key, "X-Goog-FieldMask": "places.displayName,places.location"},
            json=body,
        )
        r.raise_for_status()
        found = r.json().get("places") or []
        if found:
            p = found[0]
            return Place(p["displayName"]["text"], p["location"]["latitude"], p["location"]["longitude"])

    r = await ctx.http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1})
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        raise ToolError(f"I couldn't find a place called {place}.")
    g = results[0]
    return Place(g["name"], g["latitude"], g["longitude"])


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))
