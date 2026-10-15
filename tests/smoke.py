"""Offline smoke test for Abdo.

Exercises the real turn entry point, brain tool-loop, tool dispatch, prompt
structure, Telegram channel routing, and update parsing — with the network
(Anthropic, Telegram, Cohere, Google, ElevenLabs) and Postgres faked out.
No real credentials, DB, or HTTP calls. Run:  .venv/bin/python tests/smoke.py

Test data is deliberately fake (the repo is public): no real passwords, full
names, ids, or coordinates.
"""
import asyncio
import hashlib
import re
import os
import sys
from types import SimpleNamespace

# --- Fake env so config.Settings() loads without a real .env -----------------
# Settings reads ".env" from the working directory. Run from tests/ (which has none)
# so a developer's real keys, push config, and calendar never leak into the tests.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
os.environ.setdefault("COHERE_API_KEY", "co-fake")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://fake/fake")
os.environ["TIMEZONE"] = "America/Toronto"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import brain, db, tools, telegram, embeddings, turn, prompts  # noqa: E402

ZAIN = {"id": 1, "name": "Zain", "arabic_name": "زين", "telegram_id": 111}
MO = {"id": 2, "name": "Mo", "arabic_name": "مو", "telegram_id": 222}
SAM = {"id": 3, "name": "Sam", "arabic_name": "سام", "telegram_id": None}   # no Telegram yet
MEMBERS = {1: ZAIN, 2: MO, 3: SAM}
NAMES = {1: "Zain", 2: "Mo", 3: "Sam"}


# --- In-memory fake of the db layer ------------------------------------------
class FakeDB:
    def __init__(self):
        self.logged = []             # every log_message row (dicts)
        self.facts = []
        self.shopping = []
        self.packages = []
        self.locations = {}          # member_id -> (lat, lng), written by upsert
        self.loc_rows = {}           # name.lower() -> asyncpg-style row dict, for reads
        self.members_by_tg = {111: ZAIN, 222: MO}
        self.codes = []
        self.sessions = {}
        self.push_subs = {}
        self.reminders = []
        self.sent_log = set()
        self.statuses = {}
        self.expenses = []
        self.settlements = []
        self.classes = []
        self.exams = []
        self.pantry = []
        self.bus = []                # saved bus trips (dicts shaped like the bus_trips rows)
        self.breaks = []             # class breaks
        self.polls = []
        self.votes = []

    # members
    async def get_member_by_telegram_id(self, tg_id):
        return self.members_by_tg.get(tg_id)

    async def roster_string(self):
        return "Zain (زين), Mo (مو)"

    async def get_member_id_by_name(self, name):
        return {"zain": 1, "mo": 2, "sam": 3}.get((name or "").strip().lower())

    async def get_member(self, member_id):
        return MEMBERS.get(member_id)

    # conversation log
    async def log_message(self, **row):
        self._msg_seq = getattr(self, "_msg_seq", 0) + 1
        self.logged.append({"id": self._msg_seq, "created_at": datetime.now(timezone.utc), **row})

    async def thread_page(self, thread, before_id=None, limit=30):
        rows = [r for r in self.logged if r["thread"] == thread
                and (before_id is None or r["id"] < before_id)]
        rows = sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]
        return [{**r, "speaker": NAMES.get(r["member_id"])} for r in rows]

    # web login + sessions
    async def create_login_code(self, member_id, code_hash, expires_at):
        self._code_seq = getattr(self, "_code_seq", 0) + 1
        self.codes.append({"id": self._code_seq, "member_id": member_id, "code_hash": code_hash,
                           "expires_at": expires_at, "attempts": 0, "used": False,
                           "created_at": datetime.now(timezone.utc)})

    async def count_recent_login_codes(self, member_id, since):
        return sum(1 for c in self.codes if c["member_id"] == member_id and c["created_at"] >= since)

    async def latest_live_login_code(self, member_id):
        now = datetime.now(timezone.utc)
        live = [c for c in self.codes if c["member_id"] == member_id and not c["used"]
                and c["expires_at"] > now]
        return live[-1] if live else None

    async def bump_login_code_attempts(self, code_id):
        for c in self.codes:
            if c["id"] == code_id:
                c["attempts"] += 1

    async def use_login_code(self, code_id):
        for c in self.codes:
            if c["id"] == code_id and not c["used"]:
                c["used"] = True
                return True
        return False

    async def create_session(self, token_hash, member_id, user_agent, expires_at):
        self.sessions[token_hash] = {"member_id": member_id, "expires_at": expires_at,
                                     "last_seen": datetime.now(timezone.utc)}

    async def session_member(self, token_hash):
        s = self.sessions.get(token_hash)
        if not s or s["expires_at"] <= datetime.now(timezone.utc):
            return None
        return {**MEMBERS[s["member_id"]], "session_last_seen": s["last_seen"]}

    async def touch_session(self, token_hash, expires_at):
        self.sessions[token_hash].update(expires_at=expires_at, last_seen=datetime.now(timezone.utc))

    async def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)

    # members + prefs
    async def all_members(self):
        return [MEMBERS[i] for i in sorted(MEMBERS)]

    async def set_member_prefs(self, member_id, updates):
        MEMBERS[member_id]["prefs"] = {**(MEMBERS[member_id].get("prefs") or {}), **updates}
        return dict(MEMBERS[member_id]["prefs"])

    # reminders
    async def add_reminder(self, member_id, text, due_at, created_by, urgent=False):
        self._rem_seq = getattr(self, "_rem_seq", 0) + 1
        self.reminders.append({"id": self._rem_seq, "member_id": member_id, "text": text,
                               "due_at": due_at, "created_by": created_by, "done": False,
                               "urgent": urgent, "sends": 0, "acked_at": None})
        return self._rem_seq

    def _rem_row(self, r):
        return {**r, "member_name": NAMES.get(r["member_id"]),
                "created_by_name": NAMES.get(r["created_by"])}

    async def upcoming_reminders(self, member_id=None):
        rows = [r for r in self.reminders if not r["done"]
                and (member_id is None or r["member_id"] == member_id)]
        return [self._rem_row(r) for r in sorted(rows, key=lambda r: r["due_at"])]

    async def cancel_reminder(self, reminder_id, member_id=None):
        for r in self.reminders:
            if r["id"] == reminder_id and not r["done"] and \
                    (member_id is None or member_id in (r["member_id"], r["created_by"])):
                r["done"] = True
                return r["text"]
        return None

    async def due_reminder_ids(self, now):
        return [r["id"] for r in sorted(self.reminders, key=lambda r: r["due_at"])
                if not r["done"] and r["due_at"] <= now]

    async def claim_reminder(self, reminder_id, now, max_sends=3, every=None):
        for r in self.reminders:
            if r["id"] == reminder_id and not r["done"] and r["due_at"] <= now:
                first_due = r.setdefault("first_due", r["due_at"])
                r["sends"] = r.get("sends", 0) + 1
                r["done"] = not r.get("urgent") or r["sends"] >= max_sends
                if not r["done"]:
                    r["due_at"] = now + every
                return {**self._rem_row(r), "due_at": first_due}
        return None

    async def ack_reminders(self, member_id, reminder_id=None):
        out = []
        for r in self.reminders:
            if r["member_id"] == member_id and not r["done"] and r.get("urgent") and r.get("sends", 0) > 0 \
                    and reminder_id in (None, r["id"]):
                r["done"], r["acked_at"] = True, datetime.now(timezone.utc)
                out.append(r["id"])
        return out

    async def claim_daily(self, member_id, kind, local_date):
        key = (member_id, kind, local_date)
        if key in self.sent_log:
            return False
        self.sent_log.add(key)
        return True

    # status
    async def set_status(self, member_id, text, until_at, place=None):
        p = place or {}
        self.statuses[member_id] = {"member_id": member_id, "name": NAMES[member_id], "text": text,
                                    "until_at": until_at, "updated_at": datetime.now(timezone.utc),
                                    "place": p.get("label"), "place_lat": p.get("lat"),
                                    "place_lng": p.get("lng"), "place_tz": p.get("tz")}

    # fridge
    async def add_pantry_item(self, item, owner_id, expires_on, added_by):
        self._pan_seq = getattr(self, "_pan_seq", 0) + 1
        self.pantry.append({"id": self._pan_seq, "item": item, "owner_id": owner_id, "expires_on": expires_on,
                            "added_by": added_by, "used": False})
        return self._pan_seq

    async def open_pantry(self):
        rows = [{**r, "owner_name": NAMES.get(r["owner_id"])} for r in self.pantry if not r["used"]]
        return sorted(rows, key=lambda r: (r["expires_on"] is None, r["expires_on"] or date.max))

    async def use_pantry_item(self, item_id):
        for r in self.pantry:
            if r["id"] == item_id and not r["used"]:
                r["used"] = True
                return r["item"]
        return None

    # polls
    async def create_poll(self, question, options, created_by):
        self._poll_seq = getattr(self, "_poll_seq", 0) + 1
        self.polls.append({"id": self._poll_seq, "question": question, "options": list(options),
                           "created_by": created_by, "closed": False, "closed_at": None,
                           "created_at": datetime.now(timezone.utc)})
        return self._poll_seq

    async def polls_recent(self, closed_within_hours=24):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=closed_within_hours)
        rows = [{**p, "created_by_name": NAMES.get(p["created_by"])} for p in self.polls
                if not p.get("deleted") and (not p["closed"] or (p["closed_at"] or cutoff) > cutoff)]
        return sorted(rows, key=lambda p: (p["closed"], -p["id"]))

    async def get_poll(self, poll_id):
        return next((p for p in self.polls if p["id"] == poll_id and not p.get("deleted")), None)

    async def remove_vote(self, poll_id, member_id):
        poll = await self.get_poll(poll_id)
        before = len(self.votes)
        if poll and not poll["closed"]:
            self.votes = [v for v in self.votes if not (v["poll_id"] == poll_id and v["member_id"] == member_id)]
        return len(self.votes) < before

    async def delete_poll(self, poll_id, member_id):
        for p in self.polls:
            if p["id"] == poll_id and not p.get("deleted") and p["created_by"] == member_id:
                p["deleted"] = True
                return True
        return False

    async def poll_votes(self, poll_ids):
        return [{**v, "name": NAMES[v["member_id"]]} for v in self.votes if v["poll_id"] in poll_ids]

    async def cast_vote(self, poll_id, member_id, option_index):
        self.votes = [v for v in self.votes if not (v["poll_id"] == poll_id and v["member_id"] == member_id)]
        self.votes.append({"poll_id": poll_id, "member_id": member_id, "option_index": option_index})

    async def close_poll(self, poll_id, member_id):
        for p in self.polls:
            if p["id"] == poll_id and not p["closed"] and p["created_by"] == member_id:
                p["closed"] = True
                p["closed_at"] = datetime.now(timezone.utc)
                return True
        return False

    # classes + exams
    async def replace_classes(self, member_id, rows):
        self.classes = [c for c in self.classes if c["member_id"] != member_id]
        self.classes += [{**r, "member_id": member_id, "name": NAMES[member_id]} for r in rows]
        return len(rows)

    async def classes_for(self, member_id=None):
        return sorted([c for c in self.classes if member_id is None or c["member_id"] == member_id],
                      key=lambda c: (c["member_id"], c["weekday"], c["starts"]))

    async def add_class_break(self, member_id, starts_on, ends_on, label):
        self._brk_seq = getattr(self, "_brk_seq", 0) + 1
        self.breaks.append({"id": self._brk_seq, "member_id": member_id, "starts_on": starts_on,
                            "ends_on": ends_on, "label": label})
        return self._brk_seq

    async def class_breaks(self, member_id=None, since=None):
        return [b for b in self.breaks if (member_id is None or b["member_id"] == member_id)
                and (since is None or b["ends_on"] >= since)]

    async def delete_class_break(self, break_id, member_id):
        before = len(self.breaks)
        self.breaks = [b for b in self.breaks if not (b["id"] == break_id and b["member_id"] == member_id)]
        return len(self.breaks) < before

    async def add_exam(self, member_id, course, kind, starts_at, ends_at, room, created_by):
        self._exam_seq = getattr(self, "_exam_seq", 0) + 1
        self.exams.append({"id": self._exam_seq, "member_id": member_id, "name": NAMES[member_id],
                           "course": course, "kind": kind, "starts_at": starts_at, "ends_at": ends_at,
                           "room": room, "created_by": created_by, "deleted": False})
        return self._exam_seq

    async def exams_between(self, start, end, member_id=None):
        return sorted([e for e in self.exams if not e["deleted"] and start <= e["starts_at"] < end
                       and (member_id is None or e["member_id"] == member_id)], key=lambda e: e["starts_at"])

    async def delete_exam(self, exam_id, member_id):
        for e in self.exams:
            if e["id"] == exam_id and not e["deleted"] and member_id in (e["member_id"], e["created_by"]):
                e["deleted"] = True
                return e["course"]
        return None

    # money
    async def add_expense(self, description, amount_cents, paid_by, created_by, spent_on, shares):
        self._exp_seq = getattr(self, "_exp_seq", 0) + 1
        self.expenses.append({"id": self._exp_seq, "description": description, "amount_cents": amount_cents,
                              "paid_by": paid_by, "created_by": created_by, "spent_on": spent_on,
                              "deleted": False, "shares": dict(shares)})
        return self._exp_seq

    async def add_settlement(self, from_member, to_member, amount_cents, created_by, settled_on):
        self._set_seq = getattr(self, "_set_seq", 0) + 1
        self.settlements.append({"id": self._set_seq, "from_member": from_member, "to_member": to_member,
                                 "amount_cents": amount_cents, "created_by": created_by,
                                 "settled_on": settled_on, "deleted": False})
        return self._set_seq

    async def money_ledger(self):
        live = [e for e in self.expenses if not e["deleted"]]
        return ([{"id": e["id"], "paid_by": e["paid_by"], "amount_cents": e["amount_cents"]} for e in live],
                [{"member_id": m, "share_cents": c} for e in live for m, c in e["shares"].items()],
                [t for t in self.settlements if not t["deleted"]])

    async def recent_expenses(self, limit=20):
        live = [e for e in reversed(self.expenses) if not e["deleted"]][:limit]
        return [{**{k: v for k, v in e.items() if k != "shares"}, "paid_by_name": NAMES[e["paid_by"]],
                 "shares": [{"member_id": m, "name": NAMES[m], "share_cents": c} for m, c in sorted(e["shares"].items())]}
                for e in live]

    async def recent_settlements(self, limit=20):
        return [{**t, "from_name": NAMES[t["from_member"]], "to_name": NAMES[t["to_member"]]}
                for t in reversed(self.settlements) if not t["deleted"]][:limit]

    async def delete_expense(self, expense_id, member_id):
        for e in self.expenses:
            if e["id"] == expense_id and not e["deleted"] and member_id in (e["paid_by"], e["created_by"]):
                e["deleted"] = True
                return e["description"]
        return None

    async def delete_settlement(self, settlement_id, member_id):
        for t in self.settlements:
            if t["id"] == settlement_id and not t["deleted"] and \
                    member_id in (t["from_member"], t["to_member"], t["created_by"]):
                t["deleted"] = True
                return True
        return False

    async def clear_status(self, member_id):
        return self.statuses.pop(member_id, None) is not None

    async def current_statuses(self):
        now = datetime.now(timezone.utc)
        return [s for _, s in sorted(self.statuses.items())
                if s["until_at"] is None or s["until_at"] > now]

    # push
    async def save_push_subscription(self, member_id, endpoint, p256dh, auth):
        self.push_subs[endpoint] = {"member_id": member_id, "endpoint": endpoint,
                                    "p256dh": p256dh, "auth": auth}

    async def delete_push_subscription(self, endpoint):
        self.push_subs.pop(endpoint, None)

    async def push_subscriptions_for(self, member_id):
        return [s for s in self.push_subs.values() if s["member_id"] == member_id]

    async def recent_messages(self, thread, limit=10):
        rows = [r for r in self.logged if r["thread"] == thread][-limit:]
        return [{"role": r["role"], "content": r["content"], "created_at": r.get("created_at"),
                 "speaker": NAMES.get(r["member_id"])} for r in rows]

    # facts
    async def add_fact(self, category, content, embedding, created_by):
        self._fact_seq = getattr(self, "_fact_seq", 0) + 1
        self.facts.append({"id": self._fact_seq, "category": category,
                           "content": content, "embedding": embedding})

    async def nearest_fact(self, embedding):
        # Fake embeddings are deterministic per text, so identical content yields an
        # identical vector — a faithful stand-in for pgvector's near-zero distance.
        for f in reversed(self.facts):
            if f["embedding"] == embedding:
                return {"id": f["id"], "content": f["content"], "distance": 0.0}
        if self.facts:
            last = self.facts[-1]
            return {"id": last["id"], "content": last["content"], "distance": 1.0}
        return None

    async def update_fact(self, fact_id, category, content, embedding):
        for f in self.facts:
            if f["id"] == fact_id:
                f.update(category=category, content=content, embedding=embedding)

    async def search_facts(self, embedding, k=4):
        # Mirror the real row shape: id + category + content + distance.
        return [{"id": f["id"], "category": f["category"], "content": f["content"],
                 "distance": 0.0} for f in reversed(self.facts)][:k]

    # location
    async def upsert_location(self, member_id, lat, lng):
        self.locations[member_id] = (lat, lng)

    async def get_location(self, name):
        return self.loc_rows.get(name.lower())

    async def get_all_locations(self):
        return list(self.loc_rows.values())

    # shopping
    async def add_shopping_item(self, item, qty, member_id):
        if any(s["item"].lower() == item.lower() and not s["bought"] for s in self.shopping):
            return False
        self.shopping.append({"id": len(self.shopping) + 1, "item": item, "qty": qty, "bought": False})
        return True

    async def get_shopping_list(self):
        return [s for s in self.shopping if not s["bought"]]

    async def mark_item_bought(self, item, member_id):
        for s in self.shopping:
            if s["item"].lower() == item.lower() and not s["bought"]:
                s["bought"] = True
                return s["item"]
        return None

    async def clear_shopping_list(self, member_id):
        n = sum(1 for s in self.shopping if not s["bought"])
        for s in self.shopping:
            s["bought"] = True
        return n

    # packages
    async def add_package(self, description, carrier, owner_id, expected_on):
        self._pkg_seq = getattr(self, "_pkg_seq", 0) + 1
        self.packages.append({"id": self._pkg_seq, "description": description,
                              "carrier": carrier, "owner_id": owner_id,
                              "expected_on": expected_on, "status": "expected"})
        return self._pkg_seq

    async def get_open_packages(self, owner_id=None):
        return [{**p, "owner_name": NAMES.get(p["owner_id"])} for p in self.packages
                if p["status"] in ("expected", "arrived")
                and (owner_id is None or p["owner_id"] == owner_id)]

    async def bus_trips(self):
        return list(self.bus)

    async def update_package_status(self, package_id, status):
        for p in self.packages:
            if p["id"] == package_id and p["status"] in ("expected", "arrived"):
                p["status"] = status
                return p["description"]
        return None


