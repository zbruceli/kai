import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    device_token: str
    model: str
    voice: str
    home_name: str
    home_lat: float | None
    home_lon: float | None
    units: str  # "imperial" | "metric"
    maps_api_key: str | None
    host: str
    port: int
    data_dir: Path
    notes_dir: Path
    idle_close_s: int

    @property
    def imperial(self) -> bool:
        return self.units == "imperial"


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value else None


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ

    missing = [k for k in ("GEMINI_API_KEY", "KAI_DEVICE_TOKEN") if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)} (see .env.example)")

    if env["KAI_DEVICE_TOKEN"] == "change-me" or len(env["KAI_DEVICE_TOKEN"]) < 12:
        raise SystemExit("KAI_DEVICE_TOKEN is the example value or too short; generate one with:\n"
                         "  python3 -c 'import secrets; print(secrets.token_urlsafe(18))'")

    units = env.get("KAI_UNITS", "imperial").lower()
    if units not in ("imperial", "metric"):
        raise SystemExit("KAI_UNITS must be 'imperial' or 'metric'")

    data_dir = Path(env.get("KAI_DATA_DIR", "./data")).expanduser()
    return Settings(
        gemini_api_key=env["GEMINI_API_KEY"],
        device_token=env["KAI_DEVICE_TOKEN"],
        model=env.get("KAI_MODEL", "gemini-3.8-live"),
        voice=env.get("KAI_VOICE", "Puck"),
        home_name=env.get("KAI_HOME_NAME", "Home"),
        home_lat=_float_or_none(env.get("KAI_HOME_LAT")),
        home_lon=_float_or_none(env.get("KAI_HOME_LON")),
        units=units,
        maps_api_key=env.get("GOOGLE_MAPS_API_KEY") or None,
        host=env.get("KAI_HOST", "0.0.0.0"),
        port=int(env.get("KAI_PORT", "8765")),
        data_dir=data_dir,
        notes_dir=Path(env.get("KAI_NOTES_DIR", str(data_dir / "notes"))).expanduser(),
        idle_close_s=int(env.get("KAI_IDLE_CLOSE_S", "300")),
    )
