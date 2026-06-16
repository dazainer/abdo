import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.config import settings

# Named calendar_svc (not calendar) so it doesn't shadow Python's stdlib calendar.
# Built lazily on first use so the app still boots before the calendar is configured.
_service = None


class CalendarReadError(Exception):
    """A read against Google Calendar failed or was degraded (e.g. the Regional
    Access Boundary / FAILED_PRECONDITION path). Distinct from a genuinely empty
    result — a failed read must NEVER be treated as 'no events', because that's
    what lets the model create a duplicate of an event it just couldn't see."""


class EventNotFound(Exception):
    """No event with the given id exists on the calendar. Raised so the model is
    told plainly rather than fabricating an id or claiming a phantom success."""


def is_configured() -> bool:
    return bool(settings.google_service_account_json and settings.google_calendar_id)


def _get_service():
    global _service
    if _service is None:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(settings.google_service_account_json),
            # calendar.events = read + create/edit events (least privilege that allows
            # writes). Deliberately NOT the broader 'calendar', which can delete calendars.
            scopes=["https://www.googleapis.com/auth/calendar.events"],
        )
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


# --- Deterministic date/time resolution --------------------------------------
# The model must NEVER compute a date by arithmetic (it gets weekdays wrong, e.g.
# "Wednesday" -> "18 June" when the 18th is a Thursday). Instead the model passes
# the spoken day (a weekday name, today/tomorrow/..., next <weekday>, or an exact
# date copied from the prompt's reference) and Python turns it into a real Cairo
# date here.

# Python's date.weekday(): Monday=0 .. Sunday=6.
_WEEKDAYS = {
    "monday": 0, "mon": 0, "الاثنين": 0, "الإثنين": 0, "اتنين": 0, "etneen": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "الثلاثاء": 1, "التلات": 1, "talat": 1, "talt": 1,
    "wednesday": 2, "wed": 2, "الأربعاء": 2, "الاربعاء": 2, "الاربع": 2, "arba3": 2, "arbe3": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "الخميس": 3, "khamees": 3, "khamis": 3,
    "friday": 4, "fri": 4, "الجمعة": 4, "gom3a": 4, "gomaa": 4, "gum3a": 4,
    "saturday": 5, "sat": 5, "السبت": 5, "sabt": 5,
    "sunday": 6, "sun": 6, "الأحد": 6, "الاحد": 6, "7ad": 6, "had": 6,
}

_TODAY = {"today", "tonight", "النهاردة", "النهارده", "اليوم", "الليلة", "el naharda",
          "elnaharda", "innaharda", "delwa2ti"}
_TOMORROW = {"tomorrow", "tom", "bokra", "bukra", "bokrah", "بكرة", "بكره", "غدا", "غداً", "ghadan"}
_DAY_AFTER = {"day after tomorrow", "the day after tomorrow", "overmorrow", "ba3d bokra",
              "ba3d bukra", "baad bokra", "بعد بكرة", "بعد بكره", "بعد غد"}


def resolve_date(day_ref: str, *, now: datetime | None = None) -> date:
    """Turn a spoken day reference into a concrete Cairo-local date.

    Accepts an explicit ISO date ('2026-06-19'), today/tomorrow/day-after-tomorrow,
    a weekday name ('Saturday'/'السبت'/'sabt'), or 'next <weekday>'. Raises
    ValueError if it can't be understood (the caller surfaces that honestly rather
    than guessing). `now` is injectable for deterministic tests.
    """
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    today = now.date()
    s = (day_ref or "").strip()
    if not s:
        raise ValueError("empty day reference")

    # Explicit ISO date or datetime — already concrete, just take the date part.
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass

    low = s.lower()
    if low in _TODAY:
        return today
    if low in _TOMORROW:
        return today + timedelta(days=1)
    if low in _DAY_AFTER:
        return today + timedelta(days=2)

    nxt = False
    if low.startswith("next "):
        nxt = True
        low = low[5:].strip()

    wd = _WEEKDAYS.get(low)
    if wd is None:
        raise ValueError(f"could not understand day reference: {day_ref!r}")

    delta = (wd - today.weekday()) % 7          # 0 = today, 1..6 = this coming week
    if nxt:                                       # "next <weekday>" = the following week
        delta = delta + 7 if delta else 7
    return today + timedelta(days=delta)


def _parse_time(t: str) -> tuple[int, int]:
    """Parse a clock time into (hour, minute). Accepts 'HH', 'HH:MM', 'HH:MM:SS',
    and a trailing am/pm. Times are 24-hour Cairo local unless am/pm is given."""
    t = (t or "").strip().lower().replace(" ", "")
    ampm = None
    if t.endswith("am"):
        ampm, t = "am", t[:-2]
    elif t.endswith("pm"):
        ampm, t = "pm", t[:-2]
    parts = t.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return hour, minute