fake = FakeDB()
tools.CALENDAR_CACHE_SECONDS = 0     # every calendar scenario changes the fake events; test the cache on its own
for _name in ("get_member_by_telegram_id", "roster_string", "get_member_id_by_name",
              "get_member", "thread_page", "create_login_code", "count_recent_login_codes",
              "latest_live_login_code", "bump_login_code_attempts", "use_login_code",
              "create_session", "session_member", "touch_session", "delete_session",
              "save_push_subscription", "delete_push_subscription", "push_subscriptions_for",
              "all_members", "set_member_prefs", "add_reminder", "upcoming_reminders",
              "cancel_reminder", "due_reminder_ids", "claim_reminder", "ack_reminders", "claim_daily",
              "set_status", "clear_status", "current_statuses",
              "add_expense", "add_settlement", "money_ledger", "recent_expenses",
              "recent_settlements", "delete_expense", "delete_settlement",
              "replace_classes", "classes_for", "add_exam", "exams_between", "delete_exam",
              "add_pantry_item", "open_pantry", "use_pantry_item", "create_poll", "polls_recent",
              "get_poll", "poll_votes", "cast_vote", "close_poll", "remove_vote", "delete_poll",
              "log_message", "recent_messages", "add_fact", "nearest_fact", "update_fact",
              "search_facts", "upsert_location", "get_location", "get_all_locations",
              "add_shopping_item", "get_shopping_list", "mark_item_bought",
              "clear_shopping_list", "add_package", "get_open_packages",
              "update_package_status", "bus_trips", "add_class_break", "class_breaks", "delete_class_break"):
    setattr(db, _name, getattr(fake, _name))


# --- Fake embeddings: record input_type so we can assert store/recall asymmetry --
embed_calls = []


async def fake_embed(text, *, input_type):
    embed_calls.append(input_type)
    # Deterministic per text and non-zero, so embeddings.is_valid() passes and
    # identical content produces an identical vector (lets the dedup path fire).
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(h[i % len(h)] / 255.0) + 0.001 for i in range(embeddings.EMBED_DIM)]


embeddings.embed = fake_embed


# --- Fake calendar + a fake home coordinate for the geofence ------------------
from datetime import datetime, timedelta, timezone, date  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from app import calendar_svc  # noqa: E402
from app.config import settings  # noqa: E402

settings.home_lat = 43.0000      # fake pin; lets geo.describe compute distance
settings.home_lng = -80.0000
TZ = ZoneInfo(settings.timezone)

_fake_events = []
_created_events = []
_deleted_events = []
calendar_svc.is_configured = lambda: True
calendar_svc.get_events = lambda days_ahead=7: list(_fake_events)


def fake_create_event(summary, day, time, end_time=None):
    # Mirror the real signature: day+time, code resolves the date.
    d = calendar_svc.resolve_date(day)
    start = calendar_svc._compose(d, time)
    ev = {"id": "evt_new", "summary": summary, "start": start}
    _created_events.append(ev)
    return ev


def fake_update_event(event_id, summary=None, day=None, time=None, end_time=None):
    return {"id": event_id, "summary": summary or "Suite dinner",
            "start": "2026-10-16T19:00:00", "end": "2026-10-16T20:00:00"}


def fake_delete_event(event_id):
    _deleted_events.append(event_id)


_real_create_event = calendar_svc.create_event   # the dedupe test uses the real one
calendar_svc.create_event = fake_create_event
calendar_svc.update_event = fake_update_event
calendar_svc.delete_event = fake_delete_event


# --- Scripted Anthropic client ------------------------------------------------
def _tool_use_block(tool_name, tool_id, tool_input=None):
    return SimpleNamespace(type="tool_use", name=tool_name, input=tool_input or {}, id=tool_id)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _thinking_block():
    # Haiku 5.5 returns thinking blocks with empty text by default (display omitted).
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def _resp(stop_reason, content, **extra):
    return SimpleNamespace(stop_reason=stop_reason, content=content, **extra)


class FakeMessages:
    def __init__(self, script):
        self._script = script        # list of responses to return in order
        self._i = 0
        self.requests = []           # full kwargs of every create() call

    async def create(self, **kw):
        # Snapshot the message list: the brain keeps appending to the same list.
        self.requests.append({**kw, "messages": list(kw["messages"])})
        resp = self._script[self._i]
        self._i += 1
        return resp

    @property
    def calls(self):
        return [(r["model"], [m["role"] for m in r["messages"]]) for r in self.requests]


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)


def script_for(tool_name, final_text, tool_input=None):
    """First call asks for the tool; second call (after result) returns text."""
    return [
        _resp("tool_use", [_tool_use_block(tool_name, "toolu_1", tool_input)]),
        _resp("end_turn", [_text_block(final_text)]),
    ]


def reply_only(text):
    return [_resp("end_turn", [_text_block(text)])]


def sys_text(system) -> str:
    return "".join(b["text"] for b in system)


# --- Assertions --------------------------------------------------------------
passed = 0
failed = 0


def check(label, cond):
    global passed, failed
    mark = "PASS" if cond else "FAIL"
    if cond:
        passed += 1
    else:
        failed += 1
    print(f"  [{mark}] {label}")


# =============================================================================
# Brain: model, request shape, caching
# =============================================================================

async def test_model_and_request_shape():
    print("Scenario: Haiku 5.5 request shape (effort, max_tokens, no sampling params)")
    brain.client = FakeClient(script_for("get_shopping_list", "الليستة فاضية."))
    await brain.think(ZAIN, "tg:9", "عايزين إيه من السوبرماركت؟")
    reqs = brain.client.messages.requests
    check("model is claude-haiku-5-5 on every call", {r["model"] for r in reqs} == {"claude-haiku-5-5"})
    check("effort set explicitly to low", all(r.get("output_config") == {"effort": "low"} for r in reqs))
    check("max_tokens leaves room for thinking (>= 2048)", all(r["max_tokens"] >= 2048 for r in reqs))
    check("no sampling params (they 400 on Haiku 5.5)",
          not any(k in r for r in reqs for k in ("temperature", "top_p", "top_k")))
    check("no thinking param (adaptive by default; budget_tokens would 400)",
          not any("thinking" in r for r in reqs))
    check("conversation never ends on an assistant turn (no prefill)",
          all(r["messages"][-1]["role"] == "user" for r in reqs))
    check("same tool list object every round (stable cached prefix)",
          reqs[0]["tools"] is reqs[1]["tools"] is brain.ALL_TOOLS)
    check("same system blocks every round of the turn", reqs[0]["system"] == reqs[1]["system"])
    check("no Sonnet escalation left in the brain", not hasattr(brain, "_pick_model"))


def test_static_persona_cache_invariant():
    print("Scenario: the cached persona block never varies per request")
    a = prompts.build_system_prompt(member_name="Zain", roster="Zain, Mo", channel="telegram",
                                    now=datetime(2026, 10, 11, 9, 0, tzinfo=TZ))
    b = prompts.build_system_prompt(member_name="Mo", roster="Zain, Mo, Kateb", channel="room",
                                    voice=True, now=datetime(2026, 12, 25, 23, 59, tzinfo=TZ))
    check("two system blocks: static + dynamic", len(a) == 2 and len(b) == 2)
    check("static block is byte-identical across speakers/times/channels/voice", a[0] == b[0])
    check("static block is the STATIC_PERSONA constant", a[0]["text"] == prompts.STATIC_PERSONA)
    check("cache breakpoint sits on the static block", a[0].get("cache_control") == {"type": "ephemeral"})
    check("dynamic block carries no cache breakpoint", "cache_control" not in a[1])
    check("speaker name only in the dynamic block",
          "Talking to: Mo" in b[1]["text"] and "Talking to" not in prompts.STATIC_PERSONA)
    check("date only in the dynamic block",
          "25 December 2026" in b[1]["text"] and "December 2026" not in prompts.STATIC_PERSONA)
    check("voice hint only in the dynamic block",
          "SPOKEN ALOUD" in b[1]["text"] and "SPOKEN ALOUD" not in prompts.STATIC_PERSONA)
    check("no roommate names hard-coded in the persona",
          not any(n in prompts.STATIC_PERSONA for n in ("Zain", "Kateb", "Nour")))
    check("dynamic block names the timezone", "America/Toronto" in a[1]["text"])


# =============================================================================
# Turns, history, speakers
# =============================================================================

async def test_handle_turn_logs_and_speakers():
    print("Scenario: handle_turn logs channel/thread/speaker; history carries speaker names")
    fake.logged.clear()
    # Earlier in the same group chat: Mo asked something, Abdo answered.
    # (Plus a DM in another thread that must not leak in.)
    fake.logged += [
        {"thread": "tg:6", "channel": "telegram", "role": "user", "member_id": 2,
         "content": "private DM", "modality": "text", "speaker_confidence": None},
        {"thread": "tg:5", "channel": "telegram", "role": "user", "member_id": 2,
         "content": "مين جاي النهاردة؟", "modality": "text", "speaker_confidence": None},
        {"thread": "tg:5", "channel": "telegram", "role": "assistant", "member_id": 2,
         "content": "مفيش حد مسجّل.", "modality": "text", "speaker_confidence": None},
    ]
    brain.client = FakeClient(reply_only("أهلاً يا زين!"))
    reply = await turn.handle_turn(member=ZAIN, text="hi abdo", channel="telegram", thread="tg:5")
    check("reply returned", reply == "أهلاً يا زين!")
    sent = brain.client.messages.requests[0]["messages"]
    check("earlier user turn prefixed with its speaker", sent[0]["content"] == "Mo: مين جاي النهاردة؟")
    check("assistant turn left bare", sent[1] == {"role": "assistant", "content": "مفيش حد مسجّل."})
    check("current turn prefixed with time + speaker",
          re.fullmatch(r"\[\d\d:\d\d\] Zain: hi abdo", sent[-1]["content"]) is not None)
    check("current message sent exactly once (no duplicate from early logging)",
          sum("hi abdo" in str(m["content"]) for m in sent) == 1)
    new = fake.logged[-2:]
    check("user + assistant rows logged after the turn",
          [r["role"] for r in new] == ["user", "assistant"])
    check("rows carry thread, channel, speaker, modality",
          all(r["thread"] == "tg:5" and r["channel"] == "telegram" and r["member_id"] == 1
              and r["modality"] == "text" for r in new))
    check("other threads are not mixed in",
          not any("private DM" in str(m["content"]) for m in sent))


async def test_history_time_and_age():
    print("Scenario: history carries times and drops old turns (no stale to-dos)")
    now = datetime(2026, 10, 12, 14, 22, tzinfo=TZ)
    rows = [
        {"role": "user", "content": "add the study session at 2?", "speaker": "Zain",
         "created_at": now - timedelta(hours=4)},
        {"role": "assistant", "content": "أضيفها؟", "speaker": "Zain", "created_at": now - timedelta(hours=4)},
        {"role": "user", "content": "eggs $7, split four ways", "speaker": "Zain",
         "created_at": now - timedelta(minutes=35)},
        {"role": "assistant", "content": "تمام، سجلت البيض.", "speaker": "Zain", "created_at": now - timedelta(minutes=35)},
    ]
    msgs = brain._history_messages(rows, now, TZ)
    check("turns older than 3 hours are dropped", len(msgs) == 2 and "study session" not in str(msgs))
    check("user turns carry their time", msgs[0]["content"] == "[13:47] Zain: eggs $7, split four ways")
    check("assistant turns stay bare", msgs[1]["content"] == "تمام، سجلت البيض.")
    p = prompts.STATIC_PERSONA
    check("persona: newest message only, no re-offering dropped things",
          "Answer the NEWEST message only" in p and "never ask to do it again" in p)
    v = prompts.VOICE_HINT
    check("voice: money stays exact in words", "$1.75" in v and "never" in v)


async def test_history_drops_leading_assistant():
    print("Scenario: history cut mid-pair never starts with an assistant turn")
    rows = [{"role": "assistant", "content": "old reply", "speaker": "Zain"},
            {"role": "user", "content": "hey", "speaker": None}]
    msgs = brain._history_messages(rows)
    check("leading assistant row dropped", msgs[0]["role"] == "user")
    check("unknown speaker rendered as 'Someone'", msgs[0]["content"] == "Someone: hey")


async def test_handle_turn_voice_and_failure():
    print("Scenario: voice turns log modality; a crashing brain never escapes handle_turn")
    fake.logged.clear()
    brain.client = FakeClient(reply_only("تمام."))
    await turn.handle_turn(member=ZAIN, text="فين المفتاح", channel="telegram",
                           thread="tg:7", voice=True)
    check("voice turn logged as modality=voice", all(r["modality"] == "voice" for r in fake.logged))
    check("voice hint reached the model",
          "SPOKEN ALOUD" in sys_text(brain.client.messages.requests[0]["system"]))

    real_think = brain.think

    async def boom(*a, **kw):
        raise RuntimeError("anthropic down")

    brain.think = boom
    try:
        fake.logged.clear()
        reply = await turn.handle_turn(member=ZAIN, text="hello", channel="telegram", thread="tg:7")
        check("brain crash -> honest fallback, no exception", reply == turn.FALLBACK)
        check("the failed turn is still logged", len(fake.logged) == 2)
    finally:
        brain.think = real_think


async def test_unidentified_speaker():
    print("Scenario: an unidentified speaker (room device) still gets an answer")
    brain.client = FakeClient(script_for("get_shopping_list", "الليستة فاضية."))
    fake.shopping = []
    reply = await brain.think(None, "room", "eh el na2es?", channel="room")
    first = brain.client.messages.requests[0]
    check("unknown speaker prefixed 'Someone:'", first["messages"][-1]["content"].endswith("Someone: eh el na2es?"))
    check("room channel described in context", "room speaker" in sys_text(first["system"]))
    check("tools still run without a member id", reply == "الليستة فاضية.")


# =============================================================================
# Brain: stop reasons and failure handling
# =============================================================================

async def test_refusal_handled():
    print("Scenario: stop_reason=refusal -> in-character reply, no crash")
    brain.client = FakeClient([_resp("refusal", [], stop_details=SimpleNamespace(category="cyber"))])
    reply = await brain.think(ZAIN, "tg:9", "something off-limits")
    check("refusal returns the refusal line", reply == brain.REFUSAL_REPLY)


async def test_thinking_blocks_read_by_type():
    print("Scenario: a response that starts with a thinking block still yields its text")
    brain.client = FakeClient([_resp("end_turn", [_thinking_block(), _text_block("أهو.")])])
    reply = await brain.think(ZAIN, "tg:9", "test")
    check("text extracted past the thinking block", reply == "أهو.")

    # Thinking blocks must go back unmodified with tool results.
    thinking = _thinking_block()
    brain.client = FakeClient([
        _resp("tool_use", [thinking, _tool_use_block("get_shopping_list", "t1")]),
        _resp("end_turn", [_text_block("فاضية.")]),
    ])
    await brain.think(ZAIN, "tg:9", "الليستة؟")
    second = brain.client.messages.requests[1]["messages"]
    check("assistant turn echoed back with the same thinking block object",
          second[-2]["role"] == "assistant" and second[-2]["content"][0] is thinking)


async def test_max_tokens_without_text():
    print("Scenario: thinking used up max_tokens (no text) -> fallback, never an empty send")
    brain.client = FakeClient([_resp("max_tokens", [_thinking_block()])])
    reply = await brain.think(ZAIN, "tg:9", "long question")
    check("fallback instead of empty string", reply == brain.EMPTY_REPLY)


async def test_empty_reply_fallback():
    print("Scenario: model returns blank text -> safe fallback")
    brain.client = FakeClient([_resp("end_turn", [_text_block("   ")])])
    reply = await brain.think(ZAIN, "tg:9", "إزيك")
    check("empty/blank model output replaced", reply == brain.EMPTY_REPLY)


async def test_tool_failure_degrades():
    print("Scenario: a failing tool degrades honestly (no crash, no silence)")
    real_run_tool = brain.run_tool

    async def boom(name, tool_input, member_id):
        raise RuntimeError("calendar 404")

    brain.run_tool = boom
    try:
        brain.client = FakeClient(script_for("get_calendar", "آسف، مش عارف أوصل للتقويم دلوقتي."))
        reply = await brain.think(ZAIN, "tg:9", "عندنا إيه الجمعة؟")
        check("tool exception does not crash the loop", isinstance(reply, str) and reply)
        result = brain.client.messages.requests[1]["messages"][-1]["content"][0]
        check("model got an ERROR tool_result flagged is_error",
              result["is_error"] is True and result["content"].startswith("ERROR"))
    finally:
        brain.run_tool = real_run_tool


async def test_tool_loop_terminates():
    print("Scenario: tool loop is bounded (no infinite loop)")

    class AlwaysToolUse:
        def __init__(self):
            self.n = 0

        async def create(self, **kw):
            self.n += 1
            return _resp("tool_use", [_tool_use_block("get_shopping_list", f"t{self.n}")])

    brain.client = SimpleNamespace(messages=AlwaysToolUse())
    reply = await brain.think(ZAIN, "tg:9", "loop forever")
    check("loop stops at the cap", brain.client.messages.n == brain.MAX_TOOL_ROUNDS)
    check("returns a graceful fallback", reply == brain.TOO_LONG_REPLY)


async def test_tool_dispatch_unknown():
    print("Scenario: unknown tool name")
    out = await tools.run_tool("does_not_exist", {}, member_id=1)
    check("unknown tool handled gracefully", out.startswith("Unknown tool:"))
    out = await tools.run_tool("get_dog_status", {}, member_id=1)
    check("Egypt-era dog tool is gone", out.startswith("Unknown tool:"))


# =============================================================================
# Facts (RAG)
# =============================================================================

async def test_remember_and_recall_fact():
    print("Scenario: store a household fact, then recall it")
    fake.facts.clear()
    embed_calls.clear()
    brain.client = FakeClient([
        _resp("tool_use", [_tool_use_block("remember_fact", "t1",
                                           {"content": "The Wi-Fi password is sunflower42.",
                                            "category": "wifi"})]),
        _resp("end_turn", [_text_block("تمام، حفظت الباسورد.")]),
    ])
    await brain.think(ZAIN, "tg:9", "الواي فاي الباسورد sunflower42")
    check("fact persisted", len(fake.facts) == 1 and fake.facts[0]["category"] == "wifi")
    check("stored with input_type=search_document", embed_calls == ["search_document"])

    embed_calls.clear()
    out = await tools.run_tool("recall_facts", {"query": "what's the wifi password"}, member_id=1)
    check("recall returns the stored fact", "sunflower42" in out and "[wifi]" in out)
    check("recall used input_type=search_query", embed_calls == ["search_query"])


async def test_recall_empty():
    print("Scenario: recall with nothing stored")
    fake.facts.clear()
    out = await tools.run_tool("recall_facts", {"query": "anything"}, member_id=1)
    check("honest 'no facts' when store is empty", out == "No matching household facts found.")


async def test_store_dedup():
    print("Scenario: re-stating a fact updates the row, never duplicates")
    fake.facts.clear()
    fact = {"content": "The Wi-Fi password is maple2026.", "category": "wifi"}
    out = await tools.run_tool("remember_fact", fact, member_id=1)
    check("first store inserts", out == "Stored." and len(fake.facts) == 1)
    out = await tools.run_tool("remember_fact", fact, member_id=1)
    check("re-stating the SAME fact updates in place (no second row)",
          "Updated" in out and len(fake.facts) == 1)
    await tools.run_tool("remember_fact", {"content": "The spare key is in the kitchen drawer.",
                                           "category": "location_of_things"}, member_id=1)
    check("a genuinely different fact still inserts", len(fake.facts) == 2)


async def test_store_guard_invalid_embedding():
    print("Scenario: a junk embedding is never stored as a fact")
    fake.facts.clear()
    real_embed = embeddings.embed

    async def bad_embed(text, *, input_type):
        return [0.0] * embeddings.EMBED_DIM    # all-zero => invalid

    embeddings.embed = bad_embed
    try:
        out = await tools.run_tool("remember_fact", {"content": "x", "category": "misc"}, member_id=1)
        check("invalid embedding returns an error, not 'Stored'", out.startswith("ERROR"))
        check("nothing was written on a bad embedding", len(fake.facts) == 0)
    finally:
        embeddings.embed = real_embed


