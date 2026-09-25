"""Nearest parking via Google Places API (New). Needs GOOGLE_MAPS_API_KEY."""

from . import geo
from .registry import ToolContext, ToolError, ToolResult, card, tool

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
FIELDS = "places.displayName,places.formattedAddress,places.location,places.currentOpeningHours.openNow,places.rating"
WALK_M_PER_MIN = 80


@tool(
    "find_parking",
    "Find the closest parking lots and garages to a place. Call it for every parking question, including repeats.",
    {
        "place": {"type": "STRING", "description": "Destination, e.g. 'Ferry Building' or 'Pigeon Point Lighthouse'. Omit for home."},
        "radius_m": {"type": "INTEGER", "description": "Search radius in meters, default 1500."},
    },
)
async def find_parking(ctx: ToolContext, place: str | None = None, radius_m: int = 1500) -> ToolResult:
    key = ctx.settings.maps_api_key
    if not key:
        raise ToolError("Parking search needs a Google Maps API key on the relay.")
    p = await geo.resolve(ctx, place)
    r = await ctx.http.post(
        NEARBY_URL,
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS},
        json={
            "includedTypes": ["parking"],
            "maxResultCount": 5,
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": p.lat, "longitude": p.lon},
                    "radius": float(max(100, min(int(radius_m), 50000))),
                }
            },
        },
    )
    r.raise_for_status()
    found = r.json().get("places") or []
    if not found:
        raise ToolError(f"I couldn't find parking within {radius_m} meters of {p.name}.")

    spots = []
    for s in found:
        meters = geo.distance_km(p.lat, p.lon, s["location"]["latitude"], s["location"]["longitude"]) * 1000
        spots.append(
            {
                "name": s["displayName"]["text"],
                "address": s.get("formattedAddress"),
                "distance_m": round(meters),
                "walk_min": max(1, round(meters / WALK_M_PER_MIN)),
                "open_now": s.get("currentOpeningHours", {}).get("openNow"),
                "rating": s.get("rating"),
            }
        )

    def line(s: dict) -> str:
        dist = f"{s['distance_m']}m" if s["distance_m"] < 1000 else f"{s['distance_m'] / 1000:.1f}km"
        closed = " (closed)" if s["open_now"] is False else ""
        return f"{dist} {s['name']}{closed}"

    return ToolResult({"near": p.name, "parking": spots}, card(f"Parking {p.name}", [line(s) for s in spots]))
