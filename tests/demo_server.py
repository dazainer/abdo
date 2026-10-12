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

if __name__ == "__main__":
    import uvicorn
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