def test_is_valid_robust_to_numpy():
    print("Scenario: is_valid accepts a DB-shaped (numpy float32) vector, rejects junk")
    import numpy as np
    dim = embeddings.EMBED_DIM
    check("numpy float32 ndarray of len 1024 is valid",
          embeddings.is_valid(np.full(dim, 0.0123, dtype=np.float32)) is True)
    check("plain python-float list of len 1024 still valid", embeddings.is_valid([0.01] * dim) is True)
    check("None is invalid", embeddings.is_valid(None) is False)
    check("wrong length is invalid", embeddings.is_valid([0.01] * (dim // 2)) is False)
    check("numpy ndarray of wrong length is invalid",
          embeddings.is_valid(np.zeros(dim - 1, dtype=np.float32)) is False)
    check("all-zero vector is invalid", embeddings.is_valid(np.zeros(dim, dtype=np.float32)) is False)


async def test_search_returns_db_shaped_fact():
    print("Scenario: a fact with a DB-shaped (numpy) embedding is recalled, not dropped")
    import numpy as np
    fake.facts.clear()
    embed_calls.clear()
    fake.facts.append({"id": 6, "category": "wifi", "content": "The Wi-Fi password is maple2026.",
                       "embedding": np.full(embeddings.EMBED_DIM, 0.02, dtype=np.float32)})
    out = await tools.run_tool("recall_facts", {"query": "what's the wifi password"}, member_id=1)
    check("seeded wifi fact is returned by search", "maple2026" in out and "[wifi]" in out)
    check("recall used input_type=search_query", embed_calls == ["search_query"])


# =============================================================================
# Location
# =============================================================================

async def test_where_is():
    print("Scenario: where_is (geofence + fresh/stale tense)")
    now = datetime.now(timezone.utc)             # aware, like asyncpg returns
    fresh = now - timedelta(minutes=2)
    fake.loc_rows = {
        "zain": {"name": "Zain", "lat": 43.0000, "lng": -80.0000, "updated_at": fresh},
        "mo": {"name": "Mo", "lat": 43.0270, "lng": -80.0000, "updated_at": fresh},
    }
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("fresh reading is present tense 'is home'", "Zain is home" in out and "live" in out)
    out = await tools.run_tool("where_is", {"name": "Mo"}, member_id=1)
    check("member away reads distance", "km from home" in out)
    out = await tools.run_tool("where_is", {"name": "everyone"}, member_id=1)
    check("'everyone' lists all sharers", "Zain" in out and "Mo" in out)

    stale = now - timedelta(hours=9)
    fake.loc_rows = {"zain": {"name": "Zain", "lat": 43.0, "lng": -80.0, "updated_at": stale}}
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("stale reading is past tense 'was home'", "Zain was home" in out)
    check("stale reading flagged stale with age", "stale" in out and "~9h ago" in out)
    check("stale reading hedges current whereabouts", "may not be there now" in out)

    fake.loc_rows = {}
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("not-sharing handled", "isn't sharing" in out)
    out = await tools.run_tool("where_is", {"name": "everyone"}, member_id=1)
    check("nobody-sharing handled", out == "No one is sharing their location or a status right now.")

    # "Where's Zain?" must surface a status even with no location (it once didn't).
    fake.statuses = {1: {"member_id": 1, "name": "Zain", "text": "Studying", "updated_at": now,
                         "until_at": now + timedelta(hours=1)}}
    out = await tools.run_tool("where_is", {"name": "zain"}, member_id=2)
    check("status answers 'where is' without a location",
          out.startswith("Zain's status: Studying (until") and "Not sharing a live location" in out)
    fake.loc_rows = {"zain": {"name": "Zain", "lat": 43.0, "lng": -80.0, "updated_at": fresh}}
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=2)
    check("location and status together", "Zain is home" in out and "Zain's status: Studying" in out)
    fake.loc_rows = {}
    out = await tools.run_tool("where_is", {"name": "everyone"}, member_id=2)
    check("'everyone' includes statuses of non-sharers", "Zain's status: Studying" in out and "not sharing" in out)
    out = await tools.run_tool("where_is", {"name": "Mo"}, member_id=1)
    check("no location, no status -> says both", out == "Mo isn't sharing a location and hasn't set a status.")
    fake.statuses = {}


# =============================================================================
# Calendar
# =============================================================================

async def test_get_calendar():
    print("Scenario: get_calendar")
    global _fake_events
    _fake_events = [{"id": "evt1", "start": "2026-10-16", "summary": "Suite dinner"}]
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("returns upcoming events", "Suite dinner" in out)
    check("reader exposes event id (for edits)", "evt1" in out)

    _fake_events = []
    out = await tools.run_tool("get_calendar", {"days_ahead": 3}, member_id=1)
    check("empty window handled", out == "No events on the shared calendar in that window.")

    calendar_svc.is_configured = lambda: False
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("unconfigured calendar handled", out == "The shared calendar isn't connected yet.")
    calendar_svc.is_configured = lambda: True   # restore


async def test_calendar_weekday_read():
    print("Scenario: calendar reads carry a code-resolved weekday")
    global _fake_events
    # 2026-10-16 is a Friday; 2026-10-17 is a Saturday.
    _fake_events = [
        {"id": "evt1", "start": "2026-10-16", "summary": "Midterm"},
        {"id": "evt2", "start": "2026-10-17T19:00:00", "summary": "Dinner out"},
    ]
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("all-day event shows its true weekday (Friday)", "Friday 16 October" in out)
    check("timed event shows weekday + time (Saturday ... 19:00)",
          "Saturday 17 October" in out and "at 19:00" in out)
    check("event id still exposed for edits", "evt1" in out and "evt2" in out)


def test_event_times_shown_local():
    print("Scenario: offset-aware event times are shown in local (Toronto) time, across DST")
    fmt = tools._format_event
    out = fmt({"id": "e", "summary": "x", "start": "2026-10-16T23:00:00Z"})
    check("UTC 23:00 shows as Friday 19:00 local (EDT, -4)", "Friday 16 October 2026 at 19:00" in out)
    out = fmt({"id": "e", "summary": "x", "start": "2026-11-03T01:30:00Z"})
    check("UTC after the DST switch shows as Monday 20:30 local (EST, -5)",
          "Monday 02 November 2026 at 20:30" in out)
    out = fmt({"id": "e", "summary": "x", "start": "2026-11-02T09:00:00-05:00"})
    check("local-offset time unchanged", "Monday 02 November 2026 at 09:00" in out)


async def test_calendar_write():
    print("Scenario: create_event / update_event / delete_event (day+time, code resolves date)")
    _created_events.clear()
    out = await tools.run_tool("create_event",
                               {"summary": "Suite dinner", "day": "tomorrow", "time": "19:00"},
                               member_id=1)
    check("create echoes confirmation with id", "Suite dinner" in out and "evt_new" in out)
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
    check("spoken day was resolved to a real local date in code",
          _created_events[0]["start"].startswith(tomorrow + "T19:00"))

    out = await tools.run_tool("update_event", {"event_id": "evt_new", "time": "20:00"}, member_id=1)
    check("update echoes confirmation", out.startswith("Updated:") and "evt_new" in out)

    _deleted_events.clear()
    out = await tools.run_tool("delete_event", {"event_id": "evt_new"}, member_id=1)
    check("delete confirms removal", out == "Deleted the event.")
    check("delete actually called the calendar", _deleted_events == ["evt_new"])


def test_date_resolution():
    print("Scenario: deterministic local date resolution (no model arithmetic)")
    now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)   # a Wednesday
    r = calendar_svc.resolve_date
    check("'today' -> today's date", r("today", now=now) == now.date())
    check("'tomorrow' -> +1 day", r("tomorrow", now=now) == now.date() + timedelta(days=1))
    check("'day after tomorrow' -> +2 days", r("day after tomorrow", now=now) == date(2026, 10, 16))
    check("explicit ISO date passes through", r("2026-10-16", now=now) == date(2026, 10, 16))
    check("'Wednesday' on a Wednesday -> that same Wednesday", r("Wednesday", now=now) == date(2026, 10, 14))
    check("'Friday' -> the coming Friday (16th)", r("Friday", now=now) == date(2026, 10, 16))
    check("Arabic weekday 'السبت' -> the coming Saturday (17th)", r("السبت", now=now) == date(2026, 10, 17))
    check("Franco weekday '7ad' -> the coming Sunday (18th)", r("7ad", now=now) == date(2026, 10, 18))
    check("'next Wednesday' jumps a full week (+7)", r("next Wednesday", now=now) == date(2026, 10, 21))
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    check("every weekday name resolves to its true weekday",
          all(r(n, now=now).weekday() == i for i, n in enumerate(names)))
    # Across the DST switch (Nov 1, 2026): Saturday Oct 31 -> "Monday" is Nov 2.
    sat = datetime(2026, 10, 31, 23, 30, tzinfo=TZ)
    check("weekday resolution correct across the DST change", r("Monday", now=sat) == date(2026, 11, 2))
    try:
        r("someday", now=now)
        check("gibberish day raises (no silent guess)", False)
    except ValueError:
        check("gibberish day raises (no silent guess)", True)


async def test_calendar_read_guard():
    print("Scenario: a failed/degraded read is never reported as empty")
    real_get_events = calendar_svc.get_events
    attempts = {"n": 0}

    def boom(days_ahead=7):
        attempts["n"] += 1
        raise calendar_svc.CalendarReadError("Regional Access Boundary ... FAILED_PRECONDITION")

    calendar_svc.get_events = boom
    try:
        out = await tools.run_tool("get_calendar", {}, member_id=1)
        check("failed read does NOT say 'No events'", "No events" not in out)
        check("failed read returns an honest error", out.startswith("ERROR"))
        check("failed read tells the model not to create off it", "do NOT create" in out)
        check("read was retried once before giving up", attempts["n"] == 2)
    finally:
        calendar_svc.get_events = real_get_events

    global _fake_events
    _fake_events = []
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("genuinely empty calendar still reads empty",
          out == "No events on the shared calendar in that window.")


async def test_calendar_not_found_guard():
    print("Scenario: editing a non-existent id errors, never fakes success")
    real_update, real_delete = calendar_svc.update_event, calendar_svc.delete_event

    def update_missing(event_id, summary=None, day=None, time=None, end_time=None):
        raise calendar_svc.EventNotFound(event_id)

    def delete_missing(event_id):
        raise calendar_svc.EventNotFound(event_id)

    calendar_svc.update_event = update_missing
    calendar_svc.delete_event = delete_missing
    try:
        out = await tools.run_tool("update_event", {"event_id": "made_up", "time": "20:00"}, member_id=1)
        check("update of missing id returns an error", out.startswith("ERROR") and "made_up" in out)
        out = await tools.run_tool("delete_event", {"event_id": "made_up"}, member_id=1)
        check("delete of missing id returns an error", out.startswith("ERROR") and "made_up" in out)
    finally:
        calendar_svc.update_event = real_update
        calendar_svc.delete_event = real_delete


# =============================================================================
# Shopping list + packages
# =============================================================================

async def test_shopping_list():
    print("Scenario: shared shopping list (add / view / buy / clear)")
    fake.shopping = []
    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("empty list reads empty", out == "The shopping list is empty.")
    out = await tools.run_tool("add_to_shopping_list", {"item": "milk", "qty": "2 bags"}, member_id=1)
    check("add confirms the item", "milk" in out and "Added" in out)
    await tools.run_tool("add_to_shopping_list", {"item": "bread"}, member_id=1)
    out = await tools.run_tool("add_to_shopping_list", {"item": "Milk"}, member_id=1)
    check("duplicate (case-insensitive) rejected", "already on the list" in out)
    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("list shows both items with quantity", "milk" in out and "bread" in out and "(2 bags)" in out)
    out = await tools.run_tool("mark_item_bought", {"item": "milk"}, member_id=1)
    check("marking bought confirms", "milk" in out and "bought" in out)
    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("bought item leaves the open list", "milk" not in out and "bread" in out)
    out = await tools.run_tool("mark_item_bought", {"item": "eggs"}, member_id=1)
    check("buying an absent item is honest", "isn't on the list" in out)
    out = await tools.run_tool("clear_shopping_list", {}, member_id=1)
    check("clear reports the count", "Cleared 1 item" in out)


async def test_packages():
    print("Scenario: packages (add / list / arrive / pick up), dates and owners resolved in code")
    fake.packages = []
    out = await tools.run_tool("add_package", {"description": "desk lamp", "carrier": "Amazon",
                                               "day": "tomorrow"}, member_id=1)
    check("add confirms with id", out.startswith("Recorded package #1") and "desk lamp" in out)
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    check("day resolved to a real date in code", fake.packages[0]["expected_on"] == tomorrow)
    check("owner defaults to the speaker", fake.packages[0]["owner_id"] == 1)

    out = await tools.run_tool("add_package", {"description": "textbook", "owner": "Mo"}, member_id=1)
    check("owner by name resolves to that member", fake.packages[1]["owner_id"] == 2)
    check("no date is fine", "no expected date" in out and fake.packages[1]["expected_on"] is None)

    out = await tools.run_tool("add_package", {"description": "x", "owner": "Bob"}, member_id=1)
    check("unknown owner errors instead of guessing", out.startswith("ERROR") and len(fake.packages) == 2)
    out = await tools.run_tool("add_package", {"description": "x", "day": "someday"}, member_id=1)
    check("bad day errors instead of guessing", out.startswith("ERROR") and len(fake.packages) == 2)

    out = await tools.run_tool("get_packages", {}, member_id=1)
    check("list shows both with owners and ids",
          "#1 desk lamp (Amazon) for Zain" in out and "#2 textbook for Mo" in out)
    out = await tools.run_tool("get_packages", {"owner": "Mo"}, member_id=1)
    check("owner filter works", "textbook" in out and "desk lamp" not in out)

    out = await tools.run_tool("update_package", {"package_id": 1, "status": "arrived"}, member_id=1)
    check("arrived confirms", "now arrived" in out)
    out = await tools.run_tool("get_packages", {}, member_id=1)
    check("arrived package flagged for pickup", "ARRIVED" in out)
    await tools.run_tool("update_package", {"package_id": 1, "status": "picked_up"}, member_id=1)
    out = await tools.run_tool("get_packages", {}, member_id=1)
    check("picked-up package leaves the open list", "desk lamp" not in out)
    out = await tools.run_tool("update_package", {"package_id": 1, "status": "arrived"}, member_id=1)
    check("closed package can't be changed (honest error)", out.startswith("ERROR"))
    out = await tools.run_tool("update_package", {"package_id": 99, "status": "arrived"}, member_id=1)
    check("unknown id errors, never fakes success", out.startswith("ERROR"))


# =============================================================================
# Prompt content
# =============================================================================

def test_prompt_content():
    print("Scenario: persona keeps grounding, dialect, honesty; describes the suite's tools")
    p = prompts.STATIC_PERSONA
    check("grounding mandates recall_facts before 'don't know'", "MUST call recall_facts" in p)
    check("grounding forbids guessing weekdays", "never guess or compute a weekday" in p)
    check("calendar confirms exactly once", "exactly ONCE" in p)
    check("Egyptian dialect glossary present (معنديش)", "معنديش" in p)
    check("mirrors the speaker's language mix", "mirror the person" in p)
    check("packages capability described", "packages" in p and "get_packages" in p)
    check("speaker-prefix convention explained", "starts with the time and the speaker's name" in p)
    check("web search defers to household memory", "search the web" in p and "recall_facts" in p)
    check("dogs are gone", "dog" not in p.lower())
    check("money is CAD", "Canadian dollars" in p)


def test_voice_prompt():
    print("Scenario: voice hint (spoken replies, numbers as words, dictation carve-out)")
    voice = prompts.dynamic_context(member_name="Zain", roster="Zain", channel="telegram", voice=True)
    text = prompts.dynamic_context(member_name="Zain", roster="Zain", channel="telegram")
    check("text context has no voice hint", "SPOKEN ALOUD" not in text)
    check("voice context adds the spoken-reply hint", "SPOKEN ALOUD" in voice)
    check("numbers as words, in dollars", "تلاتين دولار" in voice)
    check("carve-out for exact dictation", "heard EXACTLY" in voice and "password" in voice)
    check("carve-out drops the disfluency", "no \"uhh\"" in voice)
    check("spelling example uses the fake password", "S-U-N-4-2" in voice)


async def test_web_search_offered_and_household_routes_to_recall():
    print("Scenario: web search offered for current info; household Q routes to recall_facts")
    brain.client = FakeClient(reply_only("الجو برد شوية النهاردة، حوالي خمس درجات."))
    reply = await brain.think(ZAIN, "tg:9", "el gaw 3amel eh enharda?")
    offered = brain.client.messages.requests[0]["tools"]
    ws = next((t for t in offered if t.get("name") == "web_search"), None)
    check("web_search server tool is offered", ws is not None)
    check("web_search pinned to the basic variant for Haiku", ws and ws["type"] == "web_search_20250305")
    check("household tools offered alongside it", any(t.get("name") == "recall_facts" for t in offered))
    check("current-info question got a normal text answer", isinstance(reply, str) and reply)

    fake.facts.clear()
    embed_calls.clear()
    brain.client = FakeClient(script_for("recall_facts", "الباسورد ...", {"query": "wifi password"}))
    await brain.think(ZAIN, "tg:9", "الواي فاي الباسورد إيه؟")
    check("household question routed to recall_facts, not the web", embed_calls == ["search_query"])


# =============================================================================
# Telegram channel
# =============================================================================

def test_parse_update():
    print("Scenario: telegram.parse_update (typed payloads)")
    text_update = {"message": {"chat": {"id": 5}, "from": {"id": 7}, "text": "hi"}}
    check("parses a text message", telegram.parse_update(text_update) ==
          {"chat_id": 5, "chat_type": "private", "from_user": {"id": 7}, "kind": "text", "text": "hi"})
    loc_update = {"edited_message": {"chat": {"id": 5}, "from": {"id": 7},
                                     "location": {"latitude": 43.1, "longitude": -80.2}}}
    check("parses a live-location edit", telegram.parse_update(loc_update) ==
          {"chat_id": 5, "chat_type": "private", "from_user": {"id": 7}, "kind": "location", "lat": 43.1, "lng": -80.2})
    voice_update = {"message": {"chat": {"id": 5}, "from": {"id": 7},
                                "voice": {"file_id": "vf1", "duration": 4}}}
    check("parses a voice message", telegram.parse_update(voice_update) ==
          {"chat_id": 5, "chat_type": "private", "from_user": {"id": 7}, "kind": "voice", "file_id": "vf1", "duration": 4})
    photo = {"message": {"chat": {"id": 5}, "from": {"id": 7}, "photo": [{"file_id": "x"}]}}
    check("ignores unsupported (photo) update", telegram.parse_update(photo) is None)
    check("ignores sender-less update", telegram.parse_update({"message": {"chat": {"id": 5}}}) is None)
    check("ignores empty update", telegram.parse_update({}) is None)


def test_telegram_webhook():
    print("Scenario: Telegram webhook routes through handle_turn (secrets, identity, location, text, voice)")
    from fastapi.testclient import TestClient
    from app import stt, tts
    from app.channels import telegram as tg_channel
    from app.main import app

    sent, voices = [], []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    async def send_voice(chat_id, ogg):
        voices.append((chat_id, ogg))

    async def noop(*a, **kw):
        return None

    async def get_file_path(file_id):
        return "voice/file.ogg"

    async def download_file(path):
        return b"ogg-bytes"

    async def transcribe(audio):
        return "فين المفتاح؟"

    async def synthesize(text):
        return b"ogg-reply"

    patches = {telegram: dict(send_message=send_message, send_voice=send_voice, send_typing=noop,
                              send_recording=noop, get_file_path=get_file_path,
                              download_file=download_file),
               stt: dict(transcribe=transcribe), tts: dict(synthesize=synthesize)}
    saved = {mod: {k: getattr(mod, k) for k in attrs} for mod, attrs in patches.items()}
    for mod, attrs in patches.items():
        for k, v in attrs.items():
            setattr(mod, k, v)

    client = TestClient(app)   # no context manager -> lifespan (real DB pool) not started
    hdr = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
    try:
        def post(update, path="/tg/test-secret", headers=hdr):
            return client.post(path, json=update, headers=headers)

        text_update = {"message": {"chat": {"id": 5}, "from": {"id": 111}, "text": "إزيك يا عبده"}}
        check("bad path secret -> 403", post(text_update, path="/tg/wrong").status_code == 403)
        check("bad header secret -> 403",
              post(text_update, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}).status_code == 403)

        sent.clear()
        r = post({"message": {"chat": {"id": 5}, "from": {"id": 999}, "text": "hi"}})
        check("unknown sender gets the polite refusal with their own id to pass on",
              r.status_code == 200 and sent == [(5, tg_channel.UNKNOWN_SENDER.format(id=999))] and "999" in sent[0][1])

        sent.clear()
        fake.locations.clear()
        r = post({"edited_message": {"chat": {"id": 5}, "from": {"id": 111},
                                     "location": {"latitude": 43.0, "longitude": -80.0}}})
        check("location ping recorded silently (no reply)",
              r.status_code == 200 and sent == [] and fake.locations == {1: (43.0, -80.0)})

        sent.clear()
        fake.logged.clear()
        brain.client = FakeClient(reply_only("الحمد لله، وانت؟"))
        r = post(text_update)
        check("text reply delivered", r.status_code == 200 and sent == [(5, "الحمد لله، وانت؟")])
        check("private chat logged on the personal thread dm:<member_id>",
              [(x["thread"], x["member_id"], x["role"]) for x in fake.logged]
              == [("dm:1", 1, "user"), ("dm:1", 1, "assistant")])

        sent.clear()
        fake.logged.clear()
        brain.client = FakeClient(reply_only("أهلاً يا شباب"))
        post({"message": {"chat": {"id": -100, "type": "group"}, "from": {"id": 222},
                          "text": "@abdo ezayak"}})
        check("group chat keeps its own shared thread tg:<chat_id>",
              sent == [(-100, "أهلاً يا شباب")] and fake.logged[0]["thread"] == "tg:-100")

        sent.clear()
        voices.clear()
        fake.logged.clear()
        brain.client = FakeClient(reply_only("في الدرج."))
        r = post({"message": {"chat": {"id": 5}, "from": {"id": 222},
                              "voice": {"file_id": "vf1", "duration": 3}}})
        check("voice webhook returns 200 immediately", r.status_code == 200)
        check("voice: text first, then the voice note",
              sent == [(5, "في الدرج.")] and voices == [(5, b"ogg-reply")])
        check("voice turn logged as Mo with modality=voice",
              all(x["member_id"] == 2 and x["modality"] == "voice" for x in fake.logged)
              and fake.logged[0]["content"] == "فين المفتاح؟")

        sent.clear()
        post({"message": {"chat": {"id": 5}, "from": {"id": 111},
                          "voice": {"file_id": "vf2", "duration": 999}}})
        check("over-long voice note politely declined", sent == [(5, tg_channel.TOO_LONG_VOICE)])

        from app import main as _main
        check("health endpoint reports the version",
              client.get("/api/health").json()["version"] == _main.VERSION)
    finally:
        for mod, attrs in saved.items():
            for k, v in attrs.items():
                setattr(mod, k, v)


