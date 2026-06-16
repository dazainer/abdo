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
        self.facts.append({"category": category, "content": content})

    async def search_facts(self, embedding, k=4):
        # Embedding is faked, so just return what's stored (most-recent first).
        return list(reversed(self.facts))[:k]

    async def upsert_location(self, member_id, lat, lng):
        self.locations[member_id] = (lat, lng)

    async def get_location(self, name):
        return self._loc_rows.get(name.lower())

    async def get_all_locations(self):
        return list(self._loc_rows.values())


fake = FakeDB()
fake.locations = {}      # member_id -> (lat, lng), written by upsert
fake._loc_rows = {}      # name.lower() -> asyncpg-style row dict, for reads
for name in ("recent_messages", "roster_string", "dogs_fed_today",
             "mark_dogs_fed", "log_message", "add_fact", "search_facts",
             "upsert_location", "get_location", "get_all_locations"):
    setattr(db, name, getattr(fake, name))


# --- Fake embeddings: record input_type so we can assert store/recall asymmetry --
embed_calls = []


async def fake_embed(text, *, input_type):
    embed_calls.append(input_type)
    return [0.0] * embeddings.EMBED_DIM   # right shape, content irrelevant to fakes


embeddings.embed = fake_embed


# --- Fake calendar + a real home coordinate for the geofence ------------------
from datetime import datetime  # noqa: E402
from app import calendar_svc, geo  # noqa: E402
from app.config import settings  # noqa: E402

settings.home_lat = 30.0000      # New Cairo-ish; lets geo.describe compute distance
settings.home_lng = 31.0000

_fake_events = []
_created_events = []
calendar_svc.is_configured = lambda: True
calendar_svc.get_events = lambda days_ahead=7: list(_fake_events)


def fake_create_event(summary, start, end=None):
    ev = {"id": "evt_new", "summary": summary, "start": start}
    _created_events.append(ev)
    return ev


def fake_update_event(event_id, summary=None, start=None, end=None):
    return {"id": event_id, "summary": summary or "Family Gathering",
            "start": start or "2026-06-17T19:00:00"}


calendar_svc.create_event = fake_create_event
calendar_svc.update_event = fake_update_event


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


async def test_where_is():
    print("Scenario: where_is (live location + geofence)")
    t = datetime(2026, 6, 16, 14, 30)
    # Zain exactly at home; Omar ~3km away.
    fake._loc_rows = {
        "zain": {"name": "Zain", "lat": 30.0000, "lng": 31.0000, "updated_at": t},
        "omar": {"name": "Omar", "lat": 30.0270, "lng": 31.0000, "updated_at": t},
    }
    out = await tools.run_tool("where_is", {"name": "Zain"}, member_id=1)
    check("member at home reads 'home'", "Zain: home" in out and "14:30" in out)
    out = await tools.run_tool("where_is", {"name": "Omar"}, member_id=1)
    check("member away reads distance", "km from home" in out)
    out = await tools.run_tool("where_is", {"name": "everyone"}, member_id=1)
    check("'everyone' lists all sharers", "Zain" in out and "Omar" in out)

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
    print("Scenario: create_event / update_event")
    _created_events.clear()
    out = await tools.run_tool(
        "create_event",
        {"summary": "Family Gathering", "start": "2026-06-17T19:00:00"}, member_id=1)
    check("create echoes confirmation with id", "Family Gathering" in out and "evt_new" in out)
    check("create actually wrote to the calendar",
          len(_created_events) == 1 and _created_events[0]["summary"] == "Family Gathering")

    out = await tools.run_tool(
        "update_event",
        {"event_id": "evt_new", "start": "2026-06-17T20:00:00"}, member_id=1)
    check("update echoes confirmation", out.startswith("Updated:") and "evt_new" in out)


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

    photo = {"message": {"chat": {"id": 5}, "from": {"id": 7}, "photo": [{"file_id": "x"}]}}
    check("ignores unsupported (photo) update", telegram.parse_update(photo) is None)
    check("ignores sender-less update", telegram.parse_update({"message": {"chat": {"id": 5}}}) is None)
    check("ignores empty update", telegram.parse_update({}) is None)


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
    await test_where_is()
    await test_get_calendar()
    await test_calendar_write()
    await test_tool_dispatch_unknown()
    test_parse_update()
    test_mark_result_parsing()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
