"""The morning briefing: a short, code-built summary of the day (no Claude call).

Lines only appear when there's something to say, and a source that fails is left out
(or says it couldn't check) rather than faked: no invented weather, and a calendar that
couldn't be read never reads as "nothing today".
"""
import asyncio
import logging
import time as _time
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app import calendar_svc, db, location, money, prayer
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

# (lat, lng) -> (fetched_at, parsed weather or None). Keyed by place for travel mode.
_weather_cache: dict[tuple[float, float], tuple[float, dict | None]] = {}
WEATHER_TTL = 30 * 60


async def _fetch_weather(lat: float, lng: float, tz: str) -> dict:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lng,
            "current": "temperature_2m,apparent_temperature,weather_code,is_day",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
            "timezone": tz, "forecast_days": 2,       # 2 days so an evening look still gets 12 hours
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


HOURS_AHEAD = 12


def _hours(data: dict) -> list[dict]:
    """The next HOURS_AHEAD hours after now: [{"hour": "15:00", "temp", "kind", "night", "rain"}]."""
    h = data.get("hourly") or {}
    times = h.get("time") or []
    now = (data["current"].get("time") or "")[:13]          # "2026-10-13T14" (local, from Open-Meteo)
    out = []
    for i, ts in enumerate(times):
        if ts[:13] <= now:
            continue
        try:
            out.append({"hour": ts[11:16], "temp": round(h["temperature_2m"][i]),
                        "kind": _kind(h["weather_code"][i]), "night": not h["is_day"][i],
                        "rain": (h["precipitation_probability"][i] or 0)})
        except (KeyError, IndexError, TypeError):
            break
        if len(out) == HOURS_AHEAD:
            break
    return out


