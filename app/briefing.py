"""The morning briefing: a short, code-built summary of the day (no Claude call).

Lines only appear when there's something to say, and a source that fails is left out
(or says it couldn't check) rather than faked: no invented weather, and a calendar that
couldn't be read never reads as "nothing today".
"""
import logging
import time as _time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from app import calendar_svc, db, prayer
from app.config import settings
from app.tools import event_when, read_calendar

log = logging.getLogger("abdo")

# WMO weather codes (Open-Meteo) -> a short phrase.
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}

_weather_cache: tuple[float, str | None] | None = None
WEATHER_TTL = 30 * 60


async def _fetch_weather() -> dict:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": settings.home_lat, "longitude": settings.home_lng,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": settings.timezone, "forecast_days": 1,
        })
        r.raise_for_status()
        return r.json()


def weather_line(data: dict) -> str:
    cur, day = data["current"], data["daily"]
    temp, feels = round(cur["temperature_2m"]), round(cur["apparent_temperature"])
    sky = _WMO.get(cur["weather_code"], "")
    line = f"Weather: {temp}°" + (f", feels like {feels}°" if feels != temp else "") + (f", {sky}" if sky else "")
    hi, lo = round(day["temperature_2m_max"][0]), round(day["temperature_2m_min"][0])
    line += f". High {hi}°, low {lo}°"
    rain = day.get("precipitation_probability_max", [None])[0]
    if rain is not None and rain >= 30:
        line += f", {rain}% chance of rain or snow"
    return line + "."


async def weather() -> str | None:
    """Cached 30 min. None (line omitted) if home isn't set or the API fails."""
    global _weather_cache
    if settings.home_lat is None or settings.home_lng is None:
        return None
    if _weather_cache and _time.monotonic() - _weather_cache[0] < WEATHER_TTL:
        return _weather_cache[1]
    try:
        line = weather_line(await _fetch_weather())
    except Exception:
        log.warning("weather fetch failed; briefing goes without it", exc_info=True)
        line = None
    _weather_cache = (_time.monotonic(), line)
    return line


async def build(member, now: datetime | None = None) -> tuple[str, str]:
    """(title, body) of the member's briefing for `now`'s local day."""
    tz = ZoneInfo(settings.timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = local.date()
    lines: list[str] = []

    w = await weather()
    if w:
        lines.append(w)

    if calendar_svc.is_configured():
        try:
            events = [e for e in await read_calendar(1) if event_when(e)["date"] == today.isoformat()]
            if events:
                parts = [f"{e['summary']}" + (f" at {event_when(e)['time']}" if event_when(e)["time"] else "")
                         for e in events]
                lines.append("Today: " + "; ".join(parts))
        except calendar_svc.CalendarReadError:
            lines.append("Couldn't check the calendar this morning.")

    mine = await db.get_open_packages(member["id"])
    waiting = [p["description"] for p in mine if p["status"] == "arrived"]
    arriving = [p["description"] for p in mine if p["status"] == "expected" and p["expected_on"] == today]
    if waiting:
        lines.append("Waiting for you at the building: " + ", ".join(waiting))
    if arriving:
        lines.append("Arriving today: " + ", ".join(arriving))

    items = await db.get_shopping_list()
    if items:
        lines.append(f"Groceries: {len(items)} item{'s' if len(items) != 1 else ''} on the list")

    others = [s for s in await db.current_statuses() if s["member_id"] != member["id"]]
    for s in others:
        until = f" until {s['until_at'].astimezone(tz).strftime('%a %H:%M')}" if s["until_at"] else ""
        lines.append(f"{s['name']}: {s['text']}{until}")

    if prayer.is_configured():
        times = prayer.times_for(today)
        upcoming = [f"{prayer.NAMES[n][1]} {prayer.clock12(times[n])}" for n in prayer.PRAYERS if times[n] > local]
        if upcoming:
            lines.append("Prayers: " + ", ".join(upcoming))

    title = f"صباح الخير يا {member['arabic_name'] or member['name']} ☀️"
    return title, "\n".join(lines) if lines else "Nothing on today. Enjoy it."
