"""Offline smoke test for Abdo Phase 1.

Exercises the real brain tool-loop, tool dispatch, DB result parsing, and
update parsing — with the network (Anthropic, Telegram) and Postgres faked out.
No real credentials, DB, or HTTP calls. Run:  python tests/smoke.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

# --- Fake env so config.Settings() loads without a real .env -----------------
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
os.environ.setdefault("COHERE_API_KEY", "co-fake")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://fake/fake")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import brain, db, tools, telegram, embeddings  # noqa: E402


# --- In-memory fake of the dog-feeding state, replacing the db layer ---------
class FakeDB:
    def __init__(self):
        self.fed_today = None        # None => not fed; dict => fed row
        self.logged = []             # (member_id, chat_id, role, content)
        self.facts = []              # stored household facts

    async def recent_messages(self, chat_id, limit=10):
        return []                    # fresh conversation

    async def roster_string(self):
        return "Zain (member)"

    async def dogs_fed_today(self):
        return self.fed_today

    async def mark_dogs_fed(self, member_id):
        if self.fed_today is None:
            self.fed_today = {"fed_by_name": "Zain"}
            return True              # fresh insert
        return False                 # already fed (idempotent)

    async def log_message(self, member_id, chat_id, role, content):
        self.logged.append((member_id, chat_id, role, content))

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
        # Embedding is faked, so just return what's stored (most-recent first).
        return list(reversed(self.facts))[:k]

    async def get_member_id_by_name(self, name):
        return {"zain": 1, "omar": 2}.get(name.lower())

    async def add_order(self, description, expected_on, ordered_by, paid, tip_note):
        self._order_seq = getattr(self, "_order_seq", 0) + 1
        self.orders.append({"id": self._order_seq, "description": description,
                            "expected_on": expected_on, "ordered_by": ordered_by,
                            "paid": paid, "tip_note": tip_note, "status": "pending"})
        return self._order_seq

    async def get_orders(self, expected_on):
        names = {1: "Zain", 2: "Omar"}
        return [{"id": o["id"], "description": o["description"], "paid": o["paid"],
                 "tip_note": o["tip_note"], "ordered_by_name": names.get(o["ordered_by"])}
                for o in self.orders
                if o["status"] == "pending" and o["expected_on"] == expected_on]

    async def mark_order_arrived(self, order_id):
        for o in self.orders:
            if o["id"] == order_id and o["status"] == "pending":
                o["status"] = "arrived"
                return True
        return False

    async def upsert_location(self, member_id, lat, lng):
        self.locations[member_id] = (lat, lng)

    async def get_location(self, name):
        return self._loc_rows.get(name.lower())

    async def get_all_locations(self):
        return list(self._loc_rows.values())

    async def add_shopping_item(self, item, qty, member_id):
        if any(s["item"].lower() == item.lower() and not s["bought"] for s in self.shopping):
            return False
        self.shopping.append({"item": item, "qty": qty, "bought": False})
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


fake = FakeDB()
fake.shopping = []       # list of {item, qty, bought}
fake.locations = {}      # member_id -> (lat, lng), written by upsert
fake._loc_rows = {}      # name.lower() -> asyncpg-style row dict, for reads
fake.orders = []         # list of order dicts
for name in ("recent_messages", "roster_string", "dogs_fed_today",
             "mark_dogs_fed", "log_message", "add_fact", "nearest_fact",
             "update_fact", "search_facts", "upsert_location", "get_location",
             "get_all_locations", "add_shopping_item", "get_shopping_list",
             "mark_item_bought", "clear_shopping_list", "get_member_id_by_name",
             "add_order", "get_orders", "mark_order_arrived"):
    setattr(db, name, getattr(fake, name))


# --- Fake embeddings: record input_type so we can assert store/recall asymmetry --
embed_calls = []


async def fake_embed(text, *, input_type):
    embed_calls.append(input_type)
    # Deterministic per text and non-zero, so embeddings.is_valid() passes and
    # identical content produces an identical vector (lets the dedup path fire).
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(h[i % len(h)] / 255.0) + 0.001 for i in range(embeddings.EMBED_DIM)]


embeddings.embed = fake_embed


# --- Fake calendar + a real home coordinate for the geofence ------------------
from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from app import calendar_svc, geo  # noqa: E402
from app.config import settings  # noqa: E402

settings.home_lat = 30.0000      # New Cairo-ish; lets geo.describe compute distance
settings.home_lng = 31.0000

_fake_events = []
_created_events = []
calendar_svc.is_configured = lambda: True
calendar_svc.get_events = lambda days_ahead=7: list(_fake_events)


def fake_create_event(summary, day, time, end_time=None):
    # Mirror the real signature: day+time, code resolves the date. The fake echoes
    # the resolved start so the tool's confirmation string can be asserted.
    d = calendar_svc.resolve_date(day)
    start = calendar_svc._compose(d, time)
    ev = {"id": "evt_new", "summary": summary, "start": start}
    _created_events.append(ev)
    return ev


def fake_update_event(event_id, summary=None, day=None, time=None, end_time=None):
    return {"id": event_id, "summary": summary or "Family Gathering",
            "start": "2026-06-17T19:00:00", "end": "2026-06-17T20:00:00"}


_deleted_events = []


def fake_delete_event(event_id):
    _deleted_events.append(event_id)


calendar_svc.create_event = fake_create_event
calendar_svc.update_event = fake_update_event
calendar_svc.delete_event = fake_delete_event


# --- Scripted Anthropic client: simulate one tool_use turn, then final text --
def _tool_use_block(tool_name, tool_id):
    return SimpleNamespace(type="tool_use", name=tool_name, input={}, id=tool_id)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


class FakeMessages:
    def __init__(self, script):
        self._script = script        # list of responses to return in order
        self._i = 0
        self.calls = []               # records (model, messages) per call

    async def create(self, *, model, max_tokens, system, tools, messages):
        self.calls.append((model, [m["role"] for m in messages]))
        self.systems = getattr(self, "systems", [])
        self.systems.append(system)
        resp = self._script[self._i]
        self._i += 1
        return resp


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)


def script_for(tool_name, final_text):
    """First call asks for the tool; second call (after result) returns text."""
    return [
        SimpleNamespace(stop_reason="tool_use",
                        content=[_tool_use_block(tool_name, "toolu_1")]),
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text_block(final_text)]),
    ]


MEMBER = {"id": 1, "name": "Zain", "role": "member"}


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


async def test_status_when_not_fed():
    print("Scenario: 'الكلاب اتأكلوا؟' when NOT fed")
    fake.fed_today = None
    brain.client = FakeClient(script_for("get_dog_status", "لسه يا زين، الكلاب مأكلوش."))
    reply = await brain.think(MEMBER, chat_id=99, user_text="الكلاب اتأكلوا؟")
    check("tool loop returned final text", reply == "لسه يا زين، الكلاب مأكلوش.")
    check("two model calls (tool turn + answer)", len(brain.client.messages.calls) == 2)
    check("second call carries tool_result in history",
          brain.client.messages.calls[1][1][-1] == "user")


async def test_mark_fed_then_status():
    print("Scenario: 'اطعمت الكلاب' marks fed, then status reports fed")
    fake.fed_today = None
    brain.client = FakeClient(script_for("mark_dogs_fed", "تمام، سجّلت إن الكلاب اتأكلت. 🐶"))
    reply = await brain.think(MEMBER, chat_id=99, user_text="اطعمت الكلاب")
    check("got a confirmation reply", "تمام" in reply)
    check("db state flipped to fed", fake.fed_today is not None)

    # direct tool dispatch: a second mark is idempotent for the day
    out = await tools.run_tool("mark_dogs_fed", {}, member_id=1)
    check("second mark is idempotent", out == "Already marked fed today.")
    status = await tools.run_tool("get_dog_status", {}, member_id=1)
    check("status now reports FED by name", status == "FED today (by Zain).")


async def test_remember_and_recall_fact():
    print("Scenario: store a household fact, then recall it (RAG tools)")
    fake.facts.clear()
    embed_calls.clear()

    # 'الواي فاي بتاعنا الباسورد بتاعه ...' -> Claude calls remember_fact
    brain.client = FakeClient([
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", name="remember_fact",
                            input={"content": "The wifi password is khalil2024.",
                                   "category": "wifi"}, id="t1")]),
        SimpleNamespace(stop_reason="end_turn",
                        content=[_text_block("تمام، حفظت الباسورد.")]),
    ])
    await brain.think(MEMBER, chat_id=99, user_text="الواي فاي الباسورد khalil2024")
    check("fact persisted", len(fake.facts) == 1 and fake.facts[0]["category"] == "wifi")
    check("stored with input_type=search_document", embed_calls == ["search_document"])

    # Now recall it (cross-lingual: English question).
    embed_calls.clear()
    out = await tools.run_tool("recall_facts", {"query": "what's the wifi password"}, member_id=1)
    check("recall returns the stored fact", "khalil2024" in out and "[wifi]" in out)
    check("recall used input_type=search_query", embed_calls == ["search_query"])


async def test_recall_empty():
    print("Scenario: recall with nothing stored")
    fake.facts.clear()
    out = await tools.run_tool("recall_facts", {"query": "anything"}, member_id=1)
    check("honest 'no facts' when store is empty", out == "No matching household facts found.")


async def test_store_dedup():
    print("Scenario: re-stating a fact updates the row, never duplicates (Fix 1d)")
    fake.facts.clear()
    out = await tools.run_tool(
        "remember_fact",
        {"content": "The Wi-Fi password is koki2013.", "category": "wifi"}, member_id=1)
    check("first store inserts", out == "Stored." and len(fake.facts) == 1)
    out = await tools.run_tool(
        "remember_fact",
        {"content": "The Wi-Fi password is koki2013.", "category": "wifi"}, member_id=1)
    check("re-stating the SAME fact updates in place (no second row)",
          "Updated" in out and len(fake.facts) == 1)
    await tools.run_tool(
        "remember_fact",
        {"content": "The spare key is with the bawab.", "category": "location_of_things"}, member_id=1)
    check("a genuinely different fact still inserts", len(fake.facts) == 2)


async def test_store_guard_invalid_embedding():
    print("Scenario: a junk embedding is never stored as a fact (Fix 1c guard)")
    fake.facts.clear()
    real_embed = embeddings.embed

    async def bad_embed(text, *, input_type):
        return [0.0] * embeddings.EMBED_DIM    # all-zero => invalid

    embeddings.embed = bad_embed
    try:
        out = await tools.run_tool(
            "remember_fact", {"content": "x", "category": "misc"}, member_id=1)
        check("invalid embedding returns an error, not 'Stored'", out.startswith("ERROR"))
        check("nothing was written on a bad embedding", len(fake.facts) == 0)
    finally:
        embeddings.embed = real_embed


async def test_calendar_weekday_read():
    print("Scenario: calendar reads carry a code-resolved weekday (Fix 2)")
    global _fake_events
    # 2026-06-19 is a Friday; 2026-06-20 is a Saturday.
    _fake_events = [
        {"id": "evt1", "start": "2026-06-19", "summary": "Trip to Cairo"},
        {"id": "evt2", "start": "2026-06-20T19:00:00", "summary": "Family dinner"},
    ]
    calendar_svc.is_configured = lambda: True
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("all-day event shows its true weekday (Friday)", "Friday 19 June" in out)
    check("timed event shows weekday + time (Saturday ... 19:00)",
          "Saturday 20 June" in out and "at 19:00" in out)
    check("event id still exposed for edits", "evt1" in out and "evt2" in out)


async def test_orders():
    print("Scenario: orders add/get/arrive with server-side date resolution (Fix 5)")
    fake.orders = []
    out = await tools.run_tool(
        "add_order",
        {"description": "Amazon package", "day": "tomorrow", "paid": False,
         "ordered_by": "Zain", "tip_note": "50 EGP"}, member_id=1)
    check("add_order confirms with id + COD", "#1" in out and "cash on delivery" in out)
    tomorrow = (datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)).date()
    check("day was resolved server-side to a real date",
          fake.orders[0]["expected_on"] == tomorrow)
    check("orderer name mapped to a member id", fake.orders[0]["ordered_by"] == 1)

    out = await tools.run_tool("get_orders", {"day": "tomorrow"}, member_id=1)
    check("get_orders lists the pending order with who/pay/tip",
          "Amazon package" in out and "Zain" in out and "cash on delivery" in out and "50 EGP" in out)

    out = await tools.run_tool("get_orders", {}, member_id=1)   # defaults to today
    check("a different day (today) shows no orders", "No pending orders" in out)

    out = await tools.run_tool("mark_order_arrived", {"order_id": 1}, member_id=1)
    check("mark_order_arrived confirms", "arrived" in out)
    out = await tools.run_tool("get_orders", {"day": "tomorrow"}, member_id=1)
    check("an arrived order leaves the pending list", "No pending orders" in out)

    out = await tools.run_tool("mark_order_arrived", {"order_id": 999}, member_id=1)
    check("marking a non-existent order errors, never fakes success", out.startswith("No pending order"))


def test_tts_model_routing():
    print("Scenario: digit-bearing replies route to multilingual_v2 (Fix 3b)")
    from app import tts
    check("a worded reply stays on Flash",
          tts._pick_model("تمام، الساعة سبعة") == settings.tts_model)
    check("a reply with a digit routes to multilingual_v2",
          tts._pick_model("الساعة 7") == "eleven_multilingual_v2")


def test_grounding_and_dialect_prompt():
    print("Scenario: grounding rules + Egyptian dialect are in the system prompt")
    from app.prompts import build_system_prompt
    p = build_system_prompt("Zain", "member", "Zain (member)")
    check("grounding mandates recall_facts before 'don't know'", "MUST call recall_facts" in p)
    check("grounding forbids guessing weekdays", "never guess or compute a weekday" in p)
    check("calendar confirms exactly once", "exactly ONCE" in p)
    check("Egyptian dialect glossary present (معنديش)", "معنديش" in p)
    check("orders capability described", "orders/deliveries" in p)


async def test_where_is():
    print("Scenario: where_is (geofence + fresh/stale tense)")
    now = datetime.now(timezone.utc)             # aware, like asyncpg returns
    fresh = now - timedelta(minutes=2)
    # Zain exactly at home; Omar ~3km away — both freshly updated.
    fake._loc_rows = {
        "zain": {"name": "Zain", "lat": 30.0000, "lng": 31.0000, "updated_at": fresh},
        "omar": {"name": "Omar", "lat": 30.0270, "lng": 31.0000, "updated_at": fresh},
    }
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("fresh reading is present tense 'is home'", "Zain is home" in out and "live" in out)
    out = await tools.run_tool("where_is", {"name": "Omar"}, member_id=1)
    check("member away reads distance", "km from home" in out)
    out = await tools.run_tool("where_is", {"name": "everyone"}, member_id=1)
    check("'everyone' lists all sharers", "Zain" in out and "Omar" in out)

    # A stale reading (sharing stopped hours ago) must flip to PAST tense and
    # flag itself stale, so the model can't claim they're there now.
    stale = now - timedelta(hours=9)
    fake._loc_rows = {"zain": {"name": "Zain", "lat": 30.0, "lng": 31.0, "updated_at": stale}}
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("stale reading is past tense 'was home'", "Zain was home" in out)
    check("stale reading flagged stale with age", "stale" in out and "~9h ago" in out)
    check("stale reading hedges current whereabouts", "may not be there now" in out)

    fake._loc_rows = {}
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("not-sharing handled", "isn't sharing" in out)
    out = await tools.run_tool("where_is", {"name": "everyone"}, member_id=1)
    check("nobody-sharing handled", out == "No one is sharing their location right now.")


async def test_get_calendar():
    print("Scenario: get_calendar")
    global _fake_events
    _fake_events = [{"id": "evt1", "start": "2026-06-19", "summary": "Friday lunch at Teta's"}]
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("returns upcoming events", "Friday lunch at Teta's" in out)
    check("reader exposes event id (for edits)", "evt1" in out)

    _fake_events = []
    out = await tools.run_tool("get_calendar", {"days_ahead": 3}, member_id=1)
    check("empty window handled", out == "No events on the shared calendar in that window.")

    # Unconfigured path.
    calendar_svc.is_configured = lambda: False
    out = await tools.run_tool("get_calendar", {}, member_id=1)
    check("unconfigured calendar handled", out == "The shared calendar isn't connected yet.")
    calendar_svc.is_configured = lambda: True   # restore


async def test_calendar_write():
    print("Scenario: create_event / update_event (day+time, code resolves date)")
    _created_events.clear()
    out = await tools.run_tool(
        "create_event",
        {"summary": "Family Gathering", "day": "tomorrow", "time": "19:00"}, member_id=1)
    check("create echoes confirmation with id", "Family Gathering" in out and "evt_new" in out)
    check("create actually wrote to the calendar",
          len(_created_events) == 1 and _created_events[0]["summary"] == "Family Gathering")
    # The model passed a spoken day; the start the tool wrote is a real resolved date.
    tomorrow = (datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)).date().isoformat()
    check("spoken day was resolved to a real date in code",
          _created_events[0]["start"].startswith(tomorrow + "T19:00"))

    out = await tools.run_tool(
        "update_event",
        {"event_id": "evt_new", "time": "20:00"}, member_id=1)
    check("update echoes confirmation", out.startswith("Updated:") and "evt_new" in out)

    _deleted_events.clear()
    out = await tools.run_tool("delete_event", {"event_id": "evt_new"}, member_id=1)
    check("delete confirms removal", out == "Deleted the event.")
    check("delete actually called the calendar", _deleted_events == ["evt_new"])


def test_date_resolution():
    print("Scenario: deterministic Cairo date resolution (no model arithmetic)")
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt, date as _date
    now = _dt(2026, 6, 17, 12, 0, tzinfo=_ZI(settings.timezone))   # a Wednesday
    r = calendar_svc.resolve_date
    check("'today' -> today's date", r("today", now=now) == now.date())
    check("'tomorrow' -> +1 day", r("tomorrow", now=now) == now.date() + timedelta(days=1))
    check("'day after tomorrow' -> +2 days",
          r("day after tomorrow", now=now) == now.date() + timedelta(days=2))
    check("explicit ISO date passes through", r("2026-06-19", now=now) == _date(2026, 6, 19))
    # The classic Haiku bug: 'Wednesday' must land on a real Wednesday (the 17th, today).
    check("'Wednesday' on a Wednesday -> that same Wednesday",
          r("Wednesday", now=now) == _date(2026, 6, 17) and r("Wednesday", now=now).weekday() == 2)
    # A weekday later in the week resolves to its real date, not an arithmetic guess.
    check("'Friday' -> the coming Friday (19th)",
          r("Friday", now=now) == _date(2026, 6, 19) and r("Friday", now=now).weekday() == 4)
    check("Arabic weekday 'السبت' -> the coming Saturday (20th)",
          r("السبت", now=now) == _date(2026, 6, 20))
    check("Franco weekday 'el 7ad' style ('7ad') -> the coming Sunday (21st)",
          r("7ad", now=now) == _date(2026, 6, 21))
    check("'next Wednesday' jumps a full week (+7)",
          r("next Wednesday", now=now) == _date(2026, 6, 24))
    # Every resolved weekday actually matches the named weekday — never off by a day.
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    check("every weekday name resolves to its true weekday",
          all(r(n, now=now).weekday() == i for i, n in enumerate(names)))
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
        check("failed read tells the model not to create off it",
              "do NOT create" in out or "do not create" in out.lower())
        check("read was retried once before giving up", attempts["n"] == 2)
    finally:
        calendar_svc.get_events = real_get_events

    # A genuinely empty calendar is still distinct from a failed one.
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
        check("update of missing id returns an error, not success",
              out.startswith("ERROR") and "made_up" in out)
        out = await tools.run_tool("delete_event", {"event_id": "made_up"}, member_id=1)
        check("delete of missing id returns an error, not success",
              out.startswith("ERROR") and "made_up" in out)
    finally:
        calendar_svc.update_event = real_update
        calendar_svc.delete_event = real_delete


async def test_shopping_list():
    print("Scenario: shared shopping list (add / view / buy / clear)")
    fake.shopping = []

    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("empty list reads empty", out == "The shopping list is empty.")

    out = await tools.run_tool("add_to_shopping_list", {"item": "milk", "qty": "2 kilo"}, member_id=1)
    check("add confirms the item", "milk" in out and "Added" in out)
    await tools.run_tool("add_to_shopping_list", {"item": "bread"}, member_id=1)

    out = await tools.run_tool("add_to_shopping_list", {"item": "Milk"}, member_id=1)
    check("duplicate (case-insensitive) rejected", "already on the list" in out)

    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("list shows both items", "milk" in out and "bread" in out)
    check("quantity rendered when present", "(2 kilo)" in out)

    out = await tools.run_tool("mark_item_bought", {"item": "milk"}, member_id=1)
    check("marking bought confirms", "milk" in out and "bought" in out)
    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("bought item leaves the open list", "milk" not in out and "bread" in out)

    out = await tools.run_tool("mark_item_bought", {"item": "eggs"}, member_id=1)
    check("buying an absent item is honest", "isn't on the list" in out)

    out = await tools.run_tool("clear_shopping_list", {}, member_id=1)
    check("clear reports the count", "Cleared 1 item" in out)
    out = await tools.run_tool("get_shopping_list", {}, member_id=1)
    check("list empty after clear", out == "The shopping list is empty.")


async def test_calendar_runs_on_haiku():
    print("Scenario: calendar turns now run on Haiku (Sonnet escalation retired)")
    check("Sonnet escalation removed from brain", not hasattr(brain, "_pick_model"))
    # A calendar turn through the real tool loop must call the model on Haiku.
    _fake_events.clear()
    brain.client = FakeClient(script_for("get_calendar", "مفيش حاجة في التقويم الفترة دي."))
    await brain.think(MEMBER, chat_id=99, user_text="زود ميعاد يوم الجمعة الساعة 7")
    models_used = {m for m, _ in brain.client.messages.calls}
    check("every model call on a calendar turn used Haiku", models_used == {brain.HAIKU})
    # And a plain chat turn is Haiku too (everyday driver unchanged).
    fake.fed_today = None
    brain.client = FakeClient(script_for("get_dog_status", "لسه يا زين."))
    await brain.think(MEMBER, chat_id=99, user_text="الكلاب اتأكلوا؟")
    check("plain chat also on Haiku",
          {m for m, _ in brain.client.messages.calls} == {brain.HAIKU})


async def test_voice_brain_threads_flag():
    print("Scenario: brain.think(voice=True) reaches the model with the spoken-reply hint")
    fake.fed_today = None
    brain.client = FakeClient(script_for("get_dog_status", "لسه يا زين."))
    await brain.think(MEMBER, chat_id=99, user_text="الكلاب اتأكلوا؟", voice=True)
    check("voice hint present in the system prompt sent to the model",
          all("SPOKEN ALOUD" in s for s in brain.client.messages.systems))
    # The text path (default voice=False) must NOT carry the hint.
    brain.client = FakeClient(script_for("get_dog_status", "لسه يا زين."))
    await brain.think(MEMBER, chat_id=99, user_text="الكلاب اتأكلوا؟")
    check("text path keeps the un-voiced prompt",
          all("SPOKEN ALOUD" not in s for s in brain.client.messages.systems))


async def test_empty_reply_fallback():
    print("Scenario: model returns no text -> safe fallback, never an empty send")
    brain.client = FakeClient([
        SimpleNamespace(stop_reason="end_turn", content=[_text_block("   ")]),
    ])
    reply = await brain.think(MEMBER, chat_id=99, user_text="إزيك")
    check("empty/blank model output replaced", isinstance(reply, str) and reply.strip() != "")


async def test_tool_failure_degrades():
    print("Scenario: a failing tool degrades honestly (no crash, no silence)")
    # Simulate a broken tool (bad calendar id / Cohere down): run_tool raises.
    real_run_tool = brain.run_tool

    async def boom(name, tool_input, member_id):
        raise RuntimeError("calendar 404")

    brain.run_tool = boom
    try:
        brain.client = FakeClient(script_for(
            "get_calendar", "آسف، مش عارف أوصل للتقويم دلوقتي."))
        reply = await brain.think(MEMBER, chat_id=99, user_text="عندنا إيه الجمعة؟")
        check("tool exception does not crash the loop", isinstance(reply, str) and reply)
        # The model's turn after the failure must have received an ERROR result.
        last = brain.client.messages.calls[-1]
        check("model was given an error tool_result to react to", last[1][-1] == "user")
    finally:
        brain.run_tool = real_run_tool


async def test_tool_loop_terminates():
    print("Scenario: tool loop is bounded (no infinite loop)")

    class AlwaysToolUse:
        def __init__(self):
            self.calls = 0

        async def create(self, **kw):
            self.calls += 1
            return SimpleNamespace(stop_reason="tool_use",
                                   content=[_tool_use_block("get_dog_status", f"t{self.calls}")])

    class AlwaysToolClient:
        def __init__(self):
            self.messages = AlwaysToolUse()

    brain.client = AlwaysToolClient()
    reply = await brain.think(MEMBER, chat_id=99, user_text="loop forever")
    check("loop stops at the cap", brain.client.messages.calls == brain.MAX_TOOL_ROUNDS)
    check("returns a graceful fallback", isinstance(reply, str) and len(reply) > 0)


async def test_tool_dispatch_unknown():
    print("Scenario: unknown tool name")
    out = await tools.run_tool("does_not_exist", {}, member_id=1)
    check("unknown tool handled gracefully", out.startswith("Unknown tool:"))


def test_parse_update():
    print("Scenario: telegram.parse_update (typed payloads)")
    text_update = {"message": {"chat": {"id": 5}, "from": {"id": 7}, "text": "hi"}}
    check("parses a text message", telegram.parse_update(text_update) ==
          {"chat_id": 5, "from_user": {"id": 7}, "kind": "text", "text": "hi"})

    # Live location arrives as edited_message.
    loc_update = {"edited_message": {"chat": {"id": 5}, "from": {"id": 7},
                                     "location": {"latitude": 30.1, "longitude": 31.2}}}
    check("parses a live-location edit", telegram.parse_update(loc_update) ==
          {"chat_id": 5, "from_user": {"id": 7}, "kind": "location", "lat": 30.1, "lng": 31.2})

    voice_update = {"message": {"chat": {"id": 5}, "from": {"id": 7},
                                "voice": {"file_id": "vf1", "duration": 4}}}
    check("parses a voice message", telegram.parse_update(voice_update) ==
          {"chat_id": 5, "from_user": {"id": 7}, "kind": "voice",
           "file_id": "vf1", "duration": 4})

    photo = {"message": {"chat": {"id": 5}, "from": {"id": 7}, "photo": [{"file_id": "x"}]}}
    check("ignores unsupported (photo) update", telegram.parse_update(photo) is None)
    check("ignores sender-less update", telegram.parse_update({"message": {"chat": {"id": 5}}}) is None)
    check("ignores empty update", telegram.parse_update({}) is None)


def test_voice_mode_prompt():
    print("Scenario: voice flag shortens the prompt for the ear (text path unchanged)")
    from app.prompts import build_system_prompt
    text_p = build_system_prompt("Zain", "member", "Zain (member)")
    voice_p = build_system_prompt("Zain", "member", "Zain (member)", voice=True)
    check("text prompt has no voice hint", "SPOKEN ALOUD" not in text_p)
    check("voice prompt adds the spoken-reply hint", "SPOKEN ALOUD" in voice_p)
    check("voice prompt steers numbers-as-words", "words" in voice_p)


def test_mark_result_parsing():
    print("Scenario: db.mark_dogs_fed result parsing logic")
    # The real query returns command tags like "INSERT 0 1" / "INSERT 0 0".
    check("'INSERT 0 1' => fresh feeding", "INSERT 0 1".endswith("1"))
    check("'INSERT 0 0' => already fed", not "INSERT 0 0".endswith("1"))


async def main():
    await test_status_when_not_fed()
    await test_mark_fed_then_status()
    await test_remember_and_recall_fact()
    await test_recall_empty()
    await test_store_dedup()
    await test_store_guard_invalid_embedding()
    await test_where_is()
    await test_get_calendar()
    await test_calendar_weekday_read()
    await test_calendar_write()
    test_date_resolution()
    await test_calendar_read_guard()
    await test_calendar_not_found_guard()
    await test_shopping_list()
    await test_orders()
    await test_calendar_runs_on_haiku()
    await test_voice_brain_threads_flag()
    await test_empty_reply_fallback()
    await test_tool_failure_degrades()
    await test_tool_loop_terminates()
    await test_tool_dispatch_unknown()
    test_parse_update()
    test_voice_mode_prompt()
    test_tts_model_routing()
    test_grounding_and_dialect_prompt()
    test_mark_result_parsing()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