def parse_weather(data: dict) -> dict:
    """Open-Meteo response -> the numbers the briefing shows."""
    cur, day = data["current"], data["daily"]
    rain = day.get("precipitation_probability_max", [None])[0]
    return {
        "night": cur.get("is_day") == 0,
        "hours": _hours(data),
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


async def weather(loc: "location.Location | None") -> dict | None:
    """Cached 30 min per place. None (left out) without a place or if the API fails:
    never invented."""
    if loc is None:
        return None
    key = (round(loc.lat, 3), round(loc.lng, 3))
    cached = _weather_cache.get(key)
    if cached and _time.monotonic() - cached[0] < WEATHER_TTL:
        return cached[1]
    try:
        w = parse_weather(await _fetch_weather(loc.lat, loc.lng, loc.tz))
    except Exception:
        log.warning("weather fetch failed; briefing goes without it", exc_info=True)
        w = None
    _weather_cache[key] = (_time.monotonic(), w)
    return w


async def money_for(member_id: int) -> dict:
    """{"net": cents, "transfers": [{"direction": "pay"|"receive", "name", "cents"}]} for one
    member, from the suite's fewest-transfers settle-up plan."""
    expenses, shares, settlements = await db.money_ledger()
    members = await db.all_members()
    names = {m["id"]: m["name"] for m in members}
    net = money.balances(list(names), expenses, shares, settlements)
    transfers = []
    for frm, to, cents in money.settle_up(net):
        if frm == member_id:
            transfers.append({"direction": "pay", "name": names[to], "cents": cents})
        elif to == member_id:
            transfers.append({"direction": "receive", "name": names[frm], "cents": cents})
    return {"net": net.get(member_id, 0), "transfers": transfers}


# Greetings, the suite's own. (main, tail, takes the name). The tail is part of the
# phrase; the app sets it smaller under the main line. A phrase that's complete on its own
# (a rhyme, or one that already addresses you) never gets "يا <name>".
_WEEKDAY_MORNING = [("صباح الفل", "", True), ("صباح الورد", "", True), ("صحي النوم", "", True)]
_WEEKDAY_EVENING = [("مساء الفل", "", True), ("مساء العسل", "", True), ("مسا مسا", "", False)]
_WEEKEND_MORNING = [("صباح الورد والتجلي", "حسب التوقيت المحلي", False),
                    ("صباح الفل", "يا سيد الكل", False)]
_WEEKEND_EVENING = [("مساء العسل", "والدلع والكسل", False)]
_WEEKEND_NIGHT = _WEEKEND_EVENING + [("الشمس نزلت", "وانت لسا منور", False)]   # after sunset only


def greeting(local: datetime, member_id: int = 0) -> dict:
    """{"main", "tail", "named"} for this moment. Morning is 4:00-11:59; Saturday and Sunday
    get the weekend phrases. The pick is fixed for the person, day and half of the day, so
    it doesn't change on every refresh."""
    morning = 4 <= local.hour < 12
    weekend = local.weekday() >= 5
    if morning:
        pool = _WEEKEND_MORNING if weekend else _WEEKDAY_MORNING
    elif weekend:
        pool = _WEEKEND_NIGHT if (local.hour >= 18 or local.hour < 4) else _WEEKEND_EVENING
    else:
        pool = _WEEKDAY_EVENING
    main, tail, named = pool[zlib.crc32(f"{local.date()}|{morning}|{member_id}".encode()) % len(pool)]
    return {"main": main, "tail": tail, "named": named}


def greeting_text(g: dict, name: str) -> str:
    return " ".join(x for x in (g["main"], g["tail"], f"يا {name}" if g["named"] else "") if x)


async def gather(member, now: datetime | None = None) -> dict:
    """Everything in the member's briefing for `now`'s local day, as data. The app renders
    this as a card; notifications and Telegram get render_text() of the same data."""
    tz = ZoneInfo(settings.timezone)
    loc = await location.location_for(member["id"])
    # The member's own clock: a traveller's "today" and prayer times are where they are.
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(loc.tz) if loc else tz)
    today = local.date()

    # The two slow outside calls (Google Calendar, the weather) run while the database
    # work happens, instead of one after another.
    async def todays_events():
        if not calendar_svc.is_configured():
            return [], True
        try:
            # The same 7-day window the Today screen reads, so one cached read serves both.
            evs = await read_calendar(7, cached=True)
        except calendar_svc.CalendarReadError:
            return [], False
        out = []
        for e in evs:
            w = event_when(e)
            if w["date"] == today.isoformat():
                out.append({"summary": e["summary"], "time": w["time"]})
        return out, True

    calendar_task = asyncio.create_task(todays_events())
    weather_task = asyncio.create_task(weather(loc))

    from app import timetable
    day_start = datetime.combine(today, datetime.min.time(), tz)
    my_classes = timetable.busy_on(today, await db.classes_for(member["id"]), [], tz,
                                   await db.class_breaks(member["id"], since=today))
    upcoming = await db.exams_between(day_start, day_start + timedelta(days=8))

    def exam_out(e):
        at = e["starts_at"].astimezone(tz)
        days = (at.date() - today).days
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        return {"name": e["name"], "course": e["course"], "kind": e["kind"], "when": when,
                "time": at.strftime("%H:%M"), "day": at.strftime("%a %d %b")}

    my_exams = [exam_out(e) for e in upcoming if e["member_id"] == member["id"]]
    others_exams = [exam_out(e) for e in upcoming if e["member_id"] != member["id"]
                    and (e["starts_at"].astimezone(tz).date() - today).days <= 1]

    from app import pantry, polls
    fridge = []
    for r in await db.open_pantry():
        if r["owner_id"] not in (None, member["id"]):
            continue                     # someone else's food isn't your briefing
        e = pantry.expiry(r["expires_on"], today)
        if e["state"] in ("soon", "expired"):
            fridge.append({"item": r["item"], "label": e["label"], "state": e["state"]})
    waiting_polls = [{"id": t["id"], "question": t["question"]} for t in await polls.overview()
                     if not t["closed"] and member["id"] not in t["voted"]]

    mine = await db.get_open_packages(member["id"])
    others = [s for s in await db.current_statuses() if s["member_id"] != member["id"]]
    prayers = []
    if prayer.is_configured() and loc is not None:
        times = prayer.times_at(today, loc)
        prayers = [{"name": prayer.NAMES[n][1], "label": prayer.clock12(times[n])}
                   for n in prayer.PRAYERS if times[n] > local]

    events, calendar_ok = await calendar_task
    g = greeting(local, member["id"])
    name = member["arabic_name"] or member["name"]
    return {
        "greeting": greeting_text(g, name),
        "greeting_main": g["main"],
        "greeting_tail": g["tail"],
        "greeting_named": g["named"],
        "name": name,
        "date": today.isoformat(),
        "day": local.strftime("%A %d %B"),
        "place": None if loc is None or loc.is_home else loc.label,
        "weather": await weather_task,
        "events": events,
        "calendar_ok": calendar_ok,
        "classes": [{"what": c.what, "from": c.start.strftime("%H:%M"), "to": c.end.strftime("%H:%M")}
                    for c in my_classes],
        "my_exams": my_exams,
        "others_exams": others_exams,
        "waiting": [p["description"] for p in mine if p["status"] == "arrived"],
        "arriving": [p["description"] for p in mine if p["status"] == "expected" and p["expected_on"] == today],
        "groceries": len(await db.get_shopping_list()),
        "fridge": fridge,
        "polls": waiting_polls,
        "statuses": [{"name": s["name"], "text": s["text"],
                      "until": s["until_at"].astimezone(tz).strftime("%a %H:%M") if s["until_at"] else None}
                     for s in others],
        "prayers": prayers,
        "money": await money_for(member["id"]),
    }


