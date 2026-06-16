import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, embeddings, calendar_svc, geo
from app.config import settings

log = logging.getLogger("abdo")


# Past this many minutes since the last ping, a live-location share has almost
# certainly stopped (active shares stream every few seconds), so the reading is
# stale: report it in the past tense, not as where the person is right now.
LOCATION_STALE_MINUTES = 15


def _human_age(mins: float) -> str:
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{int(mins)} min ago"
    if mins < 24 * 60:
        return f"~{int(mins // 60)}h ago"
    return f"~{int(mins // (24 * 60))}d ago"


def _describe_location(name, lat, lng, ts) -> str:
    """One self-describing clause per member, with the tense baked in.

    asyncpg returns TIMESTAMPTZ as UTC-aware datetimes; display must be Cairo
    (CLAUDE.md gotcha). We decide fresh-vs-stale here so the model never narrates
    an hours-old reading as where someone is *now* — it just relays our wording.
    Naive inputs (tests) are treated as Cairo-local.
    """
    place = geo.describe(lat, lng)            # "home" or "3.2 km from home"
    tz = ZoneInfo(settings.timezone)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=tz)
    local = ts.astimezone(tz)
    now = datetime.now(tz)
    mins = max(0, (now - local).total_seconds()) / 60
    age = _human_age(mins)
    if mins <= LOCATION_STALE_MINUTES:
        return f"{name} is {place} (live, updated {age})"
    clock = local.strftime("%H:%M" if local.date() == now.date() else "%d %b %H:%M")
    return (f"{name} was {place} as of {clock} ({age}); this reading is stale — "
            f"sharing looks off, so {name} may not be there now")

