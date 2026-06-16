import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from app.config import settings

# Named calendar_svc (not calendar) so it doesn't shadow Python's stdlib calendar.
# Built lazily on first use so the app still boots before the calendar is configured.
_service = None


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


def get_events(days_ahead: int = 7):
    """Blocking — call via asyncio.to_thread from async code."""
    service = _get_service()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    resp = service.events().list(
        calendarId=settings.google_calendar_id,
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(days=days_ahead)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()
    events = []
    for e in resp.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date"))  # timed or all-day
        # Include the event id so Abdo can reference an event when editing it.
        events.append({"id": e["id"], "summary": e.get("summary", "(no title)"), "start": start})
    return events


def create_event(summary: str, start: str, end: str | None = None):
    """Create a timed event. start/end are ISO 8601 strings in Cairo local time
    (e.g. '2026-06-19T19:00:00'); end defaults to one hour after start. Blocking."""
    service = _get_service()
    tz = settings.timezone
    if not end:
        end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
    body = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
    }
    ev = service.events().insert(calendarId=settings.google_calendar_id, body=body).execute()
    return {"id": ev["id"], "summary": ev.get("summary"), "start": ev["start"].get("dateTime")}


def update_event(event_id: str, summary: str | None = None,
                 start: str | None = None, end: str | None = None):
    """Patch an existing event by id. Only the provided fields change.

    Moving an event (new start, no end given) preserves its original duration —
    we read the existing event, measure its length, and shift the end to match,
    so a 5-hour event stays 5 hours instead of collapsing to a default. Blocking.
    """
    service = _get_service()
    tz = settings.timezone
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = {"dateTime": start, "timeZone": tz}
        if end is None:
            existing = service.events().get(
                calendarId=settings.google_calendar_id, eventId=event_id
            ).execute()
            old_start = existing.get("start", {}).get("dateTime")
            old_end = existing.get("end", {}).get("dateTime")
            if old_start and old_end:  # timed event — keep its length on the move
                duration = datetime.fromisoformat(old_end) - datetime.fromisoformat(old_start)
                new_end = datetime.fromisoformat(start) + duration
                body["end"] = {"dateTime": new_end.isoformat(), "timeZone": tz}
    if end is not None:
        body["end"] = {"dateTime": end, "timeZone": tz}
    ev = service.events().patch(
        calendarId=settings.google_calendar_id, eventId=event_id, body=body
    ).execute()
    return {"id": ev["id"], "summary": ev.get("summary"),
            "start": ev["start"].get("dateTime"), "end": ev["end"].get("dateTime")}


def delete_event(event_id: str) -> None:
    """Delete an event by id. Returns nothing (Google returns an empty 204). Blocking."""
    service = _get_service()
    service.events().delete(
        calendarId=settings.google_calendar_id, eventId=event_id
    ).execute()