def render_text(b: dict) -> tuple[str, str]:
    """(title, body) for a notification or Telegram message. Lines only when there's
    something to say; a calendar that couldn't be read says so (never "nothing today")."""
    lines: list[str] = []
    if b["place"]:
        lines.append(f"In {b['place']} (travel mode): weather and prayer times are for there.")
    if b["weather"]:
        lines.append(weather_text(b["weather"]))
    if not b["calendar_ok"]:
        lines.append("Couldn't check the calendar this morning.")
    elif b["events"]:
        lines.append("Today: " + "; ".join(e["summary"] + (f" at {e['time']}" if e["time"] else "")
                                         for e in b["events"]))
    if b["classes"]:
        lines.append("Classes: " + ", ".join(f"{c['what']} {c['from']}" for c in b["classes"]))
    for e in b["my_exams"]:
        lines.append(f"Your {e['course']} {e['kind']}: {e['when']} ({e['day']} {e['time']})")
    for e in b["others_exams"]:
        lines.append(f"{e['name']} has a {e['course']} {e['kind']} {e['when']} at {e['time']}")
    if b["waiting"]:
        lines.append("Waiting for you at the building: " + ", ".join(b["waiting"]))
    if b["arriving"]:
        lines.append("Arriving today: " + ", ".join(b["arriving"]))
    if b["groceries"]:
        lines.append(f"Groceries: {b['groceries']} item{'s' if b['groceries'] != 1 else ''} on the list")
    soon = [f"{f['item']} ({f['label']})" for f in b["fridge"] if f["state"] == "soon"]
    gone = [f"{f['item']} ({f['label']})" for f in b["fridge"] if f["state"] == "expired"]
    if soon:
        lines.append("Use soon: " + ", ".join(soon))
    if gone:
        lines.append("Expired, toss or check: " + ", ".join(gone))
    for q in b["polls"]:
        lines.append(f"Waiting for your vote: {q['question']}")
    for t in b["money"]["transfers"]:
        if t["cents"] >= 100:   # under $1 isn't worth a line
            lines.append(f"You owe {t['name']} {money.fmt(t['cents'])}" if t["direction"] == "pay"
                         else f"{t['name']} owes you {money.fmt(t['cents'])}")
    for s in b["statuses"]:
        lines.append(f"{s['name']}: {s['text']}" + (f" until {s['until']}" if s["until"] else ""))
    if b["prayers"]:
        lines.append("Prayers: " + ", ".join(f"{p['name']} {p['label']}" for p in b["prayers"]))
    title = b["greeting"] + (" ☀️" if b["greeting_main"].startswith("صباح") else "")
    return title, "\n".join(lines) if lines else "Nothing on today. Enjoy it."


async def build(member, now: datetime | None = None) -> tuple[str, str]:
    """(title, body) of the member's briefing for `now`'s local day."""
    return render_text(await gather(member, now))
