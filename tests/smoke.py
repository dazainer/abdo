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
    async def add_reminder(self, member_id, text, due_at, created_by):
        self._rem_seq = getattr(self, "_rem_seq", 0) + 1
        self.reminders.append({"id": self._rem_seq, "member_id": member_id, "text": text,
                               "due_at": due_at, "created_by": created_by, "done": False})
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

    async def claim_reminder(self, reminder_id):
        for r in self.reminders:
            if r["id"] == reminder_id and not r["done"]:
                r["done"] = True
                return self._rem_row(r)
        return None

    async def claim_daily(self, member_id, kind, local_date):
        key = (member_id, kind, local_date)
        if key in self.sent_log:
            return False
        self.sent_log.add(key)
        return True

    # status
    async def set_status(self, member_id, text, until_at):
        self.statuses[member_id] = {"member_id": member_id, "name": NAMES[member_id], "text": text,
                                    "until_at": until_at, "updated_at": datetime.now(timezone.utc)}

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
        return [{"role": r["role"], "content": r["content"],
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

    async def update_package_status(self, package_id, status):
        for p in self.packages:
            if p["id"] == package_id and p["status"] in ("expected", "arrived"):
                p["status"] = status
                return p["description"]
        return None


fake = FakeDB()
for _name in ("get_member_by_telegram_id", "roster_string", "get_member_id_by_name",
              "get_member", "thread_page", "create_login_code", "count_recent_login_codes",
              "latest_live_login_code", "bump_login_code_attempts", "use_login_code",
              "create_session", "session_member", "touch_session", "delete_session",
              "save_push_subscription", "delete_push_subscription", "push_subscriptions_for",
              "all_members", "set_member_prefs", "add_reminder", "upcoming_reminders",
              "cancel_reminder", "due_reminder_ids", "claim_reminder", "claim_daily",
              "set_status", "clear_status", "current_statuses",
              "log_message", "recent_messages", "add_fact", "nearest_fact", "update_fact",
              "search_facts", "upsert_location", "get_location", "get_all_locations",
              "add_shopping_item", "get_shopping_list", "mark_item_bought",
              "clear_shopping_list", "add_package", "get_open_packages",
              "update_package_status"):
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
    check("current turn prefixed with the speaker", sent[-1]["content"] == "Zain: hi abdo")
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
    check("unknown speaker prefixed 'Someone:'", first["messages"][-1]["content"] == "Someone: eh el na2es?")
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
    check("speaker-prefix convention explained", "starts with the speaker's name" in p)
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
        check("unknown sender gets the polite refusal",
              r.status_code == 200 and sent == [(5, tg_channel.UNKNOWN_SENDER)])

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
              sent_msgs[0]["content"] == "Zain: from telegram")
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

    async def deliver(member, title, body, url="/", silent=False):
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


async def test_briefing_build():
    print("Scenario: briefing lines appear only when there's something real to say")
    from app import briefing
    real_fetch = briefing._fetch_weather
    sample = {"current": {"temperature_2m": 3.2, "apparent_temperature": -1.4, "weather_code": 71},
              "daily": {"temperature_2m_max": [5.4], "temperature_2m_min": [-2.0],
                        "precipitation_probability_max": [60]}}

    async def good():
        return sample

    async def bad():
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
    briefing._fetch_weather, briefing._weather_cache = good, None
    try:
        title, body = await briefing.build(ZAIN, now)
        check("greets by Arabic name", title.startswith("صباح الخير يا زين"))
        check("weather line from real numbers",
              "Weather: 3°, feels like -1°, light snow. High 5°, low -2°, 60% chance" in body)
        check("only today's events", "Today: Suite dinner at 19:00" in body and "Not today" not in body)
        check("my waiting package", "Waiting for you at the building: desk lamp" in body)
        check("grocery count", "Groceries: 1 item on the list" in body)
        check("roommates' statuses", "Mo: away" in body)
        check("remaining prayers", "Prayers: " in body and "Isha" in body)

        briefing._fetch_weather, briefing._weather_cache = bad, None
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
        briefing._fetch_weather, briefing._weather_cache = real_fetch, None
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

        check("prefs default", c.get("/api/me/prefs").json()["prefs"]["briefing_time"] == "08:00")
        r = c.patch("/api/me/prefs", json={"briefing_time": "07:30", "prayers": False}, headers=H)
        check("prefs update", r.json()["prefs"] == {"briefing": True, "briefing_time": "07:30", "prayers": False})
        check("invalid time -> 422", c.patch("/api/me/prefs", json={"briefing_time": "25:00"}, headers=H).status_code == 422)
        check("empty update -> 400", c.patch("/api/me/prefs", json={}, headers=H).status_code == 400)
    finally:
        telegram.send_message = real_send
        settings.session_pepper = None
        MEMBERS[1].pop("prefs", None)


def test_tts_model_routing():
    print("Scenario: digit-bearing replies route to multilingual_v2")
    from app import tts
    check("a worded reply stays on Flash", tts._pick_model("تمام، الساعة سبعة") == settings.tts_model)
    check("a reply with a digit routes to multilingual_v2",
          tts._pick_model("الساعة 7") == "eleven_multilingual_v2")


async def main():
    await test_model_and_request_shape()
    test_static_persona_cache_invariant()
    await test_handle_turn_logs_and_speakers()
    await test_history_drops_leading_assistant()
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
    await test_briefing_build()
    print(f"\n{passed} passed, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
    # TestClient runs its own event loop, so the HTTP tests run outside asyncio.run.
    test_telegram_webhook()
    test_access_log_redaction()
    test_config_strips_whitespace()
    test_web_app()
    test_web_today()
    print(f"\nTOTAL: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