TOOLS = [
    {
        "name": "get_dog_status",
        "description": (
            "Check whether the household dogs have been fed their meal today. "
            "Use whenever someone asks if the dogs are fed."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mark_dogs_fed",
        "description": (
            "Record that the dogs have been fed today. "
            "Use when someone says they (or someone) fed the dogs."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remember_fact",
        "description": (
            "Store a piece of household knowledge for later recall — a contact number, "
            "the wifi password, where something is kept, a recurring bill, an appliance or "
            "service detail. Use whenever someone tells you a fact about the house to keep."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact as one clear, self-contained sentence."},
                "category": {"type": "string",
                             "enum": ["contact", "wifi", "appliance", "location_of_things",
                                      "schedule", "bill", "misc"]},
            },
            "required": ["content", "category"],
        },
    },
    {
        "name": "recall_facts",
        "description": (
            "Search stored household knowledge to answer a question about the house "
            "(where something is, a number, a password, a bill). Use this before saying you don't know."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look up, in natural language."}},
            "required": ["query"],
        },
    },
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
    {
        "name": "create_event",
        "description": (
            "Add a new event to the shared family calendar. Only call this AFTER the person "
            "has confirmed the details you read back to them. Pass the day the person SAID "
            "(or an exact date from the date reference) and the time separately — never a "
            "date you worked out yourself; Abdo's code turns the day into the real date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "The event title."},
                "day": {"type": "string",
                        "description": ("The day, exactly as said: a weekday name ('Saturday'/'السبت'), "
                                        "'today'/'tomorrow'/'day after tomorrow', 'next <weekday>', OR an "
                                        "exact date copied from the date reference (YYYY-MM-DD). NEVER "
                                        "compute or guess a date yourself.")},
                "time": {"type": "string",
                         "description": "Start time, 24-hour Cairo local, HH:MM, e.g. '19:00'."},
                "end_time": {"type": "string",
                             "description": "Optional end time, HH:MM. Defaults to one hour after start."},
            },
            "required": ["summary", "day", "time"],
        },
    },
    {
        "name": "update_event",
        "description": (
            "Change an existing calendar event (title or time). Get the event's id from "
            "get_calendar first and use it exactly. Only call AFTER the person confirms the "
            "change. To reschedule/move an event, pass the new 'day' and/or 'time' — the event "
            "keeps its original length automatically. Pass 'end_time' ONLY if the person wants "
            "a different end time or duration. Like create_event, pass the spoken day, never a "
            "date you computed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to change (from get_calendar)."},
                "summary": {"type": "string", "description": "New title, if changing it."},
                "day": {"type": "string",
                        "description": ("New day (same rules as create_event's 'day'). Omit if not "
                                        "moving it to another day.")},
                "time": {"type": "string", "description": "New start time, HH:MM. Omit if not changing the time."},
                "end_time": {"type": "string",
                             "description": "New end time, HH:MM. Omit when only moving the event — duration is preserved."},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_event",
        "description": (
            "Delete an event from the shared family calendar. Get the event's id from "
            "get_calendar first. Only call AFTER the person confirms the deletion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to delete (from get_calendar)."},
            },
            "required": ["event_id"],
        },
    },
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
    {
        "name": "add_to_shopping_list",
        "description": (
            "Add an item to the shared household shopping list. Use when someone says "
            "to buy/get something or to put it on the list (e.g. 'add milk', 'we need bread'). "
            "Add one item per call; call it multiple times for several items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "The thing to buy, e.g. 'milk', 'bread'."},
                "qty": {"type": "string", "description": "Optional amount, free text, e.g. '2 kilo', 'a dozen'."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "get_shopping_list",
        "description": (
            "Show the current shared shopping list (items still to buy). Use for "
            "'what's on the list', 'what do we need', 'what should I get'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mark_item_bought",
        "description": (
            "Mark one item on the shopping list as bought, removing it from the open list. "
            "Use when someone says they got/bought a specific item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item": {"type": "string", "description": "The item that was bought."}},
            "required": ["item"],
        },
    },
    {
        "name": "clear_shopping_list",
        "description": (
            "Clear the whole shopping list at once (mark everything bought). Use when someone "
            "says the shopping is done / they got everything. Confirm before clearing."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def _read_calendar(days_ahead: int):
    """Read upcoming events, retrying once on a degraded read. googleapiclient is
    blocking, so each attempt runs off the event loop. A persistent failure raises
    CalendarReadError — the caller turns that into an honest 'try again' and STOPS,
    never falling through to an empty-and-proceed."""
    try:
        return await asyncio.to_thread(calendar_svc.get_events, days_ahead)
    except calendar_svc.CalendarReadError:
        log.warning("calendar read failed; retrying once")
        return await asyncio.to_thread(calendar_svc.get_events, days_ahead)


async def run_tool(name: str, tool_input: dict, member_id: int) -> str:
    if name == "get_dog_status":
        row = await db.dogs_fed_today()
        if row:
            who = row["fed_by_name"] or "someone"
            return f"FED today (by {who})."
        return "NOT fed yet today."

    if name == "mark_dogs_fed":
        fresh = await db.mark_dogs_fed(member_id)
        return "Recorded: dogs fed today." if fresh else "Already marked fed today."

    if name == "remember_fact":
        vec = await embeddings.embed(tool_input["content"], input_type="search_document")
        await db.add_fact(tool_input["category"], tool_input["content"], vec, member_id)
        return "Stored."

    if name == "recall_facts":
        vec = await embeddings.embed(tool_input["query"], input_type="search_query")
        rows = await db.search_facts(vec, k=4)
        if not rows:
            return "No matching household facts found."
        return "\n".join(f"- [{r['category']}] {r['content']}" for r in rows)

    if name == "get_calendar":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        try:
            events = await _read_calendar(tool_input.get("days_ahead", 7))
        except calendar_svc.CalendarReadError as e:
            # A degraded read (boundary / FAILED_PRECONDITION) must NOT be reported
            # as an empty calendar — that's exactly what makes the model create a
            # duplicate of an event it just couldn't see. Stop and be honest.
            log.warning("get_calendar read failed (after retry): %s", e)
            return ("ERROR: couldn't fully reach the calendar right now. Tell the user "
                    "plainly to try again in a moment. Do NOT assume the calendar is "
                    "empty, and do NOT create or change any event based on this.")
        if not events:
            return "No events on the shared calendar in that window."
        return "\n".join(f"- {e['start']}: {e['summary']} (id: {e['id']})" for e in events)

    if name == "create_event":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        ev = await asyncio.to_thread(
            calendar_svc.create_event,
            tool_input["summary"], tool_input["day"], tool_input["time"],
            tool_input.get("end_time"),
        )
        return f"Created: {ev['start']} {ev['summary']} (id: {ev['id']})."

    if name == "update_event":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        try:
            ev = await asyncio.to_thread(
                calendar_svc.update_event,
                tool_input["event_id"], tool_input.get("summary"),
                tool_input.get("day"), tool_input.get("time"), tool_input.get("end_time"),
            )
        except calendar_svc.EventNotFound:
            return (f"ERROR: no event with id '{tool_input['event_id']}' exists. Do NOT "
                    f"invent or guess an id — call get_calendar to get the real one, or "
                    f"tell the user you couldn't find that event.")
        except calendar_svc.CalendarReadError as e:
            log.warning("update_event read failed: %s", e)
            return ("ERROR: couldn't reach the calendar to make that change right now. "
                    "Tell the user to try again in a moment; do NOT claim it was changed.")
        return f"Updated: {ev['summary']} now {ev['start']} to {ev['end']} (id: {ev['id']})."

    if name == "delete_event":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        try:
            await asyncio.to_thread(calendar_svc.delete_event, tool_input["event_id"])
        except calendar_svc.EventNotFound:
            return (f"ERROR: no event with id '{tool_input['event_id']}' exists. Do NOT "
                    f"invent or guess an id — call get_calendar to get the real one, or "
                    f"tell the user you couldn't find that event.")
        except calendar_svc.CalendarReadError as e:
            log.warning("delete_event read failed: %s", e)
            return ("ERROR: couldn't reach the calendar to delete that right now. "
                    "Tell the user to try again in a moment; do NOT claim it was deleted.")
        return "Deleted the event."

    if name == "where_is":
        who = tool_input["name"]
        if who.lower() in ("everyone", "all"):
            rows = await db.get_all_locations()
            if not rows:
                return "No one is sharing their location right now."
            return "\n".join(
                _describe_location(r["name"], r["lat"], r["lng"], r["updated_at"])
                for r in rows
            )
        row = await db.get_location(who)
        if not row:
            return f"{who} isn't sharing a location right now."
        return _describe_location(row["name"], row["lat"], row["lng"], row["updated_at"])

    if name == "add_to_shopping_list":
        added = await db.add_shopping_item(tool_input["item"], tool_input.get("qty"), member_id)
        if added:
            return f"Added '{tool_input['item']}' to the shopping list."
        return f"'{tool_input['item']}' is already on the list."

    if name == "get_shopping_list":
        rows = await db.get_shopping_list()
        if not rows:
            return "The shopping list is empty."
        return "\n".join(
            f"- {r['item']}" + (f" ({r['qty']})" if r["qty"] else "") for r in rows
        )

    if name == "mark_item_bought":
        got = await db.mark_item_bought(tool_input["item"], member_id)
        if got:
            return f"Marked '{got}' as bought (removed from the list)."
        return f"'{tool_input['item']}' isn't on the list."

    if name == "clear_shopping_list":
        n = await db.clear_shopping_list(member_id)
        return f"Cleared {n} item(s) from the shopping list." if n else "The list was already empty."

    return f"Unknown tool: {name}"
