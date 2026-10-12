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


# A coarse sky "kind" for the app's weather icon.
def _kind(code: int) -> str:
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "rain"


def parse_weather(data: dict) -> dict:
    """Open-Meteo response -> the numbers the briefing shows."""
    cur, day = data["current"], data["daily"]
    rain = day.get("precipitation_probability_max", [None])[0]
    return {
        "temp": round(cur["temperature_2m"]),
        "feels": round(cur["apparent_temperature"]),
        "sky": _WMO.get(cur["weather_code"], ""),
        "kind": _kind(cur["weather_code"]),
        "high": round(day["temperature_2m_max"][0]),
        "low": round(day["temperature_2m_min"][0]),
        "rain": rain if rain is not None and rain >= 30 else None,   # only worth mentioning at 30%+
    }


def weather_text(w: dict) -> str:
    line = f"Weather: {w['temp']}°" + (f", feels like {w['feels']}°" if w["feels"] != w["temp"] else "") \
        + (f", {w['sky']}" if w["sky"] else "")
    line += f". High {w['high']}°, low {w['low']}°"
    if w["rain"] is not None:
        line += f", {w['rain']}% chance of rain or snow"
    return line + "."


async def weather() -> dict | None:
    """Cached 30 min. None (left out) if home isn't set or the API fails: never invented."""
    global _weather_cache
    if settings.home_lat is None or settings.home_lng is None:
        return None
    if _weather_cache and _time.monotonic() - _weather_cache[0] < WEATHER_TTL:
        return _weather_cache[1]
    try:
        w = parse_weather(await _fetch_weather())
    except Exception:
        log.warning("weather fetch failed; briefing goes without it", exc_info=True)
        w = None
    _weather_cache = (_time.monotonic(), w)
    return w


def greeting(local: datetime) -> str:
    if local.hour < 12:
        return "صباح الخير"
    if local.hour < 18:
        return "أهلاً"
    return "مساء الخير"


async def gather(member, now: datetime | None = None) -> dict:
    """Everything in the member's briefing for `now`'s local day, as data. The app renders
    this as a card; notifications and Telegram get render_text() of the same data."""
    tz = ZoneInfo(settings.timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = local.date()

    events, calendar_ok = [], True
    if calendar_svc.is_configured():
        try:
            for e in await read_calendar(1):
                w = event_when(e)
                if w["date"] == today.isoformat():
                    events.append({"summary": e["summary"], "time": w["time"]})
        except calendar_svc.CalendarReadError:
            calendar_ok = False

    mine = await db.get_open_packages(member["id"])
    others = [s for s in await db.current_statuses() if s["member_id"] != member["id"]]
    prayers = []
    if prayer.is_configured():
        times = prayer.times_for(today)
        prayers = [{"name": prayer.NAMES[n][1], "label": prayer.clock12(times[n])}
                   for n in prayer.PRAYERS if times[n] > local]

    return {
        "greeting": greeting(local),
        "name": member["arabic_name"] or member["name"],
        "date": today.isoformat(),
        "day": local.strftime("%A %d %B"),
        "weather": await weather(),
        "events": events,
        "calendar_ok": calendar_ok,
        "waiting": [p["description"] for p in mine if p["status"] == "arrived"],
        "arriving": [p["description"] for p in mine if p["status"] == "expected" and p["expected_on"] == today],
        "groceries": len(await db.get_shopping_list()),
        "statuses": [{"name": s["name"], "text": s["text"],
                      "until": s["until_at"].astimezone(tz).strftime("%a %H:%M") if s["until_at"] else None}
                     for s in others],
        "prayers": prayers,
    }


def render_text(b: dict) -> tuple[str, str]:
    """(title, body) for a notification or Telegram message. Lines only when there's
    something to say; a calendar that couldn't be read says so (never "nothing today")."""
    lines: list[str] = []
    if b["weather"]:
        lines.append(weather_text(b["weather"]))
    if not b["calendar_ok"]:
        lines.append("Couldn't check the calendar this morning.")
    elif b["events"]:
        lines.append("Today: " + "; ".join(e["summary"] + (f" at {e['time']}" if e["time"] else "")
                                         for e in b["events"]))
    if b["waiting"]:
        lines.append("Waiting for you at the building: " + ", ".join(b["waiting"]))
    if b["arriving"]:
        lines.append("Arriving today: " + ", ".join(b["arriving"]))
    if b["groceries"]:
        lines.append(f"Groceries: {b['groceries']} item{'s' if b['groceries'] != 1 else ''} on the list")
    for s in b["statuses"]:
        lines.append(f"{s['name']}: {s['text']}" + (f" until {s['until']}" if s["until"] else ""))
    if b["prayers"]:
        lines.append("Prayers: " + ", ".join(f"{p['name']} {p['label']}" for p in b["prayers"]))
    title = f"{b['greeting']} يا {b['name']}" + (" ☀️" if b["greeting"] == "صباح الخير" else "")
    return title, "\n".join(lines) if lines else "Nothing on today. Enjoy it."


async def build(member, now: datetime | None = None) -> tuple[str, str]:
    """(title, body) of the member's briefing for `now`'s local day."""
    return render_text(await gather(member, now))
