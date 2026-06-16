# Abdo — Phase 3 Build Spec: Calendar + Location

Two capabilities that make Abdo aware of the family's week and whereabouts: read-only access to a shared family Google Calendar, and "who's home / where is X" via Telegram live location. Both are still pure Telegram — no app, no wall display.

Prerequisite: Phase 2 done, and the family Google Calendar created (you mentioned you'd make a shared one).

---

## Part A — Shared Google Calendar (read-only)

### A1. Definition of done
Abdo answers "what's on this week", "any plans Friday", "what are we doing tomorrow" from the shared family calendar.

### A2. Google setup (your hands — I can't do this)
1. Create (or reuse) a project in Google Cloud Console.
2. Enable the **Google Calendar API**.
3. Create a **service account**; download its JSON key.
4. In Google Calendar, open the **family calendar's settings → Share with specific people**, and add the service account's email (the `client_email` from the JSON) with **"See all event details."**
5. From the same settings page (**Integrate calendar**), copy the **Calendar ID**.

A service account is the right call here: it's a single shared calendar, so you avoid per-user OAuth and token-refresh entirely. Direct calendar sharing to the SA email works on a normal Google account — no Workspace domain-wide delegation needed.

### A3. Config additions (`app/config.py`)
```python
    google_service_account_json: str | None = None   # the JSON key, as a string
    google_calendar_id: str | None = None
```

### A4. Dependencies
```
google-api-python-client
google-auth
```

### A5. Calendar module (`app/calendar_svc.py`, new)
> Name it `calendar_svc` (not `calendar`) so it doesn't shadow Python's stdlib `calendar`.

```python
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from app.config import settings

_creds = service_account.Credentials.from_service_account_info(
    json.loads(settings.google_service_account_json),
    scopes=["https://www.googleapis.com/auth/calendar.readonly"],
)
_service = build("calendar", "v3", credentials=_creds, cache_discovery=False)


def get_events(days_ahead: int = 7):
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    resp = _service.events().list(
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
        events.append({"summary": e.get("summary", "(no title)"), "start": start})
    return events
```

### A6. Tool (add to `app/tools.py`)
```python
{
    "name": "get_calendar",
    "description": (
        "Look up upcoming family events from the shared calendar. Use for questions like "
        "what's on this week, what are we doing Friday, any plans tomorrow."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"days_ahead": {"type": "integer", "description": "Days ahead to look (default 7)."}},
        "required": [],
    },
},
```

Dispatch — `googleapiclient` is **blocking**, so push it off the event loop:
```python
import asyncio
from app import calendar_svc

if name == "get_calendar":
    events = await asyncio.to_thread(calendar_svc.get_events, tool_input.get("days_ahead", 7))
    if not events:
        return "No events on the shared calendar in that window."
    return "\n".join(f"- {e['start']}: {e['summary']}" for e in events)
```

---

## Part B — Telegram live location ("who's home")

### B1. Definition of done
A family member shares their **live location to the bot**; Abdo records the latest position silently. Then "where is X", "is X on the way", "who's home" report **home** or distance-from-home, with how recently it updated.

Why not Google Maps: there's no official API for Maps location sharing, and the scraping workarounds are fragile and against ToS. Live location lives entirely inside Telegram, is opt-in per person, and you already have the webhook.

### B2. Config additions (`app/config.py`)
```python
    home_lat: float | None = None
    home_lng: float | None = None
```
Get these by dropping a pin on your building in Google Maps and copying the coordinates.

### B3. Extend the update parser (modify `app/telegram.py`)
Phase 1 ignored everything non-text. Make `parse_update` return a typed payload and also read `edited_message` (live-location updates arrive as edits):

```python
def parse_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    base = {"chat_id": msg["chat"]["id"], "from_user": msg["from"]}
    if "text" in msg:
        return {**base, "kind": "text", "text": msg["text"]}
    if "location" in msg:
        loc = msg["location"]
        return {**base, "kind": "location", "lat": loc["latitude"], "lng": loc["longitude"]}
    return None
```

### B4. Location storage (add to `app/db.py`)
```python
async def upsert_location(member_id, lat, lng) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO member_locations (member_id, lat, lng, updated_at) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (member_id) DO UPDATE SET lat = $2, lng = $3, updated_at = now()",
            member_id, lat, lng,
        )

async def get_location(name):
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT m.name, l.lat, l.lng, l.updated_at "
            "FROM member_locations l JOIN family_members m ON m.id = l.member_id "
            "WHERE m.name ILIKE $1", name,
        )

async def get_all_locations():
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT m.name, l.lat, l.lng, l.updated_at "
            "FROM member_locations l JOIN family_members m ON m.id = l.member_id"
        )
```

### B5. Home geofence (`app/geo.py`, new)
```python
from math import radians, sin, cos, asin, sqrt
from app.config import settings

def km_from_home(lat: float, lng: float) -> float:
    dlat = radians(lat - settings.home_lat)
    dlng = radians(lng - settings.home_lng)
    a = (sin(dlat / 2) ** 2
         + cos(radians(settings.home_lat)) * cos(radians(lat)) * sin(dlng / 2) ** 2)
    return 2 * 6371 * asin(sqrt(a))   # kilometers

def describe(lat: float, lng: float) -> str:
    d = km_from_home(lat, lng)
    return "home" if d < 0.15 else f"{d:.1f} km from home"
```
Tune the 0.15 km (~150 m) radius to your compound — New Cairo plots are large.

### B6. Tool (add to `app/tools.py`)
```python
{
    "name": "where_is",
    "description": (
        "Find where a family member currently is, from their shared live location. "
        "Use for who's home, where is X, is X on the way. Pass 'everyone' for all."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "A family member's name, or 'everyone'."}},
        "required": ["name"],
    },
},
```

Dispatch:
```python
from app import geo

if name == "where_is":
    who = tool_input["name"]
    if who.lower() in ("everyone", "all"):
        rows = await db.get_all_locations()
        if not rows:
            return "No one is sharing their location right now."
        return "\n".join(
            f"{r['name']}: {geo.describe(r['lat'], r['lng'])} (updated {r['updated_at']:%H:%M})"
            for r in rows
        )
    row = await db.get_location(who)
    if not row:
        return f"{who} isn't sharing a location right now."
    return f"{row['name']}: {geo.describe(row['lat'], row['lng'])} (updated {row['updated_at']:%H:%M})"
```

### B7. Route location updates (modify `app/main.py`)
Record location pings **silently** — live location edits arrive frequently, and replying to each would spam the chat. Branch before the text flow:

```python
parsed = telegram.parse_update(update)
if not parsed:
    return {"ok": True}

member = await db.get_member_by_telegram_id(parsed["from_user"]["id"])
if not member:
    await telegram.send_message(parsed["chat_id"], "أنا عبده 👋 بس لسه مش عارفك. كلّم Zain يضيفك للعيلة.")
    return {"ok": True}

if parsed["kind"] == "location":
    await db.upsert_location(member["id"], parsed["lat"], parsed["lng"])
    return {"ok": True}   # silent — just record the latest position

# kind == "text" → existing Phase 1 flow (typing, log, brain.think, send, log)
```

### B8. Persona update (`app/prompts.py`)
```
- You can check the family's shared calendar for upcoming events.
- You can see where family members are when they've shared live location — report "home" or distance from home, and how recent it is. This is opt-in and a bit sensitive; answer the question plainly, don't be creepy or volunteer people's whereabouts unprompted.
```

---

## Gotchas (both parts)

- **Never reply per location update.** Edits stream in every few seconds; record and return `200`. Only the `where_is` tool talks.
- **Webhook update types.** Live-location edits come as `edited_message`. Telegram's default `allowed_updates` already includes it, so as long as you didn't restrict `allowed_updates` on `setWebhook`, nothing extra is needed. If you ever set it explicitly, include `"message"` and `"edited_message"`.
- **`googleapiclient` blocks** — always call it via `asyncio.to_thread`, or it stalls the event loop (and risks webhook-timeout retries).
- **Module name** — `calendar_svc`, not `calendar`, to avoid shadowing the stdlib.
- **Staleness & consent.** Live sharing stops when the person ends it in Telegram, leaving stale coordinates — that's why every answer shows the `updated HH:MM` timestamp. Make clear to the family that this is opt-in.
- **Timezone** — keep `Africa/Cairo` for both event display and `updated_at` formatting.

## Build checklist

Calendar:
- [ ] GCP project + Calendar API enabled + service account JSON
- [ ] Share the family calendar with the SA email; copy the Calendar ID
- [ ] Env: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_CALENDAR_ID`
- [ ] `calendar_svc.py` + `get_calendar` tool (via `asyncio.to_thread`)
- [ ] Deps: `google-api-python-client`, `google-auth`
- [ ] Test: add an event → ask Abdo

Location:
- [ ] Env: `HOME_LAT`, `HOME_LNG` (from Google Maps)
- [ ] `parse_update` returns typed payload + reads `edited_message`
- [ ] `main.py` routes `location` → silent `upsert_location`
- [ ] `db.upsert_location` / `get_location` / `get_all_locations`
- [ ] `geo.py` geofence
- [ ] `where_is` tool + dispatch
- [ ] Persona update
- [ ] Test: share live location to the bot → "where am I" / "who's home"

## CLAUDE.md update

- Flip **current phase** to Phase 3.
- Env additions: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_CALENDAR_ID`, `HOME_LAT`, `HOME_LNG`.
- Note under the architecture/flow: "The webhook now handles **text and location** updates; location pings are recorded silently (no reply)."
- Gotcha: "Calendar access is read-only via a service account the family calendar is shared with; `googleapiclient` calls must be wrapped in `asyncio.to_thread`."
