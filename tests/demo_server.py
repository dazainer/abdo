"""Run the real app against in-memory fakes, for UI work and screenshots.

No database, Claude, Telegram, or ElevenLabs calls: the fake DB from the smoke suite,
a canned Abdo, and login codes printed to this terminal instead of sent by Telegram.

    cd web && npm run build && cd .. && .venv/bin/python tests/demo_server.py
    # open http://localhost:8000 and log in as "Zain" or "Mo"
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smoke  # noqa: E402  (installs the fake db/embeddings/calendar on import)
from app import brain, db, telegram  # noqa: E402
from app.config import settings  # noqa: E402


async def _no_pool():
    return None


db.init_pool = db.close_pool = _no_pool   # never open a real database connection
settings.session_pepper = "demo-pepper"
settings.scheduler_enabled = False   # no background sends in demo mode
settings.cookie_secure = False      # plain http on localhost


async def print_code(chat_id, text):
    print(f"\n[telegram -> {chat_id}] {text}\n", flush=True)


telegram.send_message = print_code


class CannedAbdo:
    async def create(self, **kw):
        last = kw["messages"][-1]["content"]
        said = last.split(": ", 1)[-1] if isinstance(last, str) else "…"
        reply = f"تمام يا باشا — you said “{said}”. (Demo mode: no real Abdo here.)"
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=reply)])


brain.client = SimpleNamespace(messages=CannedAbdo())

# Some state so every screen has something to show.
now = datetime.now(timezone.utc)
for i, (role, mid, ch, text) in enumerate([
    ("user", 1, "telegram", "ezayak ya abdo, eh el akhbar?"),
    ("assistant", 1, "telegram", "الحمد لله يا زين! كله تمام. Need anything for the suite today?"),
    ("user", 1, "web", "Did anything arrive for me?"),
    ("assistant", 1, "web", "أيوه، الـ desk lamp بتاعتك وصلت — it's waiting at the front desk."),
]):
    smoke.fake.logged.append({"id": i + 1, "thread": "dm:1", "channel": ch, "role": role, "member_id": mid,
                              "content": text, "modality": "text", "speaker_confidence": None,
                              "created_at": now - timedelta(minutes=30 - i * 5)})
smoke.fake._msg_seq = 10
smoke.fake.shopping = [{"id": 1, "item": "rice", "qty": "2 bags", "bought": False},
                       {"id": 2, "item": "عيش", "qty": None, "bought": False},
                       {"id": 3, "item": "eggs", "qty": "a dozen", "bought": False}]
smoke.fake.packages = [
    {"id": 1, "description": "desk lamp", "carrier": "Amazon", "owner_id": 1,
     "expected_on": None, "status": "arrived"},
    {"id": 2, "description": "textbook", "carrier": "Canada Post", "owner_id": 2,
     "expected_on": (datetime.now(smoke.TZ) + timedelta(days=1)).date(), "status": "expected"},
]
smoke.fake._pkg_seq = 2
today = datetime.now(smoke.TZ).date()
smoke._fake_events[:] = [
    {"id": "e1", "summary": "Suite dinner", "start": f"{today.isoformat()}T19:00:00-04:00"},
    {"id": "e2", "summary": "Midterm (Mo)", "start": (today + timedelta(days=2)).isoformat()},
    {"id": "e3", "summary": "Groceries run", "start": f"{(today + timedelta(days=3)).isoformat()}T11:00:00-04:00"},
]

smoke.fake.statuses = {
    2: {"member_id": 2, "name": "Mo", "text": "in a midterm", "updated_at": now,
        "until_at": now + timedelta(hours=2)},
    3: {"member_id": 3, "name": "Sam", "text": "away till Sunday", "updated_at": now, "until_at": None,
        "place": "Montreal, Canada", "place_lat": 45.50884, "place_lng": -73.58781, "place_tz": "America/Toronto"},
}
smoke.fake.expenses = [
    {"id": 1, "description": "Walmart groceries", "amount_cents": 8640, "paid_by": 1, "created_by": 1,
     "spent_on": today - timedelta(days=2), "deleted": False, "shares": {1: 2880, 2: 2880, 3: 2880}},
    {"id": 2, "description": "Pizza night", "amount_cents": 3600, "paid_by": 2, "created_by": 2,
     "spent_on": today - timedelta(days=1), "deleted": False, "shares": {1: 1800, 2: 1800}},
    {"id": 3, "description": "Dish soap + sponges", "amount_cents": 1199, "paid_by": 3, "created_by": 3,
     "spent_on": today, "deleted": False, "shares": {1: 400, 2: 400, 3: 399}},
]
smoke.fake._exp_seq = 3
from datetime import time as _t
smoke.fake.classes = [
    {"member_id": 2, "name": "Mo", "course": "CS 246", "section": "002", "kind": "LAB", "weekday": today.weekday(),
     "starts": _t(18, 0), "ends": _t(19, 30), "room": "MC 3003", "term_start": today - timedelta(days=30),
     "term_end": today + timedelta(days=60)},
]
smoke.fake.exams = [
    {"id": 1, "member_id": 2, "name": "Mo", "course": "CS 246", "kind": "midterm", "room": "M3 1006",
     "starts_at": datetime.combine(today + timedelta(days=1), _t(19, 0), smoke.TZ), "ends_at": None,
     "created_by": 2, "deleted": False},
    {"id": 2, "member_id": 1, "name": "Zain", "course": "MATH 136", "kind": "quiz", "room": None,
     "starts_at": datetime.combine(today + timedelta(days=3), _t(10, 30), smoke.TZ), "ends_at": None,
     "created_by": 1, "deleted": False},
]
smoke.fake._exam_seq = 2
smoke.fake.pantry = [
    {"id": 1, "item": "milk", "owner_id": None, "expires_on": today + timedelta(days=1), "added_by": 1, "used": False},
    {"id": 2, "item": "Greek yogurt", "owner_id": 1, "expires_on": today, "added_by": 1, "used": False},
    {"id": 3, "item": "leftover koshary", "owner_id": None, "expires_on": today - timedelta(days=1), "added_by": 2, "used": False},
    {"id": 4, "item": "rice", "owner_id": None, "expires_on": None, "added_by": 3, "used": False},
]
smoke.fake._pan_seq = 4
smoke.fake.polls = [{"id": 1, "question": "Where are we eating Friday?", "options": ["Shawarma", "Pizza", "Sushi"],
                     "created_by": 2, "closed": False, "closed_at": None, "created_at": now}]
smoke.fake.votes = [{"poll_id": 1, "member_id": 2, "option_index": 0}, {"poll_id": 1, "member_id": 3, "option_index": 1}]
smoke.fake._poll_seq = 1
smoke.fake.reminders = [
    {"id": 1, "member_id": 1, "text": "switch the laundry", "due_at": now + timedelta(minutes=35),
     "created_by": 1, "done": False},
    {"id": 2, "member_id": 1, "text": "bring the HDMI cable", "due_at": now + timedelta(days=1, hours=2),
     "created_by": 2, "done": False},
]
smoke.fake._rem_seq = 2

# Buses: made-up trips, with departures generated around the current time (no GRT calls).
from app import transit  # noqa: E402
smoke.fake.bus = smoke.BUS_TRIPS


async def _demo_departures(stops):
    base = transit.now().replace(second=0, microsecond=0)
    rows = []
    for k, stop in enumerate(stops):
        for i in range(4):
            at = base + timedelta(minutes=4 + 11 * k + 15 * i)
            rows.append({"stop": {"id": stop, "name": f"Stop {stop}"}, "route": {"shortName": "7" if k == 0 else "301"},
                         "trip": {"headsign": "Downtown", "stops": [
                             {"stop": {"id": stop, "name": f"Stop {stop}"}, "departure": at.isoformat()},
                             *({"stop": {"id": s, "name": f"Stop {s}"}, "departure": (at + timedelta(minutes=9)).isoformat()}
                               for s in ("205", "100", "399"))]},
                         "type": "SCHEDULED", "departure": at.isoformat(), "departureDeltaFromScheduledSeconds": 0})
    return rows
transit._post = _demo_departures

if __name__ == "__main__":
    import uvicorn
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