def test_config_strips_whitespace():
    print("Scenario: secrets pasted with stray whitespace still match")
    from app.config import Settings
    s = Settings(anthropic_api_key="k", telegram_bot_token=" 1:abc\n", telegram_webhook_secret="  s3cret \n",
                 database_url="postgresql://x/y")
    check("webhook secret stripped", s.telegram_webhook_secret == "s3cret")
    check("bot token stripped", s.telegram_bot_token == "1:abc")


def test_access_log_redaction():
    print("Scenario: uvicorn access log never prints the webhook secret")
    import logging
    from app.main import RedactWebhookSecret
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "POST", "/tg/supersecret", "1.1", 200), None)
    RedactWebhookSecret().filter(rec)
    check("webhook path redacted", "supersecret" not in rec.getMessage()
          and "/tg/<redacted>" in rec.getMessage())
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/me", "1.1", 200), None)
    RedactWebhookSecret().filter(rec)
    check("other paths untouched", "/api/me" in rec.getMessage())


def test_web_app():
    print("Scenario: web app API (login by Telegram code, chat, voice, lists, calendar, push, SPA)")
    import base64 as _b64
    import re
    import tempfile
    from pathlib import Path
    from fastapi.testclient import TestClient
    from app import auth, push, stt, tts, main as app_main

    settings.session_pepper = "test-pepper"
    dms = []

    async def send_message(chat_id, text):
        dms.append((chat_id, text))

    real_send = telegram.send_message
    telegram.send_message = send_message
    fake.codes.clear()
    fake.sessions.clear()
    client = TestClient(app_main.app, base_url="https://testserver")
    H = {"X-Abdo": "1"}

    def code_from(dm_text):
        return re.search(r"\b(\d{6})\b", dm_text).group(1)

    try:
        # --- auth ---------------------------------------------------------------
        check("API without a session -> 401", client.get("/api/me").status_code == 401)
        r = client.post("/api/auth/request-code", json={"name": "Zain"})
        check("mutating request without X-Abdo -> 403", r.status_code == 403)

        r = client.post("/api/auth/request-code", json={"name": "Zain"}, headers=H)
        check("request-code ok for a real member", r.json() == {"ok": True})
        check("code DM'd to that member's Telegram id", len(dms) == 1 and dms[0][0] == 111)
        check("only the code's hash is stored", code_from(dms[0][1]) not in str(fake.codes))

        dms.clear()
        r1 = client.post("/api/auth/request-code", json={"name": "Nobody"}, headers=H)
        r2 = client.post("/api/auth/request-code", json={"name": "Sam"}, headers=H)
        check("unknown name and no-Telegram member get the same 'ok' (no enumeration)",
              r1.json() == r2.json() == {"ok": True} and dms == [])

        # Latest code wins; wrong codes count toward the limit.
        dms.clear()
        client.post("/api/auth/request-code", json={"name": "zain"}, headers=H)
        good = code_from(dms[-1][1])
        bad = "000000" if good != "000000" else "111111"
        r = client.post("/api/auth/verify", json={"name": "Zain", "code": bad}, headers=H)
        check("wrong code -> 400, no cookie", r.status_code == 400 and auth.COOKIE not in r.cookies)

        r = client.post("/api/auth/verify", json={"name": "Zain", "code": good}, headers=H)
        cookie_hdr = r.headers.get("set-cookie", "")
        check("right code -> member + session cookie", r.status_code == 200
              and r.json()["member"]["name"] == "Zain" and "abdo_session=" in cookie_hdr)
        check("cookie is HttpOnly, Secure, SameSite=Lax",
              "HttpOnly" in cookie_hdr and "Secure" in cookie_hdr and "samesite=lax" in cookie_hdr.lower())
        check("session token stored only as a hash",
              r.cookies.get(auth.COOKIE) not in fake.sessions)
        r = client.post("/api/auth/verify", json={"name": "Zain", "code": good}, headers=H)
        check("a used code can't be replayed", r.status_code == 400)
        check("/api/me works with the session", client.get("/api/me").json()["member"]["id"] == 1)

        # Five wrong attempts kill a code, even before the right one is tried.
        other = TestClient(app_main.app, base_url="https://testserver")
        dms.clear()
        other.post("/api/auth/request-code", json={"name": "Mo"}, headers=H)
        mo_code = code_from(dms[-1][1])
        wrong = "000000" if mo_code != "000000" else "111111"
        for _ in range(5):
            other.post("/api/auth/verify", json={"name": "Mo", "code": wrong}, headers=H)
        r = other.post("/api/auth/verify", json={"name": "Mo", "code": mo_code}, headers=H)
        check("5 wrong attempts kill the code", r.status_code == 400)

        # Expired codes never work.
        dms.clear()
        other.post("/api/auth/request-code", json={"name": "Mo"}, headers=H)
        fake.codes[-1]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        r = other.post("/api/auth/verify", json={"name": "Mo", "code": code_from(dms[-1][1])}, headers=H)
        check("expired code rejected", r.status_code == 400)

        # Rate limit: at most 3 codes per member per 15 minutes.
        fake.codes.clear()
        dms.clear()
        for _ in range(5):
            other.post("/api/auth/request-code", json={"name": "Mo"}, headers=H)
        check("code requests rate-limited to 3 per window", len(dms) == 3)

        # --- chat ---------------------------------------------------------------
        fake.logged.clear()
        fake.logged.append({"id": 900, "thread": "dm:1", "channel": "telegram", "role": "user",
                            "member_id": 1, "content": "from telegram", "modality": "text",
                            "speaker_confidence": None, "created_at": datetime.now(timezone.utc)})
        fake._msg_seq = 900
        brain.client = FakeClient(reply_only("أهلاً من الويب"))
        r = client.post("/api/chat", json={"text": "hi from the app"}, headers=H)
        check("chat goes through handle_turn and returns the reply", r.json() == {"reply": "أهلاً من الويب"})
        sent_msgs = brain.client.messages.requests[0]["messages"]
        check("web chat shares the Telegram DM history (dm:<id>)",
              sent_msgs[0]["content"].endswith("Zain: from telegram"))
        check("web turn logged with channel=web on dm:1",
              [(x["thread"], x["channel"]) for x in fake.logged[-2:]] == [("dm:1", "web"), ("dm:1", "web")])
        h = client.get("/api/chat/history").json()
        check("history returns both channels, oldest first",
              [m["channel"] for m in h["messages"]] == ["telegram", "web", "web"] and h["has_more"] is False)
        h = client.get("/api/chat/history?limit=1").json()
        check("history pages backwards", len(h["messages"]) == 1 and h["has_more"] is True)
        older = client.get(f"/api/chat/history?limit=5&before={h['messages'][0]['id']}").json()
        check("before=<id> returns older messages", len(older["messages"]) == 2)
        check("chat rejects empty text", client.post("/api/chat", json={"text": ""}, headers=H).status_code == 422)

        # --- voice --------------------------------------------------------------
        real_tx, real_mp3 = stt.transcribe, tts.synthesize_mp3
        got = {}

        async def transcribe(audio, filename="x", content_type="x"):
            got.update(filename=filename, content_type=content_type)
            return "فين المفتاح؟"

        async def synthesize_mp3(text):
            return b"mp3-bytes"

        stt.transcribe, tts.synthesize_mp3 = transcribe, synthesize_mp3
        settings.elevenlabs_api_key, settings.tts_voice_id = "xi-fake", "voice-fake"
        try:
            brain.client = FakeClient(reply_only("في الدرج."))
            r = client.post("/api/chat/voice", headers=H,
                            files={"audio": ("rec.webm", b"webm-bytes", "audio/webm;codecs=opus")})
            j = r.json()
            check("voice: transcript + reply + base64 MP3",
                  j["transcript"] == "فين المفتاح؟" and j["reply"] == "في الدرج."
                  and _b64.b64decode(j["audio"]) == b"mp3-bytes")
            check("recording passed to STT with its real type", got["content_type"] == "audio/webm")
            check("voice turn logged as modality=voice on channel web",
                  fake.logged[-1]["modality"] == "voice" and fake.logged[-1]["channel"] == "web")

            async def tts_down(text):
                raise RuntimeError("elevenlabs down")

            tts.synthesize_mp3 = tts_down
            brain.client = FakeClient(reply_only("تمام."))
            j = client.post("/api/chat/voice", headers=H,
                            files={"audio": ("rec.m4a", b"mp4-bytes", "audio/mp4")}).json()
            check("TTS failure degrades to text (audio null)", j["reply"] == "تمام." and j["audio"] is None)

            async def blank(audio, filename="x", content_type="x"):
                return ""

            stt.transcribe = blank
            n = len(fake.logged)
            j = client.post("/api/chat/voice", headers=H,
                            files={"audio": ("rec.webm", b"x", "audio/webm")}).json()
            check("blank transcript -> 'didn't hear', nothing logged",
                  j["transcript"] == "" and j["audio"] is None and len(fake.logged) == n)
        finally:
            stt.transcribe, tts.synthesize_mp3 = real_tx, real_mp3
            settings.elevenlabs_api_key, settings.tts_voice_id = None, None

        # --- shopping + packages ---------------------------------------------------
        fake.shopping = []
        check("add item", client.post("/api/shopping", json={"item": "rice"}, headers=H).status_code == 200)
        check("duplicate item -> 409", client.post("/api/shopping", json={"item": "Rice"}, headers=H).status_code == 409)
        check("list shows it", [i["item"] for i in client.get("/api/shopping").json()["items"]] == ["rice"])
        check("bought absent item -> 404",
              client.post("/api/shopping/bought", json={"item": "eggs"}, headers=H).status_code == 404)
        client.post("/api/shopping/bought", json={"item": "rice"}, headers=H)
        check("bought item leaves the list", client.get("/api/shopping").json()["items"] == [])
        check("Abdo sees the same rows the app wrote", fake.shopping[0]["bought"] is True)

        fake.packages = []
        r = client.post("/api/packages", json={"description": "lamp", "day": "tomorrow"}, headers=H)
        check("package added, owner defaults to the logged-in member",
              r.status_code == 200 and fake.packages[0]["owner_id"] == 1)
        check("day resolved in code",
              fake.packages[0]["expected_on"] == (datetime.now(TZ) + timedelta(days=1)).date())
        check("bad day -> 400",
              client.post("/api/packages", json={"description": "x", "day": "someday"}, headers=H).status_code == 400)
        check("unknown owner -> 400",
              client.post("/api/packages", json={"description": "x", "owner": "Bob"}, headers=H).status_code == 400)
        pk = client.get("/api/packages").json()["packages"]
        check("packages listed with owner + ISO date", pk[0]["owner"] == "Zain" and pk[0]["expected_on"])
        check("bad status -> 400",
              client.patch(f"/api/packages/{pk[0]['id']}", json={"status": "lost"}, headers=H).status_code == 400)
        check("arrived ok",
              client.patch(f"/api/packages/{pk[0]['id']}", json={"status": "arrived"}, headers=H).status_code == 200)
        check("unknown id -> 404",
              client.patch("/api/packages/99", json={"status": "arrived"}, headers=H).status_code == 404)

        # --- calendar ---------------------------------------------------------------
        global _fake_events
        _fake_events = [{"id": "e1", "summary": "Dinner", "start": "2026-10-16T23:00:00Z"}]
        ev = client.get("/api/calendar").json()
        check("calendar events carry code-resolved local day/time",
              ev["configured"] and ev["events"][0]["day"] == "Friday 16 October 2026"
              and ev["events"][0]["time"] == "19:00")
        real_get = calendar_svc.get_events

        def degraded(days_ahead=7):
            raise calendar_svc.CalendarReadError("boundary")

        calendar_svc.get_events = degraded
        try:
            check("degraded calendar read -> 503, never an empty list",
                  client.get("/api/calendar").status_code == 503)
        finally:
            calendar_svc.get_events = real_get

        # --- push ------------------------------------------------------------------
        check("no push key when push isn't configured", client.get("/api/push/public-key").json() == {"key": None})
        check("test push -> 503 when not configured",
              client.post("/api/push/test", headers=H).status_code == 503)
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "p", "auth": "a"}}
        check("subscribe stores the subscription",
              client.post("/api/push/subscribe", json=sub, headers=H).status_code == 200
              and "https://push.example/abc" in fake.push_subs)
        check("non-https endpoint rejected",
              client.post("/api/push/subscribe", headers=H,
                          json={**sub, "endpoint": "http://x"}).status_code == 400)
        settings.vapid_public_key, settings.vapid_private_key = "pub", "priv"
        real_send_push = push._send
        delivered = []
        push._send = lambda s, payload: delivered.append((s["endpoint"], payload))
        try:
            r = client.post("/api/push/test", headers=H)
            check("test push delivered to the member's device",
                  r.json() == {"sent": 1} and "notifications work" in delivered[0][1])

            from pywebpush import WebPushException

            def gone(s, payload):
                raise WebPushException("gone", response=SimpleNamespace(status_code=410))

            push._send = gone
            client.post("/api/push/test", headers=H)
            check("expired subscription (410) is deleted", "https://push.example/abc" not in fake.push_subs)
        finally:
            push._send = real_send_push
            settings.vapid_public_key, settings.vapid_private_key = None, None

        # --- logout ----------------------------------------------------------------
        client.post("/api/auth/logout", headers=H)
        check("logout ends the session", client.get("/api/me").status_code == 401)

        # --- serving the SPA ---------------------------------------------------------
        real_dist = app_main.WEB_DIST
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            (dist / "index.html").write_text("<!doctype html><title>Abdo</title>")
            (dist / "sw.js").write_text("// sw")
            (dist / "assets").mkdir()
            (dist / "assets" / "app-123.js").write_text("// app")
            app_main.WEB_DIST = dist.resolve()
            try:
                r = client.get("/lists")
                check("client-side route serves index.html", "<title>Abdo</title>" in r.text)
                r = client.get("/sw.js")
                check("service worker served, never cached", r.text == "// sw"
                      and r.headers.get("cache-control") == "no-cache")
                check("hashed asset served", client.get("/assets/app-123.js").text == "// app")
                check("unknown /api path is a JSON 404, not the app",
                      client.get("/api/nope").status_code == 404)
                r = client.get("/..%2F..%2Fetc%2Fpasswd")
                check("path traversal falls back to index.html", "<title>Abdo</title>" in r.text)
            finally:
                app_main.WEB_DIST = real_dist
    finally:
        telegram.send_message = real_send
        settings.session_pepper = None


# =============================================================================
# Phase 7a: proactive Abdo
# =============================================================================

def test_resolve_due_and_until():
    print("Scenario: reminder/status times are resolved in code (timers, day+time, DST, past)")
    rd, ru, TE = tools.resolve_due, tools.resolve_until, tools.TimeError_
    now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)      # Wednesday noon
    check("in 35 minutes", rd(35, now=now) == now + timedelta(minutes=35))
    check("tomorrow 09:00", rd(day="tomorrow", time="09:00", now=now) == datetime(2026, 10, 15, 9, 0, tzinfo=TZ))
    check("time alone means today", rd(time="21:30", now=now) == datetime(2026, 10, 14, 21, 30, tzinfo=TZ))

    def raises(fn):
        try:
            fn()
            return False
        except TE:
            return True

    check("a time already passed today is rejected", raises(lambda: rd(time="11:00", now=now)))
    check("both forms at once rejected", raises(lambda: rd(10, day="today", time="13:00", now=now)))
    check("neither form rejected", raises(lambda: rd(now=now)))
    check("0 minutes rejected", raises(lambda: rd(0, now=now)))
    check("gibberish time rejected", raises(lambda: rd(time="soonish", now=now)))
    sat = datetime(2026, 10, 31, 23, 30, tzinfo=TZ)
    due = rd(day="tomorrow", time="09:00", now=sat)
    check("across the DST switch: 09:00 EST on Nov 1", due.date() == date(2026, 11, 1)
          and due.utcoffset() == timedelta(hours=-5) and due.hour == 9)
    check("status without an end = until cleared", ru(now=now) is None)
    check("status until a day = end of that day", ru(day="Friday", now=now) == datetime(2026, 10, 16, 23, 59, tzinfo=TZ))
    check("status until a time today", ru(time="15:00", now=now) == datetime(2026, 10, 14, 15, 0, tzinfo=TZ))