def _compose(d: date, time_str: str) -> str:
    """date + 'HH:MM' -> naive ISO local string, e.g. '2026-06-19T19:00:00'."""
    h, m = _parse_time(time_str)
    return f"{d.isoformat()}T{h:02d}:{m:02d}:00"


def get_events(days_ahead: int = 7):
    """Blocking — call via asyncio.to_thread from async code.

    Raises CalendarReadError on any failure so a degraded read is never silently
    returned as an empty list (which would invite a duplicate create)."""
    service = _get_service()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    try:
        resp = service.events().list(
            calendarId=settings.google_calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
    except Exception as e:  # HttpError, boundary/precondition, transport — all "not empty"
        raise CalendarReadError(str(e)) from e
    events = []
    for e in resp.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date"))  # timed or all-day
        # Include the event id so Abdo can reference an event when editing it.
        events.append({"id": e["id"], "summary": e.get("summary", "(no title)"), "start": start})
    return events


def get_event(event_id: str):
    """Fetch a single event by id. Returns the raw event dict, or None if it
    doesn't exist (404). Raises CalendarReadError on any other failure. Blocking."""
    service = _get_service()
    try:
        return service.events().get(
            calendarId=settings.google_calendar_id, eventId=event_id
        ).execute()
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 404:
            return None
        raise CalendarReadError(str(e)) from e
    except Exception as e:
        raise CalendarReadError(str(e)) from e


def create_event(summary: str, day: str, time: str, end_time: str | None = None,
                 now: datetime | None = None):
    """Create a timed event. `day` is a spoken day reference resolved to a Cairo
    date in Python; `time`/`end_time` are clock times (24h, HH:MM). The model never
    passes a computed date. end defaults to one hour after start. Blocking."""
    service = _get_service()
    tz = settings.timezone
    d = resolve_date(day, now=now)
    start = _compose(d, time)
    if end_time:
        end = _compose(d, end_time)
    else:
        end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
    body = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
    }
    ev = service.events().insert(calendarId=settings.google_calendar_id, body=body).execute()
    return {"id": ev["id"], "summary": ev.get("summary"), "start": ev["start"].get("dateTime")}


def update_event(event_id: str, summary: str | None = None, day: str | None = None,
                 time: str | None = None, end_time: str | None = None,
                 now: datetime | None = None):
    """Patch an existing event by id. The event must exist — otherwise EventNotFound
    is raised so the model can't fabricate an id or claim a phantom success.

    `day`/`time` are spoken/clock references; either may be omitted and is then
    filled from the existing event (move to another day at the same time, or change
    the time on the same day). When the start moves and no end_time is given, the
    original duration is preserved (a 5-hour event stays 5 hours). Blocking.
    """
    service = _get_service()
    tz = settings.timezone
    body: dict = {}
    if summary is not None:
        body["summary"] = summary

    if day is not None or time is not None or end_time is not None:
        existing = get_event(event_id)   # raises CalendarReadError on a degraded read
        if existing is None:
            raise EventNotFound(event_id)
        old_start_s = existing.get("start", {}).get("dateTime")
        old_end_s = existing.get("end", {}).get("dateTime")

        if day is not None or time is not None:
            base = datetime.fromisoformat(old_start_s) if old_start_s else datetime.now(ZoneInfo(tz))
            new_date = resolve_date(day, now=now) if day is not None else base.date()
            new_time = time if time is not None else base.strftime("%H:%M")
            new_start = _compose(new_date, new_time)
            body["start"] = {"dateTime": new_start, "timeZone": tz}
            if end_time is None and old_start_s and old_end_s:
                duration = datetime.fromisoformat(old_end_s) - datetime.fromisoformat(old_start_s)
                new_end = datetime.fromisoformat(new_start) + duration
                body["end"] = {"dateTime": new_end.isoformat(), "timeZone": tz}

        if end_time is not None:
            end_date = body.get("start", {}).get("dateTime")
            end_date = datetime.fromisoformat(end_date).date() if end_date \
                else (datetime.fromisoformat(old_start_s).date() if old_start_s
                      else datetime.now(ZoneInfo(tz)).date())
            body["end"] = {"dateTime": _compose(end_date, end_time), "timeZone": tz}
    elif summary is None:
        raise ValueError("update_event called with nothing to change")

    ev = service.events().patch(
        calendarId=settings.google_calendar_id, eventId=event_id, body=body
    ).execute()
    return {"id": ev["id"], "summary": ev.get("summary"),
            "start": ev["start"].get("dateTime"), "end": ev["end"].get("dateTime")}


def delete_event(event_id: str) -> None:
    """Delete an event by id. Verifies the event exists first (EventNotFound
    otherwise) so we never claim to have deleted a phantom. Blocking."""
    service = _get_service()
    existing = get_event(event_id)        # raises CalendarReadError on a degraded read
    if existing is None:
        raise EventNotFound(event_id)
    service.events().delete(
        calendarId=settings.google_calendar_id, eventId=event_id
    ).execute()