async def test_reminder_and_status_tools():
    print("Scenario: reminder + status tools (who, when, honest errors, cancel rights)")
    fake.reminders.clear()
    out = await tools.run_tool("set_reminder", {"text": "laundry", "in_minutes": 35}, member_id=1)
    check("timer set and the resolved time read back", out.startswith("Reminder #") and "laundry" in out
          and fake.reminders[0]["member_id"] == 1 and fake.reminders[0]["created_by"] == 1)
    out = await tools.run_tool("set_reminder", {"text": "charger", "for": "Mo", "day": "tomorrow",
                                                "time": "09:00"}, member_id=1)
    check("reminder for a roommate is theirs, set by the speaker",
          fake.reminders[1]["member_id"] == 2 and fake.reminders[1]["created_by"] == 1 and "for Mo" in out)
    out = await tools.run_tool("set_reminder", {"text": "x", "for": "Bob", "in_minutes": 5}, member_id=1)
    check("unknown roommate -> honest error", out.startswith("ERROR") and len(fake.reminders) == 2)
    out = await tools.run_tool("set_reminder", {"text": "x", "day": "today", "time": "00:00"}, member_id=1)
    check("a past time -> honest error, nothing saved", out.startswith("ERROR") and len(fake.reminders) == 2)
    out = await tools.run_tool("list_reminders", {}, member_id=1)
    check("list shows both with ids and who set Mo's", "#1" in out and "#2" in out and "set by Zain" in out)
    out = await tools.run_tool("cancel_reminder", {"reminder_id": 1}, member_id=2)
    check("can't cancel someone else's own reminder", out.startswith("ERROR") and not fake.reminders[0]["done"])
    out = await tools.run_tool("cancel_reminder", {"reminder_id": 2}, member_id=2)
    check("can cancel a reminder that's for you", out.startswith("Cancelled") and fake.reminders[1]["done"])
    out = await tools.run_tool("cancel_reminder", {"reminder_id": 99}, member_id=1)
    check("fake id -> honest error", out.startswith("ERROR"))

    fake.statuses.clear()
    out = await tools.run_tool("set_status", {"text": "in a midterm", "until_day": "tomorrow",
                                              "until_time": "15:00"}, member_id=2)
    check("status set with its end", out.startswith("Status set") and fake.statuses[2]["until_at"] is not None)
    brain.client = FakeClient(reply_only("ماشي."))
    await brain.think(ZAIN, "dm:1", "where's Mo?")
    sys_blocks = brain.client.messages.requests[0]["system"]
    check("statuses reach Abdo in the dynamic context", "Mo: in a midterm (until" in sys_blocks[1]["text"])
    check("…and never in the cached persona", "in a midterm (until" not in sys_blocks[0]["text"])
    fake.statuses[2]["until_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    brain.client = FakeClient(reply_only("ماشي."))
    await brain.think(ZAIN, "dm:1", "where's Mo?")
    check("expired status is ignored",
          "Statuses right now" not in brain.client.messages.requests[0]["system"][1]["text"])
    out = await tools.run_tool("clear_status", {}, member_id=2)
    check("clear status", out == "Status cleared." and 2 not in fake.statuses)

    out = await tools.run_tool("get_prayer_times", {"day": "tomorrow"}, member_id=1)
    check("prayer times tool answers from code", "Fajr" in out and "Isha" in out and "p.m." in out)


def test_prayer_times():
    print("Scenario: prayer times (order, solar noon, local day, DST, Asr school)")
    from app import prayer
    day = date(2026, 10, 12)
    t = prayer.times_for(day)
    check("all on the requested local day", all(v.date() == day for v in t.values()))
    check("ordered fajr < sunrise < dhuhr < asr < maghrib < isha",
          all(t[a] < t[b] for a, b in zip(prayer.ORDER, prayer.ORDER[1:])))
    check("dhuhr near solar noon (EDT, ~13:00 at 80°W in October)",
          datetime(2026, 10, 12, 12, 45, tzinfo=TZ) < t["dhuhr"] < datetime(2026, 10, 12, 13, 30, tzinfo=TZ))
    dst = prayer.times_for(date(2026, 11, 1))
    check("DST day works, dhuhr an hour earlier on the clock",
          dst["dhuhr"].utcoffset() == timedelta(hours=-5) and dst["dhuhr"].hour == 12)
    real = settings.prayer_asr
    settings.prayer_asr = "HANAFI"
    try:
        check("Hanafi Asr is later than standard", prayer.times_for(day)["asr"] > t["asr"])
    finally:
        settings.prayer_asr = real
    check("12-hour label", prayer.clock12(datetime(2026, 1, 1, 15, 52)) == "3:52 p.m."
          and prayer.clock12(datetime(2026, 1, 1, 0, 5)) == "12:05 a.m.")


async def test_notify_deliver():
    print("Scenario: delivery = push if it lands, else Telegram (never both); Fajr silent")
    from app import notify, push
    real_send, real_tg = push.send_to_member, telegram.send_message
    tg = []

    async def tg_send(chat_id, text, disable_notification=False):
        tg.append((chat_id, text, disable_notification))

    telegram.send_message = tg_send
    try:
        async def one(*a, **kw):
            return 1
        push.send_to_member = one
        check("push lands -> no Telegram", await notify.deliver(ZAIN, "t", "b") == "push" and tg == [])

        async def zero(*a, **kw):
            return 0
        push.send_to_member = zero
        check("no push device -> Telegram", await notify.deliver(ZAIN, "Fajr", "x", silent=True) == "telegram"
              and tg == [(111, "Fajr\nx", True)])

        async def boom(*a, **kw):
            raise RuntimeError("push service down")
        push.send_to_member = boom
        tg.clear()
        check("push error -> Telegram fallback", await notify.deliver(ZAIN, "t", "b") == "telegram" and len(tg) == 1)
        push.send_to_member = zero
        check("no push and no Telegram -> 'none'", await notify.deliver(SAM, "t", "b") == "none")
    finally:
        push.send_to_member, telegram.send_message = real_send, real_tg
    check("prefs default on at 08:00", notify.prefs_of(ZAIN) == {"briefing": True, "briefing_time": "08:00", "prayers": True})


async def test_scheduler():
    print("Scenario: scheduler sends each thing exactly once, on time, and never stale")
    from app import notify, prayer, scheduler, briefing
    sent = []

    async def deliver(member, title, body, url="/", silent=False, urgent=False, ack=None):
        sent.append((member["id"], title, body, silent))
        return "push"

    async def build(member, now=None):
        return ("Good morning", "today")

    real_deliver, real_build = notify.deliver, briefing.build
    notify.deliver, briefing.build = deliver, build
    for m in MEMBERS.values():
        m.pop("prefs", None)
    try:
        # Reminders: once, even with two overlapping ticks (a zero-downtime deploy).
        fake.reminders.clear()
        fake.sent_log.clear()
        now = datetime(2026, 10, 14, 7, 0, tzinfo=TZ)           # before the briefing
        await fake.add_reminder(1, "laundry", now - timedelta(minutes=1), 1)
        await fake.add_reminder(2, "charger", now - timedelta(minutes=1), 1)
        await fake.add_reminder(1, "old one", now - timedelta(hours=7), 1)
        await fake.add_reminder(1, "later", now + timedelta(hours=1), 1)
        await asyncio.gather(scheduler.tick(now), scheduler.tick(now))
        bodies = sorted(b for _, _, b, _ in sent)
        check("each due reminder delivered exactly once (two concurrent ticks)",
              bodies == sorted(["laundry", "From Zain: charger", "(late) old one"]))
        check("future reminder untouched", not fake.reminders[3]["done"])
        sent.clear()
        await scheduler.tick(now)
        check("nothing resent on the next tick", sent == [])

        # Briefings: after 08:00, before noon, once per member per day, respecting prefs.
        await scheduler.tick(datetime(2026, 10, 14, 7, 59, tzinfo=TZ))
        check("no briefing before 08:00", sent == [])
        MEMBERS[2]["prefs"] = {"briefing": False}
        MEMBERS[3]["prefs"] = {"briefing_time": "09:30"}
        await scheduler.tick(datetime(2026, 10, 14, 8, 1, tzinfo=TZ))
        check("08:01: only members due get it (Mo off, Sam at 09:30)",
              [m for m, t, _, _ in sent if t == "Good morning"] == [1])
        await scheduler.tick(datetime(2026, 10, 14, 9, 31, tzinfo=TZ))
        check("09:31: Sam's custom time; Zain not again",
              [m for m, t, _, _ in sent if t == "Good morning"] == [1, 3])
        sent.clear()
        await scheduler.tick(datetime(2026, 10, 15, 12, 30, tzinfo=TZ))
        check("missed by noon -> skipped, not sent stale", not any(t == "Good morning" for _, t, _, _ in sent))
        for m in MEMBERS.values():
            m.pop("prefs", None)

        # Prayers: inside the 20-minute window, once per member; Fajr silent; prefs respected.
        fake.sent_log.clear()
        sent.clear()
        day = date(2026, 10, 16)
        times = prayer.times_for(day)
        MEMBERS[3]["prefs"] = {"prayers": False, "briefing": False}
        MEMBERS[1]["prefs"] = MEMBERS[2]["prefs"] = {"briefing": False}
        await scheduler.tick(times["asr"] + timedelta(minutes=5))
        asr = [(m, s) for m, t, _, s in sent if t.startswith("Asr")]
        check("Asr sent to members with prayers on, not silent", asr == [(1, False), (2, False)])
        sent.clear()
        await scheduler.tick(times["asr"] + timedelta(minutes=6))
        check("Asr not resent", sent == [])
        await scheduler.tick(times["maghrib"] + timedelta(minutes=25))
        check("outside the window -> skipped", sent == [])
        await scheduler.tick(times["fajr"] + timedelta(minutes=1))
        fajr = [(m, s) for m, t, _, s in sent if t.startswith("Fajr")]
        check("Fajr is silent", fajr and all(s for _, s in fajr))
    finally:
        notify.deliver, briefing.build = real_deliver, real_build
        for m in MEMBERS.values():
            m.pop("prefs", None)


def test_calendar_no_duplicates():
    print("Scenario: creating the same event twice hands back the first one")
    store = []

    class Req:
        def __init__(self, fn):
            self.fn = fn

        def execute(self):
            return self.fn()

    class Events:
        def list(self, **kw):
            return Req(lambda: {"items": [e for e in store if kw["timeMin"] <= e["start"]["dateTime"] < kw["timeMax"]]})

        def insert(self, calendarId, body):
            def go():
                ev = {"id": f"ev{len(store) + 1}", "summary": body["summary"],
                      "start": {"dateTime": body["start"]["dateTime"] + "-04:00"}}
                store.append(ev)
                return ev
            return Req(go)

    class Service:
        def events(self):
            return Events()

    real = calendar_svc._get_service
    calendar_svc._get_service = lambda: Service()
    try:
        now = datetime(2026, 10, 14, 9, 0, tzinfo=TZ)
        a = _real_create_event("Suite dinner", "Friday", "19:00", now=now)
        b = _real_create_event("suite dinner ", "Friday", "19:00", now=now)
        c = _real_create_event("Suite dinner", "Friday", "20:00", now=now)
        check("first create adds it", not a.get("existing") and len(store) >= 1)
        check("same title + same start: the existing event comes back, nothing added",
              b.get("existing") is True and b["id"] == a["id"])
        check("a different time is a different event", not c.get("existing") and len(store) == 2)
    finally:
        calendar_svc._get_service = real


async def test_calendar_view_cache():
    print("Scenario: the app's calendar reads are cached for a minute; Abdo's never are")
    global _fake_events
    calls = []
    real = calendar_svc.get_events

    def counting(days):
        calls.append(days)
        return list(_fake_events)

    calendar_svc.get_events = counting
    tools.CALENDAR_CACHE_SECONDS = 60
    tools.invalidate_calendar_cache()
    try:
        _fake_events = [{"id": "e1", "summary": "A", "start": "2026-10-14T19:00:00-04:00"}]
        await tools.read_calendar(7, cached=True)
        await tools.read_calendar(7, cached=True)
        check("two app reads, one Google call", calls == [7])
        await tools.read_calendar(7)
        check("Abdo's read goes to Google", calls == [7, 7])
        tools.invalidate_calendar_cache()
        await tools.read_calendar(7, cached=True)
        check("a write clears the cache", calls == [7, 7, 7])
    finally:
        calendar_svc.get_events = real
        tools.CALENDAR_CACHE_SECONDS = 0
        tools.invalidate_calendar_cache()


async def test_urgent_reminders():
    print("Scenario: urgent reminders repeat (3 sends max, 2 min apart) until seen")
    from app import scheduler, notify
    sent = []

    async def deliver(member, title, body, url="/", silent=False, urgent=False, ack=None):
        sent.append((member["id"], title, body, urgent, ack))
        return "push"

    real = notify.deliver
    notify.deliver = deliver
    fake.reminders = []
    try:
        t0 = datetime(2026, 10, 14, 15, 0, tzinfo=TZ)
        out = await tools.run_tool("set_reminder", {"text": "call the landlord", "in_minutes": 5, "urgent": True}, 1)
        check("urgent reminder: the tool says it repeats", "Urgent" in out and fake.reminders[-1]["urgent"] is True)
        await tools.run_tool("set_reminder", {"text": "ضروري ادفع الإيجار", "in_minutes": 5}, 1)
        check("an urgent word in the reminder makes it urgent even without the flag", fake.reminders[-1]["urgent"] is True)
        await tools.run_tool("set_reminder", {"text": "buy bread", "in_minutes": 5}, 1)
        check("a plain reminder stays plain", fake.reminders[-1]["urgent"] is False)

        id1, id2, id3 = (r["id"] for r in fake.reminders)
        for r in fake.reminders:
            r["due_at"] = t0
        await scheduler.send_due_reminders(t0)
        check("all three go out once", len(sent) == 3)
        check("urgent ones are flagged and carry their id for the tap",
              [(s[3], s[4]) for s in sent] == [(True, id1), (True, id2), (False, None)])
        check("an urgent first send says it will come back", "again in 2 minutes" in sent[0][2])
        await scheduler.send_due_reminders(t0 + timedelta(minutes=1))
        check("not again before 2 minutes", len(sent) == 3)
        await scheduler.send_due_reminders(t0 + timedelta(minutes=2))
        check("2 minutes later the urgent ones repeat, numbered",
              len(sent) == 5 and sent[3][1].endswith("(2/3)") and sent[4][1].endswith("(2/3)"))
        await db.ack_reminders(1, id2)
        await scheduler.send_due_reminders(t0 + timedelta(minutes=4))
        check("a tap stops that one; the other makes its third and last send",
              len(sent) == 6 and sent[5][4] == id1 and sent[5][1].endswith("(3/3)") and "again" not in sent[5][2])
        await scheduler.send_due_reminders(t0 + timedelta(minutes=10))
        check("never more than 3 sends", len(sent) == 6 and all(r["done"] for r in fake.reminders))

        sent.clear()
        fake.reminders = []
        await tools.run_tool("set_reminder", {"text": "meds", "in_minutes": 1, "urgent": True}, 1)
        fake.reminders[-1]["due_at"] = t0
        await scheduler.send_due_reminders(t0)
        brain.client = FakeClient(reply_only("تمام"))
        await turn.handle_turn(member=ZAIN, text="ok ok", channel="telegram", thread="dm:1")
        await scheduler.send_due_reminders(t0 + timedelta(minutes=5))
        check("messaging Abdo counts as seeing it: no more repeats", len(sent) == 1 and fake.reminders[-1]["done"])
    finally:
        notify.deliver = real
        fake.reminders = []


def test_weather_hours():
    print("Scenario: hourly forecast strip")
    from app import briefing
    hours = [f"2026-10-13T{h:02d}:00" for h in range(24)] + [f"2026-10-14T{h:02d}:00" for h in range(24)]
    data = {"current": {"time": "2026-10-13T20:15", "temperature_2m": 9.6, "apparent_temperature": 7.0,
                        "weather_code": 0, "is_day": 0},
            "daily": {"temperature_2m_max": [14.0], "temperature_2m_min": [3.0], "precipitation_probability_max": [10]},
            "hourly": {"time": hours, "temperature_2m": [float(i % 24) for i in range(48)],
                       "weather_code": [61] * 48, "precipitation_probability": [40] * 48,
                       "is_day": [1 if 7 <= i % 24 < 19 else 0 for i in range(48)]}}
    w = briefing.parse_weather(data)
    check("12 hours, starting the hour after now, past midnight",
          [x["hour"] for x in w["hours"]][:5] == ["21:00", "22:00", "23:00", "00:00", "01:00"] and len(w["hours"]) == 12)
    check("each hour has temp, sky, night, rain", w["hours"][0] == {"hour": "21:00", "temp": 21, "kind": "rain", "night": True, "rain": 40})
    check("current night flag", w["night"] is True)
    del data["hourly"]
    check("no hourly data -> no strip, nothing invented", briefing.parse_weather(data)["hours"] == [])


async def test_briefing_build():
    print("Scenario: briefing lines appear only when there's something real to say")
    from app import briefing
    real_fetch = briefing._fetch_weather
    sample = {"current": {"temperature_2m": 3.2, "apparent_temperature": -1.4, "weather_code": 71},
              "daily": {"temperature_2m_max": [5.4], "temperature_2m_min": [-2.0],
                        "precipitation_probability_max": [60]}}

    async def good(lat, lng, tz):
        return sample

    async def bad(lat, lng, tz):
        raise RuntimeError("open-meteo down")

    global _fake_events
    now = datetime(2026, 10, 14, 8, 0, tzinfo=TZ)
    _fake_events = [{"id": "e1", "summary": "Suite dinner", "start": "2026-10-14T19:00:00-04:00"},
                    {"id": "e2", "summary": "Not today", "start": "2026-10-15T19:00:00-04:00"}]
    fake.packages = [{"id": 7, "description": "desk lamp", "carrier": None, "owner_id": 1,
                      "expected_on": None, "status": "arrived"}]
    fake.shopping = [{"id": 1, "item": "rice", "qty": None, "bought": False}]
    fake.statuses = {2: {"member_id": 2, "name": "Mo", "text": "away", "until_at": None,
                         "updated_at": now}}
    briefing._fetch_weather, briefing._weather_cache = good, {}
    try:
        title, body = await briefing.build(ZAIN, now)
        check("greets by Arabic name with a weekday morning phrase",
              title.startswith(("صباح الفل يا زين", "صباح الورد يا زين", "صحي النوم يا زين")))
        check("weather line from real numbers",
              "Weather: 3°, feels like -1°, light snow. High 5°, low -2°, 60% chance" in body)
        check("only today's events", "Today: Suite dinner at 19:00" in body and "Not today" not in body)
        check("my waiting package", "Waiting for you at the building: desk lamp" in body)
        check("grocery count", "Groceries: 1 item on the list" in body)
        check("roommates' statuses", "Mo: away" in body)
        check("remaining prayers", "Prayers: " in body and "Isha" in body)

        b = await briefing.gather(ZAIN, now)
        check("structured weather for the app card", b["weather"] == {
            "temp": 3, "feels": -1, "sky": "light snow", "kind": "snow", "high": 5, "low": -2, "rain": 60,
            "night": False, "hours": []})
        check("structured events/packages/groceries",
              b["events"] == [{"summary": "Suite dinner", "time": "19:00"}] and b["waiting"] == ["desk lamp"]
              and b["groceries"] == 1 and b["calendar_ok"] is True)
        G = briefing.greeting
        mornings = {"صباح الفل", "صباح الورد", "صحي النوم"}
        evenings = {"مساء الفل", "مساء العسل", "مسا مسا"}
        check("weekday morning / evening phrases (Wednesday)",
              G(now)["main"] in mornings and G(now.replace(hour=15))["main"] in evenings
              and G(now.replace(hour=2))["main"] in evenings)
        sat = now.replace(day=17)                    # Saturday 17 October 2026
        weekend_morning = {(G(sat, m)["main"], G(sat, m)["tail"]) for m in range(40)}
        check("weekend mornings use the weekend phrases, tail set apart",
              weekend_morning == {("صباح الورد والتجلي", "حسب التوقيت المحلي"), ("صباح الفل", "يا سيد الكل")})
        check("the sun-down line only after sunset",
              all(G(sat.replace(hour=14), m)["main"] == "مساء العسل" for m in range(20))
              and "الشمس نزلت" in {G(sat.replace(hour=20), m)["main"] for m in range(40)})
        check("same person, same day, same half: the same greeting",
              G(now, 1) == G(now.replace(hour=11, minute=59), 1))
        check("complete phrases stand alone; short ones take the name",
              briefing.greeting_text({"main": "مسا مسا", "tail": "", "named": False}, "زين") == "مسا مسا"
              and briefing.greeting_text({"main": "مساء الفل", "tail": "", "named": True}, "زين") == "مساء الفل يا زين"
              and briefing.greeting_text({"main": "مساء العسل", "tail": "والدلع والكسل", "named": False}, "زين")
              == "مساء العسل والدلع والكسل")
        check("structured greeting for the app",
              b["greeting_main"] in mornings and b["greeting_named"] is True and b["greeting"].endswith("يا زين"))

        briefing._fetch_weather, briefing._weather_cache = bad, {}
        _, body = await briefing.build(ZAIN, now)
        check("weather failure -> line omitted, not invented", "Weather" not in body)

        real_get = calendar_svc.get_events

        def degraded(days_ahead=7):
            raise calendar_svc.CalendarReadError("boundary")

        calendar_svc.get_events = degraded
        try:
            _, body = await briefing.build(ZAIN, now)
            check("degraded calendar says so (never 'nothing today')", "Couldn't check the calendar" in body)
        finally:
            calendar_svc.get_events = real_get
    finally:
        briefing._fetch_weather, briefing._weather_cache = real_fetch, {}
        fake.statuses = {}
        fake.packages, fake.shopping = [], []


def test_web_today():
    print("Scenario: web Today screen, status, prefs, reminder cancel rights")
    import re
    from fastapi.testclient import TestClient
    from app import main as app_main
    settings.session_pepper = "test-pepper"
    dms = []

    async def capture(chat_id, text, disable_notification=False):
        dms.append(text)

    real_send = telegram.send_message
    telegram.send_message = capture
    H = {"X-Abdo": "1"}
    try:
        c = TestClient(app_main.app, base_url="https://testserver")
        c.post("/api/auth/request-code", json={"name": "Zain"}, headers=H)
        code = re.search(r"\b(\d{6})\b", dms[-1]).group(1)
        c.post("/api/auth/verify", json={"name": "Zain", "code": code}, headers=H)

        fake.reminders.clear()
        fake.statuses = {}
        r1 = asyncio.run(fake.add_reminder(1, "laundry", datetime.now(timezone.utc) + timedelta(hours=1), 1))
        r2 = asyncio.run(fake.add_reminder(2, "Mo's thing", datetime.now(timezone.utc) + timedelta(hours=1), 2))
        t = c.get("/api/today").json()
        check("today: prayers with a next one", t["prayers"] and len(t["prayers"]["times"]) == 6)
        check("today: only my reminders", [x["text"] for x in t["reminders"]] == ["laundry"])

        r = c.put("/api/status", json={"text": "studying", "until_time": "23:58"}, headers=H)
        check("set my status", r.status_code == 200 and fake.statuses[1]["text"] == "studying")
        check("status shows on Today", any(s["name"] == "Zain" for s in c.get("/api/today").json()["statuses"]))
        r = c.put("/api/status", json={"text": "x", "until_day": "someday"}, headers=H)
        check("bad status end -> 400", r.status_code == 400)
        c.delete("/api/status", headers=H)
        check("clear my status", 1 not in fake.statuses)

        check("cancel someone else's reminder -> 404", c.delete(f"/api/reminders/{r2}", headers=H).status_code == 404)
        check("cancel mine", c.delete(f"/api/reminders/{r1}", headers=H).status_code == 200)

        from app import briefing as _briefing
        real_fetch = _briefing._fetch_weather

        async def no_weather(lat, lng, tz):
            raise RuntimeError("offline")

        _briefing._fetch_weather, _briefing._weather_cache = no_weather, {}
        try:
            b = c.get("/api/briefing").json()
            check("briefing endpoint: live data for the logged-in member",
                  b["name"] == "زين" and "events" in b and "prayers" in b and b["weather"] is None)
        finally:
            _briefing._fetch_weather, _briefing._weather_cache = real_fetch, {}
        check("briefing needs a session", TestClient(app_main.app, base_url="https://testserver")
              .get("/api/briefing").status_code == 401)

        check("prefs default", c.get("/api/me/prefs").json()["prefs"]["briefing_time"] == "08:00")
        r = c.patch("/api/me/prefs", json={"briefing_time": "07:30", "prayers": False}, headers=H)
        check("prefs update", r.json()["prefs"] == {"briefing": True, "briefing_time": "07:30", "prayers": False})
        check("invalid time -> 422", c.patch("/api/me/prefs", json={"briefing_time": "25:00"}, headers=H).status_code == 422)
        check("empty update -> 400", c.patch("/api/me/prefs", json={}, headers=H).status_code == 400)
    finally:
        telegram.send_message = real_send
        settings.session_pepper = None
        MEMBERS[1].pop("prefs", None)


# =============================================================================
# Phase 7b.1: money + travel mode
# =============================================================================

def test_money_math():
    print("Scenario: money math is exact (cents), balances sum to zero, settle-up is minimal")
    from app import money
    pa, ME = money.parse_amount, money.MoneyError

    def bad(raw):
        try:
            pa(raw)
            return False
        except ME:
            return True

    check("parses 42, 42.5, $42.50, 1,200", pa("42") == 4200 and pa("42.5") == 4250
          and pa("$42.50") == 4250 and pa("1,200") == 120000)
    check("rejects 0, negative, sub-cent, words, absurd",
          all(bad(x) for x in ("0", "-5", "1.005", "lots", "99999")))
    for amount, n in ((100, 3), (1, 3), (10000, 3), (4250, 4), (7, 2)):
        sh = money.split(amount, list(range(1, n + 1)))
        if sum(sh.values()) != amount or max(sh.values()) - min(sh.values()) > 1:
            check(f"split {amount}c / {n} exact and fair", False)
            break
    else:
        check("splits always sum exactly and differ by at most 1 cent", True)
    check("$100 / 3 = 33.34 + 33.33 + 33.33", money.split(10000, [3, 1, 2]) == {1: 3334, 2: 3333, 3: 3333})

    exps = [{"paid_by": 1, "amount_cents": 4200}, {"paid_by": 2, "amount_cents": 1000}]
    shares = [{"member_id": m, "share_cents": c} for m, c in money.split(4200, [1, 2, 3, 4]).items()] + \
             [{"member_id": m, "share_cents": c} for m, c in money.split(1000, [2, 3]).items()]
    sets = [{"from_member": 4, "to_member": 1, "amount_cents": 500}]
    net = money.balances([1, 2, 3, 4], exps, shares, sets)
    check("balances sum to zero", sum(net.values()) == 0)
    check("payer is owed, payback counted", net[1] == 4200 - 1050 - 500 and net[4] == -1050 + 500)
    plan = money.settle_up(net)
    after = dict(net)
    for f, t, c in plan:
        after[f] += c
        after[t] -= c
    check("settle-up zeroes everyone in at most n-1 transfers",
          all(v == 0 for v in after.values()) and len(plan) <= 3)
    check("nothing owed -> no transfers", money.settle_up({1: 0, 2: 0}) == [])
    check("format", money.fmt(1050) == "$10.50" and money.fmt(-5) == "$0.05" and money.fmt(123456) == "$1,234.56")


async def test_money_tools():
    print("Scenario: money tools (default everyone, split_with, exclude, payments, delete rights)")
    fake.expenses.clear()
    fake.settlements.clear()
    out = await tools.run_tool("add_expense", {"description": "groceries", "amount": "42"}, member_id=1)
    e = fake.expenses[-1]
    check("default split: everyone, payer = speaker",
          e["paid_by"] == 1 and set(e["shares"]) == {1, 2, 3} and sum(e["shares"].values()) == 4200)
    check("result reads the shares back", "Zain $14.00" in out and "Mo $14.00" in out)
    await tools.run_tool("add_expense", {"description": "pizza", "amount": "20", "paid_by": "Mo",
                                         "split_with": ["Mo", "Zain"]}, member_id=1)
    check("split_with + paid_by", fake.expenses[-1]["paid_by"] == 2 and fake.expenses[-1]["shares"] == {1: 1000, 2: 1000})
    await tools.run_tool("add_expense", {"description": "cleaning", "amount": "9", "exclude": ["Sam"]}, member_id=1)
    check("exclude narrows everyone", set(fake.expenses[-1]["shares"]) == {1, 2})
    out = await tools.run_tool("add_expense", {"description": "x", "amount": "ten"}, member_id=1)
    check("bad amount -> honest error, nothing saved", out.startswith("ERROR") and len(fake.expenses) == 3)
    out = await tools.run_tool("add_expense", {"description": "x", "amount": "5", "split_with": ["Bob"]}, member_id=1)
    check("unknown name -> honest error", out.startswith("ERROR") and len(fake.expenses) == 3)

    out = await tools.run_tool("get_balances", {}, member_id=1)
    # Zain paid 42 + 9, owes 14 + 10 + 4.50 -> +22.50; Mo paid 20, owes 28.50 -> -8.50; Sam -14.
    check("balances", "Zain: is owed $22.50" in out and "Mo: owes $8.50" in out and "Sam: owes $14.00" in out)
    check("settle-up plan", "To settle up: Sam pays Zain $14.00; Mo pays Zain $8.50" in out)
    out = await tools.run_tool("record_payment", {"to": "Zain", "amount": "8.50"}, member_id=2)
    check("payment recorded from the speaker", out.startswith("Recorded payment") and fake.settlements[-1]["from_member"] == 2)
    out = await tools.run_tool("record_payment", {"to": "Mo", "amount": "5"}, member_id=2)
    check("paying yourself -> error", out.startswith("ERROR"))
    out = await tools.run_tool("get_balances", {}, member_id=1)
    check("payment moves the balances", "Mo: all square" in out)

    out = await tools.run_tool("delete_expense", {"expense_id": fake.expenses[0]["id"]}, member_id=3)
    check("can't delete an expense you neither paid nor recorded", out.startswith("ERROR"))
    out = await tools.run_tool("delete_expense", {"expense_id": fake.expenses[0]["id"]}, member_id=1)
    check("payer can delete", out.startswith("Deleted") and fake.expenses[0]["deleted"])
    out = await tools.run_tool("list_expenses", {}, member_id=1)
    check("deleted expense gone from the list", "groceries" not in out and "pizza" in out)
    check("money persona: confirm once, no arithmetic", "Never do the arithmetic yourself" in prompts.STATIC_PERSONA)


async def test_travel_mode():
    print("Scenario: travel mode (geocode, prayers/briefing follow the place, expiry returns home)")
    from app import location, prayer, scheduler, notify, briefing
    real_search = location._search

    async def search(name):
        if name.lower().startswith("montreal"):
            return {"results": [{"name": "Montreal", "country": "Canada", "latitude": 45.50884,
                                 "longitude": -73.58781, "timezone": "America/Toronto"}]}
        return {}

    location._search = search
    fake.statuses = {}
    try:
        out = await tools.run_tool("set_status", {"text": "away", "place": "Montreal",
                                                  "until_day": "Sunday"}, member_id=1)
        check("place geocoded and confirmed", "Travel mode: Montreal, Canada" in out
              and fake.statuses[1]["place_lat"] == 45.50884)
        out = await tools.run_tool("set_status", {"text": "away", "place": "Atlantis"}, member_id=2)
        check("unknown place -> honest error, no status", out.startswith("ERROR") and 2 not in fake.statuses)

        loc = await location.location_for(1)
        check("traveller's location is the place", loc.label == "Montreal, Canada" and not loc.is_home)
        check("others stay home", (await location.location_for(2)).is_home)
        out = await tools.run_tool("get_prayer_times", {}, member_id=1)
        check("prayer tool uses the traveller's place", "in Montreal, Canada" in out)

        # Scheduler: at Montreal's Asr (earlier than Waterloo's), only the traveller gets it.
        sent = []

        async def deliver(member, title, body, url="/", silent=False, urgent=False, ack=None):
            sent.append((member["id"], title, body))
            return "push"

        real_deliver = notify.deliver
        notify.deliver = deliver
        for m in MEMBERS.values():
            m["prefs"] = {"briefing": False}
        fake.sent_log.clear()
        try:
            day = datetime.now(TZ).date()
            mtl = prayer.times_at(day, loc)
            home = prayer.times_for(day)
            check("Montreal's Asr is ~25 min before Waterloo's",
                  timedelta(minutes=15) < home["asr"] - mtl["asr"] < timedelta(minutes=35))
            await scheduler.tick(mtl["asr"] + timedelta(minutes=2))
            asr = [(m, b) for m, t, b in sent if t.startswith("Asr")]
            check("traveller gets Asr at the place's time, labelled", asr == [(1, f"It's Asr time in Montreal, Canada: {prayer.clock12(mtl['asr'])}")])
            sent.clear()
            await scheduler.tick(home["asr"] + timedelta(minutes=2))
            check("home members get Asr at home time; traveller not twice",
                  sorted(m for m, t, _ in sent if t.startswith("Asr")) == [2, 3])
        finally:
            notify.deliver = real_deliver
            for m in MEMBERS.values():
                m.pop("prefs", None)

        # Briefing: weather fetched for the place, travel note in the text.
        fetched = []

        async def weather(lat, lng, tz):
            fetched.append((lat, lng))
            return {"current": {"temperature_2m": 5, "apparent_temperature": 2, "weather_code": 3},
                    "daily": {"temperature_2m_max": [8], "temperature_2m_min": [1],
                              "precipitation_probability_max": [10]}}

        real_fetch = briefing._fetch_weather
        briefing._fetch_weather, briefing._weather_cache = weather, {}
        try:
            b = await briefing.gather(ZAIN)
            check("briefing weather for the place", fetched == [(45.50884, -73.58781)] and b["place"] == "Montreal, Canada")
            check("briefing text says so", "In Montreal, Canada (travel mode)" in briefing.render_text(b)[1])
        finally:
            briefing._fetch_weather, briefing._weather_cache = real_fetch, {}

        fake.statuses[1]["until_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        check("expired travel status -> back home", (await location.location_for(1)).is_home)
        brain.client = FakeClient(reply_only("ok"))
        fake.statuses[1]["until_at"] = None
        await brain.think(MO, "dm:2", "where's Zain?")
        check("Abdo's context shows the place", "Zain: away (in Montreal, Canada)" in brain.client.messages.requests[0]["system"][1]["text"])
    finally:
        location._search = real_search
        fake.statuses = {}


def test_web_money():
    print("Scenario: web money (overview, add with split, settle, delete rights, validation)")
    import re
    from fastapi.testclient import TestClient
    from app import main as app_main
    settings.session_pepper = "test-pepper"
    dms = []

    async def capture(chat_id, text, disable_notification=False):
        dms.append(text)

    real_send = telegram.send_message
    telegram.send_message = capture
    H = {"X-Abdo": "1"}
    fake.expenses.clear()
    fake.settlements.clear()
    fake.codes.clear()       # an earlier test deliberately rate-limited Mo's login codes
    try:
        c = TestClient(app_main.app, base_url="https://testserver")
        c.post("/api/auth/request-code", json={"name": "Mo"}, headers=H)
        c.post("/api/auth/verify", json={"name": "Mo", "code": re.search(r"\b(\d{6})\b", dms[-1]).group(1)}, headers=H)

        r = c.post("/api/expenses", json={"description": "Walmart run", "amount": "30"}, headers=H)
        check("add expense: everyone, payer = me", r.status_code == 200 and fake.expenses[-1]["paid_by"] == 2
              and fake.expenses[-1]["shares"] == {1: 1000, 2: 1000, 3: 1000})
        r = c.post("/api/expenses", json={"description": "x", "amount": "10", "split_with": [1, 2]}, headers=H)
        check("add expense with a chosen split", fake.expenses[-1]["shares"] == {1: 500, 2: 500})
        check("bad amount -> 400", c.post("/api/expenses", json={"description": "x", "amount": "0"}, headers=H).status_code == 400)
        check("unknown roommate -> 400", c.post("/api/expenses", json={"description": "x", "amount": "5",
                                                                      "split_with": [99]}, headers=H).status_code == 400)
        m = c.get("/api/money").json()
        me = next(b for b in m["balances"] if b["member_id"] == 2)
        check("overview: my net, transfers, expenses", me["net_cents"] == 4000 - 1500 and m["transfers"]
              and m["expenses"][0]["can_delete"] is True)
        r = c.post("/api/settlements", json={"to_member": 2, "amount": "15", "from_member": 1}, headers=H)
        check("record a payment I received", r.status_code == 200)
        r = c.post("/api/settlements", json={"to_member": 1, "amount": "5", "from_member": 3}, headers=H)
        check("can't record a payment between two other people", r.status_code == 403)
        check("can't delete someone else's expense",
              asyncio.run(fake.add_expense("theirs", 100, 3, 3, date.today(), {3: 100})) and
              c.delete(f"/api/expenses/{fake.expenses[-1]['id']}", headers=H).status_code == 404)
        check("delete my expense", c.delete(f"/api/expenses/{fake.expenses[0]['id']}", headers=H).status_code == 200)
        check("money needs a session", TestClient(app_main.app, base_url="https://testserver")
              .get("/api/money").status_code == 401)
    finally:
        telegram.send_message = real_send
        settings.session_pepper = None
        fake.expenses.clear()
        fake.settlements.clear()


# =============================================================================
# Phase 7b.2: timetables + exams
# =============================================================================

from datetime import time as dtime  # noqa: E402

QUEST_ITEMS = [
    {"course": "math 136", "section": "001", "kind": "LEC", "days": ["Mon", "Wed", "Fri"], "start": "10:30",
     "end": "11:20", "room": "MC 2065", "start_date": "2026-09-09", "end_date": "2026-12-08"},
    {"course": "MATH 136", "section": "102", "kind": "TUT", "days": ["Fri"], "start": "14:30",
     "end": "15:20", "room": "MC 4021", "start_date": "", "end_date": ""},
]


def test_timetable_validate():
    print("Scenario: timetable rows are validated in code (whatever the model or browser sent)")
    from app import timetable
    rows, problems = timetable.validate(QUEST_ITEMS, date(2026, 9, 9), date(2026, 12, 8))
    check("one row per meeting day", len(rows) == 4 and [r["weekday"] for r in rows] == [0, 2, 4, 4])
    check("course normalized, times parsed", rows[0]["course"] == "MATH 136" and rows[0]["starts"] == dtime(10, 30))
    check("missing term dates filled from the term", rows[3]["term_start"] == date(2026, 9, 9) and not problems)
    _, problems = timetable.validate(QUEST_ITEMS)
    check("no term dates at all -> a clear problem", problems == ["MATH 136: no term dates (set them above)"])
    bad = [{**QUEST_ITEMS[0], "end": "09:00"}, {**QUEST_ITEMS[0], "days": []}, {**QUEST_ITEMS[0], "start": "soon"}]
    _, problems = timetable.validate(bad)
    check("bad rows rejected with reasons", len(problems) == 3 and "ends before it starts" in problems[0])
    check("term guess = most common dates", timetable.term_guess(QUEST_ITEMS) == ("2026-09-09", "2026-12-08"))

    tst = {"course": "CS 245", "section": "201", "kind": "TST", "days": ["Thu"], "start": "16:30", "end": "18:20",
           "room": "", "start_date": "2026-10-29", "end_date": "2026-10-29"}
    one_day = {**QUEST_ITEMS[0], "kind": "LEC", "start_date": "2026-11-02", "end_date": "2026-11-02"}
    weekly, once = timetable.split_one_offs(QUEST_ITEMS + [tst, one_day])
    check("TST slots and one-day rows are one-offs, not weekly", len(weekly) == 2 and once == [tst, one_day])
    rows, problems = timetable.validate(QUEST_ITEMS + [tst], date(2026, 9, 9), date(2026, 12, 8))
    check("validate never saves a TST as a weekly class", len(rows) == 4 and not problems)
    swapped = [{**QUEST_ITEMS[0], "start_date": "2026-09-09", "end_date": "2026-08-12"}]
    _, problems = timetable.validate(swapped)
    check("a day/month mix-up is caught, not saved", problems and "day/month order" in problems[0])
    too_long = [{**QUEST_ITEMS[0], "start_date": "2026-09-09", "end_date": "2027-08-12"}]
    _, problems = timetable.validate(too_long)
    check("an implausible term length is caught", problems and "looks wrong" in problems[0])

    tz = TZ
    d = date(2026, 10, 14)
    w = (datetime.combine(d, dtime(8), tz), datetime.combine(d, dtime(23), tz))
    busy = [(datetime.combine(d, dtime(10), tz), datetime.combine(d, dtime(11), tz)),
            (datetime.combine(d, dtime(10, 30), tz), datetime.combine(d, dtime(12), tz)),
            (datetime.combine(d, dtime(12, 15), tz), datetime.combine(d, dtime(13), tz))]
    free = [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in timetable.free_windows(w, busy)]
    check("free windows merge overlaps and drop gaps under 30 min",
          free == [("08:00", "10:00"), ("13:00", "23:00")])


def test_quest_parser():
    print("Scenario: deterministic Quest list-view parser (fixtures mirror real copies)")
    from app import quest, timetable
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    r = quest.parse(open(os.path.join(fx, "quest_list_view.txt")).read())
    weekly = [(c["course"], c["kind"], ",".join(c["days"]), c["start"], c["end"], c["room"]) for c in r["classes"]]
    check("weekly classes read exactly (fields on separate lines)", weekly == [
        ("ENGL 109", "LEC", "Mon,Wed", "14:30", "16:20", "HH 1101"),
        ("MATH 135", "LEC", "Tue,Thu", "13:00", "14:20", "MC 2017"),
        ("MATH 135", "TUT", "Fri", "12:30", "13:20", "MC 4045"),
        ("PHYS 121", "TUT", "Wed", "10:30", "11:20", "PHY 145"),
        ("PHYS 121", "LEC", "Tue,Thu", "10:00", "11:20", "STC 0050"),
        ("STAT 230", "TUT", "Fri", "15:30", "16:20", "AL 116"),
        ("STAT 230", "LEC", "Mon,Wed,Fri", "11:30", "12:20", "DC 1350")])
    check("DD/MM dates: term Sep 9 to Dec 8", (r["term_start"], r["term_end"]) == ("2026-09-09", "2026-12-08"))
    exams = [(e["course"], e["date"], e["start"], e["end"]) for e in r["exams"]]
    check("TST slots become proposed exams, incl. a continuation row's second date", exams == [
        ("STAT 230", "2026-10-08", "16:30", "18:20"), ("MATH 135", "2026-10-29", "16:30", "18:20"),
        ("PHYS 121", "2026-11-10", "16:30", "18:20"), ("STAT 230", "2026-11-19", "16:30", "18:20")])
    check("no TST ever becomes a weekly class", all(c["kind"] != "TST" for c in r["classes"]))
    check("online/TBA and dropped courses are skipped with reasons",
          r["skipped"] == ["ECON 101 LEC: online, no weekly slot", "HIST 110 LEC: dropped"])
    rows, problems = timetable.validate(r["classes"])
    check("parser output passes validation as-is", not problems and len(rows) == 12)

    d = quest.parse(open(os.path.join(fx, "quest_list_view_desktop.txt")).read())
    check("desktop tab-row copy, AM/PM times",
          [(c["kind"], ",".join(c["days"]), c["start"], c["end"]) for c in d["classes"]]
          == [("LEC", "Mon,Wed,Fri", "08:30", "09:20"), ("TUT", "Thu", "14:30", "15:20")])
    check("MM/DD dates detected (a day over 12)", (d["term_start"], d["term_end"]) == ("2027-01-06", "2027-04-05")
          and d["exams"][0]["date"] == "2027-02-24" and d["exams"][0]["start"] == "19:00")
    for junk in ("", "hello there", "Weekly Calendar View\nMonday Tuesday"):
        try:
            quest.parse(junk)
            check(f"non-Quest text rejected ({junk[:12]!r})", False)
        except quest.QuestFormatError:
            check(f"non-Quest text rejected ({junk[:12]!r})", True)


async def test_who_is_free_and_exams():
    print("Scenario: who's free (classes, exams, travel), exam tools, briefing, Abdo's context")
    from app import timetable, briefing
    fake.classes, fake.exams, fake.statuses = [], [], {}
    wed = date(2026, 10, 14)                     # a Wednesday, inside the term
    rows, _ = timetable.validate(QUEST_ITEMS, date(2026, 9, 9), date(2026, 12, 8))
    await fake.replace_classes(1, rows)          # Zain: MATH 136 Mon/Wed/Fri 10:30-11:20
    await fake.replace_classes(2, [{"course": "CS 246", "section": None, "kind": "LAB", "weekday": 2,
                                     "starts": dtime(18), "ends": dtime(19, 30), "room": None,
                                     "term_start": date(2026, 9, 9), "term_end": date(2026, 12, 8)}])
    await fake.add_exam(1, "MATH 136", "midterm", datetime(2026, 10, 14, 19, 0, tzinfo=TZ), None, None, 1)
    f = await timetable.who_is_free(wed, dtime(17), dtime(23), now=datetime(2026, 10, 14, 9, 0, tzinfo=TZ))
    check("together = window minus everyone's classes/exams",
          f["together"] == [{"from": "17:00", "to": "18:00"}, {"from": "21:00", "to": "23:00"}])
    sam = next(p for p in f["people"] if p["name"] == "Sam")
    check("no timetable flagged, not assumed busy", sam["has_timetable"] is False and sam["busy"] == [])
    check("exam blocks 2 hours without an end time",
          any(b["what"] == "MATH 136 midterm" and b["to"] == "21:00" for b in f["people"][0]["busy"]))
    sat = await timetable.who_is_free(date(2026, 12, 19), dtime(9), dtime(12))
    check("after the term ends, classes don't count", sat["together"] == [{"from": "09:00", "to": "12:00"}])

    fake.statuses = {2: {"member_id": 2, "name": "Mo", "text": "away", "until_at": None, "updated_at": None,
                         "place": "Montreal, Canada", "place_lat": 45.5, "place_lng": -73.6, "place_tz": "America/Toronto"}}
    f = await timetable.who_is_free(wed, dtime(17), dtime(23), now=datetime(2026, 10, 14, 9, 0, tzinfo=TZ))
    check("someone away isn't counted against 'together', and is named",
          {"from": "17:00", "to": "19:00"} in f["together"] and "away (in Montreal, Canada)" in timetable.describe_free(f))
    fake.statuses = {}

    out = await tools.run_tool("pause_classes", {"from_day": "2026-10-12", "to_day": "2026-10-16",
                                                 "label": "Reading week"}, member_id=1)
    check("pause_classes: a break for the speaker", out.startswith("Break #") and "Reading week" in out)
    f = await timetable.who_is_free(wed, dtime(17), dtime(23), now=datetime(2026, 10, 14, 9, 0, tzinfo=TZ))
    zain = next(p for p in f["people"] if p["name"] == "Zain")
    check("during a break, that member's classes don't count, but exams still do",
          not any("LEC" in b["what"] for b in zain["busy"]) and any("midterm" in b["what"] for b in zain["busy"]))
    out = await tools.run_tool("get_schedule", {"day": "2026-10-16"}, member_id=1)
    check("get_schedule says the day is off", "No classes on Friday 16 October (Reading week)" in out)
    out = await tools.run_tool("get_schedule", {"name": "Mo", "day": "2026-10-14"}, member_id=1)
    check("someone else's classes are untouched", "CS 246 LAB" in out)
    check("a break can't end before it starts",
          (await tools.run_tool("pause_classes", {"from_day": "2026-10-16", "to_day": "2026-10-12"}, member_id=1)).startswith("ERROR"))
    bid = fake.breaks[0]["id"]
    check("only your own break can be removed",
          (await tools.run_tool("resume_classes", {"break_id": bid}, member_id=2)).startswith("ERROR"))
    out = await tools.run_tool("resume_classes", {"break_id": bid}, member_id=1)
    check("resume_classes brings them back", out.startswith("Removed break") and not fake.breaks)
    fake.statuses = {}

    out = await tools.run_tool("add_exam", {"course": "cs 246", "kind": "midterm", "day": "tomorrow",
                                            "time": "19:00", "end_time": "21:00", "room": "M3 1006"}, member_id=2)
    check("add exam resolves the day in code", out.startswith("Added exam #") and "CS 246 midterm" in out)
    out = await tools.run_tool("add_exam", {"course": "x", "kind": "assignment", "day": "today", "time": "10:00"}, member_id=2)
    check("assignments aren't exams", out.startswith("ERROR"))
    out = await tools.run_tool("list_exams", {}, member_id=1)
    check("exams shared with everyone", "Mo: CS 246 midterm" in out)
    eid = fake.exams[-1]["id"]
    out = await tools.run_tool("delete_exam", {"exam_id": eid}, member_id=3)
    check("can't remove someone else's exam", out.startswith("ERROR"))
    out = await tools.run_tool("get_schedule", {"name": "Zain", "day": "2026-10-14"}, member_id=2)
    check("get_schedule lists classes + exams in order", "10:30–11:20 MATH 136 LEC" in out and "19:00–21:00 MATH 136 midterm" in out)
    out = await tools.run_tool("who_is_free", {"day": "2026-10-14", "from_time": "17:00"}, member_id=1)
    check("who_is_free tool text", out.startswith("Free together on Wednesday 14 October"))

    brain.client = FakeClient(reply_only("ok"))
    await brain.think(ZAIN, "dm:1", "anything tomorrow?")
    ctx = brain.client.messages.requests[0]["system"][1]["text"]
    check("tomorrow's exam reaches Abdo's context",
          re.search(r"Exams coming up: [^\n]*Mo: CS 246 midterm tomorrow 19:00", ctx) is not None)

    real_fetch = briefing._fetch_weather

    async def no_weather(lat, lng, tz):
        raise RuntimeError("offline")

    briefing._fetch_weather, briefing._weather_cache = no_weather, {}
    try:
        b = await briefing.gather(ZAIN, datetime(2026, 10, 14, 8, 0, tzinfo=TZ))
        check("briefing: my classes today", b["classes"] == [{"what": "MATH 136 LEC", "from": "10:30", "to": "11:20"}])
        check("briefing: my exam with a countdown", b["my_exams"][0]["when"] == "today")
        text = briefing.render_text(b)[1]
        check("briefing text has classes + exam", "Classes: MATH 136 LEC 10:30" in text and "Your MATH 136 midterm: today" in text)
    finally:
        briefing._fetch_weather, briefing._weather_cache = real_fetch, {}
    fake.classes, fake.exams = [], []


def test_web_schedule():
    print("Scenario: web schedule import (extract -> review -> save) and exams")
    import re
    import json as _json
    from fastapi.testclient import TestClient
    from app import main as app_main
    settings.session_pepper = "test-pepper"
    dms = []

    async def capture(chat_id, text, disable_notification=False):
        dms.append(text)

    real_send = telegram.send_message
    telegram.send_message = capture
    H = {"X-Abdo": "1"}
    fake.codes.clear()
    fake.classes, fake.exams = [], []
    try:
        c = TestClient(app_main.app, base_url="https://testserver")
        c.post("/api/auth/request-code", json={"name": "Zain"}, headers=H)
        c.post("/api/auth/verify", json={"name": "Zain", "code": re.search(r"\b(\d{6})\b", dms[-1]).group(1)}, headers=H)

        fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "quest_list_view.txt")
        r = c.post("/api/schedule/parse", json={"text": open(fx).read()}, headers=H)
        j = r.json()
        check("parse proposes classes + test slots, saves nothing",
              r.status_code == 200 and len(j["classes"]) == 7 and len(j["exams"]) == 4 and fake.classes == [])
        check("non-Quest text -> 400 with how-to", c.post("/api/schedule/parse", json={"text": "my classes are MWF"},
                                                          headers=H).status_code == 400)
        bad = [{**j["classes"][0], "start_date": "", "end_date": ""}]
        r = c.put("/api/schedule", json={"classes": bad}, headers=H)
        check("save without term dates for every row -> 400 with the reason", r.status_code == 400 and "term dates" in r.text)
        r = c.put("/api/schedule", json={"classes": j["classes"], "exams": j["exams"][:2]}, headers=H)
        check("save classes + the chosen test slots as exams",
              r.json() == {"saved": 12, "exams_added": 2} and len(fake.exams) == 2 and fake.exams[0]["kind"] == "midterm")
        r = c.put("/api/schedule", json={"classes": j["classes"], "exams": j["exams"]}, headers=H)
        check("re-import doesn't duplicate exams", r.json()["exams_added"] == 2 and len(fake.exams) == 4)
        got = c.get("/api/schedule").json()["classes"]
        check("schedule read back per meeting day", len(got) == 12 and got[0]["day"] == "Mon")

        r = c.post("/api/exams", json={"course": "math 136", "kind": "midterm", "date": "2026-10-20", "time": "19:00"}, headers=H)
        check("add exam", r.status_code == 200 and fake.exams[-1]["course"] == "MATH 136")
        check("bad kind -> 400", c.post("/api/exams", json={"course": "x", "kind": "essay", "date": "2026-10-20",
                                                            "time": "19:00"}, headers=H).status_code == 400)
        check("bad time -> 422", c.post("/api/exams", json={"course": "x", "kind": "quiz", "date": "2026-10-20",
                                                            "time": "7pm"}, headers=H).status_code == 422)
        t = c.get("/api/today").json()
        check("today includes free-together data", "together" in t["free"] and "no_timetable" in t["free"])

        r = c.post("/api/schedule/breaks", json={"starts_on": "2099-10-12", "ends_on": "2099-10-16", "label": "Reading week"}, headers=H)
        check("add a class break from the app", r.status_code == 200)
        brk = c.get("/api/schedule").json()["breaks"]
        check("breaks come back with the schedule", [b["label"] for b in brk] == ["Reading week"])
        check("a backwards break is refused",
              c.post("/api/schedule/breaks", json={"starts_on": "2099-10-16", "ends_on": "2099-10-12"}, headers=H).status_code == 400)
        check("remove it", c.delete(f"/api/schedule/breaks/{brk[0]['id']}", headers=H).status_code == 200
              and c.get("/api/schedule").json()["breaks"] == [])
    finally:
        telegram.send_message = real_send
        settings.session_pepper = None
        fake.classes, fake.exams = [], []
    

# =============================================================================
# Phase 7b.3: fridge, going shopping, polls
# =============================================================================

async def test_fridge_shopping_polls():
    print("Scenario: fridge + expiry, going shopping pings, polls (vote by name/number, close)")
    from app import pantry, polls, notify, briefing
    fake.pantry, fake.polls, fake.votes = [], [], []
    today = datetime.now(TZ).date()
    e = pantry.expiry
    check("expiry labels", [e(today, today)["label"], e(today + timedelta(days=1), today)["label"],
                            e(today + timedelta(days=5), today)["label"], e(today - timedelta(days=2), today)["label"]]
          == ["today", "tomorrow", "in 5 days", "expired 2 days ago"])

    out = await tools.run_tool("add_to_fridge", {"item": "milk", "expires_day": "tomorrow"}, member_id=1)
    check("shared item with expiry resolved in code",
          out.startswith("Added #") and fake.pantry[-1]["expires_on"] == today + timedelta(days=1) and fake.pantry[-1]["owner_id"] is None)
    await tools.run_tool("add_to_fridge", {"item": "yogurt", "expires_in_days": 0, "mine": True}, member_id=2)
    await tools.run_tool("add_to_fridge", {"item": "rice"}, member_id=1)
    out = await tools.run_tool("add_to_fridge", {"item": "x", "expires_day": "someday"}, member_id=1)
    check("bad expiry -> honest error", out.startswith("ERROR"))
    out = await tools.run_tool("list_fridge", {}, member_id=1)
    check("fridge listed soonest first, with owners", out.index("yogurt") < out.index("milk") < out.index("rice")
          and "(Mo's)" in out and "(shared)" in out)
    fake.shopping = []
    out = await tools.run_tool("fridge_item_done", {"item_id": fake.pantry[0]["id"], "rebuy": True}, member_id=1)
    check("finished + rebuy -> on the shopping list", "Added it to the shopping list" in out and fake.shopping[0]["item"] == "milk")
    out = await tools.run_tool("fridge_item_done", {"item_id": 999}, member_id=1)
    check("unknown fridge id -> honest error", out.startswith("ERROR"))

    sent = []

    async def deliver(member, title, body, url="/", silent=False, urgent=False, ack=None):
        sent.append((member["id"], title))
        return "push"

    real = notify.deliver
    notify.deliver = deliver
    pantry._last_going.clear()
    try:
        out = await tools.run_tool("going_shopping", {"store": "Walmart"}, member_id=1)
        check("going shopping pings everyone else (not me)", sorted(m for m, _ in sent) == [2, 3]
              and "Zain is going to Walmart" in sent[0][1] and "Current list: milk" in out)
        sent.clear()
        out = await tools.run_tool("going_shopping", {}, member_id=1)
        check("no second ping within half an hour", sent == [] and "not pinging" in out)

        sent.clear()
        out = await tools.run_tool("create_poll", {"question": "Where are we eating?",
                                                   "options": ["Shawarma", "Pizza", "Sushi", "pizza"]}, member_id=1)
        check("poll created (duplicate option dropped), others notified",
              out.startswith("Poll #") and fake.polls[-1]["options"] == ["Shawarma", "Pizza", "Sushi"]
              and sorted(m for m, _ in sent) == [2, 3])
        out = await tools.run_tool("create_poll", {"question": "x", "options": ["only one"]}, member_id=1)
        check("a poll needs 2+ options", out.startswith("ERROR"))
        await tools.run_tool("vote", {"option": "shawarma"}, member_id=1)
        await tools.run_tool("vote", {"option": "2"}, member_id=2)
        out = await tools.run_tool("vote", {"option": "sush"}, member_id=3)
        check("vote by name, number, and partial match", out.startswith("Voted Sushi"))
        out = await tools.run_tool("vote", {"option": "burgers"}, member_id=3)
        check("not an option -> lists the real ones", out.startswith("ERROR") and "Shawarma" in out)
        await tools.run_tool("vote", {"option": "Shawarma"}, member_id=3)
        out = await tools.run_tool("poll_results", {}, member_id=2)
        check("changed vote counted once; tally correct", "Shawarma 2 (Zain, Sam)" in out and "Pizza 1 (Mo)" in out
              and "everyone voted" in out)
        out = await tools.run_tool("close_poll", {}, member_id=2)
        check("only the asker can close", out.startswith("ERROR"))
        out = await tools.run_tool("vote", {"remove": True}, member_id=3)
        check("a vote can be taken back", out.startswith("Removed") and not any(v["member_id"] == 3 for v in fake.votes))
        out = await tools.run_tool("vote", {"remove": True}, member_id=3)
        check("removing a vote you don't have is honest", "hadn't voted" in out)
        await tools.run_tool("vote", {"option": "Shawarma"}, member_id=3)
        out = await tools.run_tool("close_poll", {}, member_id=1)
        check("closing announces the winner", "Winner: Shawarma" in out)
        fake.polls[-1]["closed_at"] = datetime.now(timezone.utc) - timedelta(hours=25)
        check("closed polls drop off after a day", "Where are we eating?" not in await tools.run_tool("poll_results", {}, member_id=1))
        await tools.run_tool("create_poll", {"question": "Temp?", "options": ["a", "b"]}, member_id=1)
        out = await tools.run_tool("close_poll", {"delete": True}, member_id=2)
        check("only the asker can delete a poll", out.startswith("ERROR"))
        out = await tools.run_tool("close_poll", {"delete": True}, member_id=1)
        check("asker deletes a poll for everyone", out.startswith("Deleted") and "Temp?" not in await tools.run_tool("poll_results", {}, member_id=2))

        await tools.run_tool("create_poll", {"question": "Movie night?", "options": ["Yes", "No"]}, member_id=2)
        brain.client = FakeClient(reply_only("ok"))
        await brain.think(ZAIN, "dm:1", "anything I need to do?")
        ctx = brain.client.messages.requests[0]["system"][1]["text"]
        check("open polls reach Abdo's context", "Open polls: Poll #" in ctx and "Movie night?" in ctx and "waiting on" in ctx)

        real_fetch = briefing._fetch_weather

        async def no_weather(lat, lng, tz):
            raise RuntimeError("offline")

        briefing._fetch_weather, briefing._weather_cache = no_weather, {}
        await fake.add_pantry_item("cheese", None, today + timedelta(days=1), 2)
        try:
            b = await briefing.gather(ZAIN)
            text = briefing.render_text(b)[1]
            check("briefing: shared item going off, never someone else's own food",
                  "Use soon: cheese (tomorrow)" in text and "yogurt" not in text)
            check("briefing: poll waiting for my vote", "Waiting for your vote: Movie night?" in text)
        finally:
            briefing._fetch_weather, briefing._weather_cache = real_fetch, {}
    finally:
        notify.deliver = real
        pantry._last_going.clear()
        fake.pantry, fake.polls, fake.votes, fake.shopping = [], [], [], []


def test_web_fridge_polls():
    print("Scenario: web fridge, going shopping, polls")
    import re
    from fastapi.testclient import TestClient
    from app import main as app_main, notify, pantry
    settings.session_pepper = "test-pepper"
    dms = []

    async def capture(chat_id, text, disable_notification=False):
        dms.append(text)

    async def deliver(member, title, body, url="/", silent=False, urgent=False, ack=None):
        return "push"

    real_send, real_deliver = telegram.send_message, notify.deliver
    telegram.send_message, notify.deliver = capture, deliver
    H = {"X-Abdo": "1"}
    fake.codes.clear()
    pantry._last_going.clear()
    try:
        c = TestClient(app_main.app, base_url="https://testserver")
        c.post("/api/auth/request-code", json={"name": "Zain"}, headers=H)
        c.post("/api/auth/verify", json={"name": "Zain", "code": re.search(r"\b(\d{6})\b", dms[-1]).group(1)}, headers=H)
        today = datetime.now(TZ).date()
        r = c.post("/api/fridge", json={"item": "milk", "expires_on": today.isoformat()}, headers=H)
        check("add fridge item", r.status_code == 200)
        items = c.get("/api/fridge").json()["items"]
        check("fridge item with expiry state", items[0]["label"] == "today" and items[0]["state"] == "soon")
        r = c.post(f"/api/fridge/{items[0]['id']}/done", json={"rebuy": True}, headers=H)
        check("done + rebuy", r.json()["added_to_list"] is True)
        check("going shopping notifies others", c.post("/api/shopping/going", json={"store": "Walmart"}, headers=H).json() == {"notified": 2})
        check("going shopping cooldown -> 429", c.post("/api/shopping/going", json={}, headers=H).status_code == 429)
        r = c.post("/api/polls", json={"question": "Dinner?", "options": ["Shawarma", "Pizza"]}, headers=H)
        pid = r.json()["id"]
        check("create poll", r.status_code == 200)
        check("vote", c.post(f"/api/polls/{pid}/vote", json={"option_index": 1}, headers=H).status_code == 200)
        p = c.get("/api/polls").json()["polls"][0]
        check("poll shows my vote + who's left", p["my_vote"] == 1 and p["mine"] and "Mo" in p["waiting_on"])
        check("bad option -> 400", c.post(f"/api/polls/{pid}/vote", json={"option_index": 4}, headers=H).status_code == 400)
        check("remove my vote", c.delete(f"/api/polls/{pid}/vote", headers=H).status_code == 200
              and c.get("/api/polls").json()["polls"][0]["my_vote"] is None)
        check("removing again -> 404", c.delete(f"/api/polls/{pid}/vote", headers=H).status_code == 404)
        c.post(f"/api/polls/{pid}/vote", json={"option_index": 1}, headers=H)
        check("close my poll", c.post(f"/api/polls/{pid}/close", headers=H).status_code == 200)
        check("can't vote in a closed poll", c.post(f"/api/polls/{pid}/vote", json={"option_index": 0}, headers=H).status_code == 404)
        check("delete my poll", c.delete(f"/api/polls/{pid}", headers=H).status_code == 200 and c.get("/api/polls").json()["polls"] == [])
    finally:
        telegram.send_message, notify.deliver = real_send, real_deliver
        settings.session_pepper = None
        pantry._last_going.clear()
        fake.pantry, fake.polls, fake.votes, fake.shopping = [], [], [], []


# Made-up stops and routes (the real ones live in the gitignored local/bus_trips.sql).
BUS_TRIPS = [
    {"id": 1, "name": "Home stop", "from_stops": ["100", "101"], "to_stops": [], "routes": []},
    {"id": 2, "name": "Groceries", "from_stops": ["101"], "to_stops": ["205"], "routes": []},
    {"id": 3, "name": "Groceries -> home", "from_stops": ["206"], "to_stops": ["100"], "routes": []},
    {"id": 4, "name": "Mall", "from_stops": ["101", "300"], "to_stops": ["399"], "routes": []},
]


def _arrival(route, headsign, stop, at, stops, kind="SCHEDULED", delay=0, block=None):
    """One GRT arrivals row: departs `stop` at `at`; `stops` is the trip's [(id, "HH:MM")]."""
    day = "2026-10-14T"
    return {"stop": {"id": stop, "name": f"Stop {stop}"}, "route": {"shortName": route},
            "trip": {"headsign": headsign, "blockId": block,
                     "stops": [{"stop": {"id": s, "name": f"Stop {s}"}, "departure": f"{day}{hm}:00-04:00"}
                               for s, hm in stops]},
            "type": kind, "departure": f"{day}{at}:00-04:00", "departureDeltaFromScheduledSeconds": delay}


GRT_ROWS = {
    ("100", "101"): [
        _arrival("7", "North", "101", "16:05", [("90", "16:00"), ("101", "16:05"), ("205", "16:12")]),
        _arrival("7", "South", "100", "16:08", [("206", "16:00"), ("100", "16:08"), ("80", "16:15")]),
    ],
    ("101",): [
        _arrival("7", "North", "101", "15:50", [("101", "15:50"), ("205", "15:57")]),          # already gone
        _arrival("9", "East", "101", "16:01", [("101", "16:01"), ("150", "16:10")]),           # doesn't go there
        _arrival("7", "North", "101", "16:05", [("90", "16:00"), ("101", "16:05"), ("205", "16:12")]),
        _arrival("7", "North", "101", "16:35", [("101", "16:35"), ("205", "16:42")], kind="REALTIME", delay=180),
        _arrival("7", "North", "101", "16:50", [("101", "16:50"), ("205", "16:57")], kind="CANCELED"),
    ],
    # An interlined bus: the 12 ends at stop 500 and the same bus (block B1) leaves as the 8,
    # which reaches stop 205. A different block's 8 from 500 doesn't count for that 12.
    ("101", "102"): [
        _arrival("12", "King", "102", "16:10", [("102", "16:10"), ("500", "16:14")], block="B1"),
        _arrival("12", "King", "102", "16:40", [("102", "16:40"), ("500", "16:44")], block="B2"),
    ],
    ("500",): [
        _arrival("8", "Fairway", "500", "16:16", [("500", "16:16"), ("205", "16:22")], block="B1"),
        _arrival("8", "Fairway", "500", "16:46", [("500", "16:46"), ("205", "16:52")], block="B9"),
    ],
    ("206",): [_arrival("7", "South", "206", "16:20", [("205", "16:19"), ("206", "16:20"), ("100", "16:28")])],
    ("101", "300"): [
        _arrival("301", "301 North", "300", "16:03", [("300", "16:03"), ("399", "16:20")]),
        _arrival("7", "North", "101", "16:05", [("101", "16:05"), ("205", "16:12")]),
    ],
}


async def test_buses():
    print("Scenario: buses: GRT departures matched to saved trips")
    from app import transit
    now = datetime(2026, 10, 14, 16, 0, tzinfo=TZ)
    calls = []

    async def fake_post(stops):
        calls.append(tuple(stops))
        return GRT_ROWS[tuple(sorted(stops))]

    real_post, real_now = transit._post, transit.now
    transit._post, transit._cache, transit.now = fake_post, {}, lambda: now
    try:
        trips = BUS_TRIPS
        deps = await transit.next_for(trips[1], now)
        check("only departures that reach the stop, in time order, cancelled dropped",
              [(d["route"], d["at"].strftime("%H:%M")) for d in deps] == [("7", "16:05"), ("7", "16:35")])
        check("arrival time at the destination comes from the trip itself",
              deps[0]["arrive"].strftime("%H:%M") == "16:12" and deps[0]["in_min"] == 5)
        check("live and delay read from GRT", deps[1]["live"] and deps[1]["delay_min"] == 3 and not deps[0]["live"])
        await transit.next_for(trips[1], now)
        check("a second look within a minute is served from the cache", calls.count(("101",)) == 1)
        home = await transit.next_for(trips[0], now)
        check("a trip without a destination lists every departure", [d["headsign"] for d in home] == ["North", "South"])
        mall = await transit.next_for(trips[3], now)
        check("several boarding stops: any route that gets there", [d["route"] for d in mall] == ["301"])
        check("ION headsign without the repeated route number", mall[0]["headsign"] == "North")
        check("direction settles itself (southbound trip isn't 'Groceries')",
              all(d["headsign"] == "North" for d in deps))

        inter = {"id": 9, "name": "Via the 12", "from_stops": ["101", "102"], "to_stops": ["205"], "routes": ["12"]}
        via = await transit.next_for(inter, now)
        check("a bus that carries on as another route counts, with the arrival from the second leg",
              [(d["route"], d["then_route"], d["arrive"].strftime("%H:%M")) for d in via] == [("12", "8", "16:22")])
        check("the tool says to stay on", "stay on as it becomes the 8" in transit.describe(inter, via))

        check("find_trip: exact name", transit.find_trip(trips, "groceries")["id"] == 2)
        check("find_trip: words as said", transit.find_trip(trips, "groceries home")["id"] == 3)
        check("find_trip: unknown -> None", transit.find_trip(trips, "airport") is None)

        fake.bus = trips
        out = await tools.run_tool("next_bus", {"trip": "groceries"}, 1)
        check("tool: times, arrival, live marker",
              "7 to North at 4:05 PM (in 5 min)" in out and "gets to Stop 205 4:12 PM" in out
              and "live, 3 min late" in out)
        out = await tools.run_tool("next_bus", {}, 1)
        check("tool: no trip -> the stop by home, flagged as scheduled",
              out.startswith("Home stop") and "scheduled times" in out and "Other saved trips: Home stop, Groceries" in out)
        out = await tools.run_tool("next_bus", {"trip": "airport"}, 1)
        check("tool: unknown trip lists the saved ones", out.startswith("ERROR") and "Mall" in out)
        out = await tools.run_tool("next_bus", {"trip": "mall", "route": "7"}, 1)
        check("tool: route filter", "Nothing" in out or "nothing" in out)

        async def down(stops):
            raise RuntimeError("timeout")
        transit._post, transit._cache = down, {}
        out = await tools.run_tool("next_bus", {"trip": "groceries"}, 1)
        check("tool: GRT down -> honest error, no times", out.startswith("ERROR") and "PM" not in out)
        ov = await transit.overview(trips, now)
        check("overview: each trip fails on its own", all(not t["ok"] for t in ov) and len(ov) == 4)
        fake.bus = []
        check("tool: nothing saved", "No bus trips" in await tools.run_tool("next_bus", {}, 1))
    finally:
        transit._post, transit._cache, transit.now = real_post, {}, real_now
        fake.bus = []


def test_web_buses():
    print("Scenario: web buses")
    import re
    from fastapi.testclient import TestClient
    from app import main as app_main, transit
    settings.session_pepper = "test-pepper"
    dms = []

    async def capture(chat_id, text, disable_notification=False):
        dms.append(text)

    async def fake_post(stops):
        return GRT_ROWS.get(tuple(sorted(stops)), [])

    real_send, real_post, real_now = telegram.send_message, transit._post, transit.now
    telegram.send_message, transit._post, transit._cache = capture, fake_post, {}
    transit.now = lambda: datetime(2026, 10, 14, 16, 0, tzinfo=TZ)
    H = {"X-Abdo": "1"}
    fake.codes.clear()
    fake.bus = BUS_TRIPS
    try:
        c = TestClient(app_main.app, base_url="https://testserver")
        check("buses need a login", c.get("/api/buses").status_code == 401)
        c.post("/api/auth/request-code", json={"name": "Zain"}, headers=H)
        c.post("/api/auth/verify", json={"name": "Zain", "code": re.search(r"\b(\d{6})\b", dms[-1]).group(1)}, headers=H)
        r = c.get("/api/buses")
        trips = r.json()["trips"]
        check("every saved trip, in order", r.status_code == 200 and [t["name"] for t in trips] == [t["name"] for t in BUS_TRIPS])
        check("trips with departures", [len(t["departures"]) for t in trips] == [2, 2, 1, 1])
        home = c.get("/api/buses?home=1").json()["trips"]
        check("home=1: only the stop by home", [t["name"] for t in home] == ["Home stop"])
        check("departures carry route, time, and arrival",
              all(t["ok"] for t in trips) and all({"route", "at", "in_min", "live", "arrive"} <= set(d) for t in trips for d in t["departures"]))
    finally:
        telegram.send_message, transit._post, transit._cache, transit.now = real_send, real_post, {}, real_now
        settings.session_pepper = None
        fake.bus = []


def test_tts_model_routing():
    print("Scenario: digit-bearing replies route to multilingual_v2")
    from app import tts
    check("a worded reply stays on Flash", tts._pick_model("تمام، الساعة سبعة") == settings.tts_model)
    check("a reply with a digit routes to multilingual_v2",
          tts._pick_model("الساعة 7") == "eleven_multilingual_v2")

    print("Scenario: numbers are spoken as words (Egyptian or English by context)")
    from app.speak import normalize as say
    check("Arabic-Indic digits in Arabic", say("فاضي للـ٣٠ يوم") == "فاضي للـتلاتين يوم")
    check("compound numbers the Egyptian way", say("25 دقيقة") == "خمسة وعشرين دقيقة")
    check("money exact, cents included", say("دفعت $12.50") == "دفعت اتناشر دولار وخمسين سنت"
          and say("دفعت $1.75") == "دفعت دولار وخمسة وسبعين سنت")
    check("money in an English sentence", say("you paid $2") == "you paid two dollars")
    check("clock times, Egyptian style", say("الساعة 7:30") == "الساعة سبعة ونص"
          and say("الساعة 12:45") == "الساعة واحدة إلا ربع" and say("الساعة 3:15 pm") == "الساعة تلاتة وربع العصر")
    check("clock time in English", say("the bus is at 4:05 pm") == "the bus is at four oh five p m")
    check("percent and years", say("المطر 40%") == "المطر أربعين في المية" and say("سنة 2026") == "سنة ألفين وستة وعشرين")
    check("phone numbers and codes digit by digit", say("رقمه 0123") == "رقمه صفر، واحد، اتنين، تلاتة")
    check("thousands", say("3000 و 1,200") == "تلات تلاف و ألف وميتين")
    check("text without numbers is untouched", say("تمام يا زين") == "تمام يا زين")

    print("Scenario: spoken lists flow as one sentence (bare-word lists lose the accent)")
    from app.speak import for_speech, flow_lists
    check("a list after a colon is joined with و",
          flow_lists("هتحتاج: فرخة، ملوخية، توم، ورق لورا، ورز.") == "هتحتاج: فرخة وملوخية وتوم وورق لورا ورز.")
    check("ordinary clauses with commas are left alone",
          flow_lists("أيوه، أنا فاكر، بس استنى شوية، هقولك.") == "أيوه، أنا فاكر، بس استنى شوية، هقولك.")
    check("two items aren't a list", flow_lists("خد: رز، شعرية") == "خد: رز، شعرية")
    check("bullet lines become one sentence", flow_lists("هتحتاج:\n- فرخة\n- ملوخية\n- توم\n") == "هتحتاج:\nفرخة وملوخية وتوم.\n")
    check("for_speech does both", for_speech("هتحتاج: 2 فرخة، ملوخية، توم") == "هتحتاج: اتنين فرخة وملوخية وتوم")

    print("Scenario: sound tags in a transcript aren't speech")
    from app.stt import clean
    check("only a breathing tag -> nothing heard", clean("[صوت تنفس].") == "")
    check("tags removed from real speech", clean("عبده (يضحك) فين مو") == "عبده فين مو")


async def main():
    await test_model_and_request_shape()
    test_static_persona_cache_invariant()
    await test_handle_turn_logs_and_speakers()
    await test_history_drops_leading_assistant()
    await test_history_time_and_age()
    await test_handle_turn_voice_and_failure()
    await test_unidentified_speaker()
    await test_refusal_handled()
    await test_thinking_blocks_read_by_type()
    await test_max_tokens_without_text()
    await test_empty_reply_fallback()
    await test_tool_failure_degrades()
    await test_tool_loop_terminates()
    await test_tool_dispatch_unknown()
    await test_remember_and_recall_fact()
    await test_recall_empty()
    await test_store_dedup()
    await test_store_guard_invalid_embedding()
    test_is_valid_robust_to_numpy()
    await test_search_returns_db_shaped_fact()
    await test_where_is()
    await test_get_calendar()
    await test_calendar_weekday_read()
    test_event_times_shown_local()
    await test_calendar_write()
    test_date_resolution()
    await test_calendar_read_guard()
    await test_calendar_not_found_guard()
    await test_shopping_list()
    await test_packages()
    test_prompt_content()
    test_voice_prompt()
    await test_web_search_offered_and_household_routes_to_recall()
    test_parse_update()
    test_tts_model_routing()
    test_resolve_due_and_until()
    await test_reminder_and_status_tools()
    test_prayer_times()
    await test_notify_deliver()
    await test_scheduler()
    test_weather_hours()
    await test_urgent_reminders()
    await test_calendar_view_cache()
    test_calendar_no_duplicates()
    await test_briefing_build()
    test_money_math()
    await test_money_tools()
    await test_travel_mode()
    test_timetable_validate()
    test_quest_parser()
    await test_who_is_free_and_exams()
    await test_fridge_shopping_polls()
    await test_buses()
    print(f"\n{passed} passed, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
    # TestClient runs its own event loop, so the HTTP tests run outside asyncio.run.
    test_telegram_webhook()
    test_access_log_redaction()
    test_config_strips_whitespace()
    test_web_app()
    test_web_today()
    test_web_money()
    test_web_schedule()
    test_web_fridge_polls()
    test_web_buses()
    print(f"\nTOTAL: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
