import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import db, embeddings, calendar_svc, geo
from app.config import settings

log = logging.getLogger("abdo")

# A re-stated fact this close (cosine distance) to an existing one is treated as
# the same fact and updated in place, not inserted again — stops the double-save
# (the real fact + a garbled "the wifi password" fragment).
FACT_DEDUP_DISTANCE = 0.15


def event_when(e: dict) -> dict:
    """An event's day and time, resolved in CODE, never by the model — the model
    guessing weekdays once put an event on the wrong day. Start may be an all-day
    date ('2026-06-19') or a timed dateTime; an offset-aware time is converted to
    local time (so a UTC-stamped event can't land on the wrong day or hour).
    Returns {"date": local ISO date, "day": "Friday 16 October 2026", "time": "19:00" | None}."""
    start = e["start"]
    has_time = "T" in start
    try:
        dt = datetime.fromisoformat(start)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo(settings.timezone))
    except ValueError:
        d = date.fromisoformat(start[:10])
        dt = datetime(d.year, d.month, d.day)
    return {"date": dt.date().isoformat(), "day": dt.strftime("%A %d %B %Y"),
            "time": dt.strftime("%H:%M") if has_time else None}


def _format_event(e: dict) -> str:
    w = event_when(e)
    when = w["day"] + (f" at {w['time']}" if w["time"] else " (all day)")
    return f"- {when}: {e['summary']} (id: {e['id']})"


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

    asyncpg returns TIMESTAMPTZ as UTC-aware datetimes; display must be local time
    (CLAUDE.md gotcha). We decide fresh-vs-stale here so the model never narrates
    an hours-old reading as where someone is *now* — it just relays our wording.
    Naive inputs (tests) are treated as local.
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
        "name": "remember_fact",
        "description": (
            "Store a piece of household knowledge for later recall — a contact number, "
            "the wifi password, where something is kept, a recurring bill, an appliance or "
            "service detail. Use whenever someone tells you a fact about the suite to keep."
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
            "Search stored household knowledge to answer a question about the suite "
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
            "Look up upcoming events on the suite's shared calendar. Use for questions like "
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
            "Add a new event to the suite's shared calendar. Only call this AFTER the person "
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
                         "description": "Start time, 24-hour local time, HH:MM, e.g. '19:00'."},
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
            "Delete an event from the suite's shared calendar. Get the event's id from "
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
            "Find where a roommate is and what they're up to: their shared live location AND "
            "their status if they set one. Use for who's home, where is X, what's X doing, is X "
            "busy. Pass 'everyone' for all. Lead with the status when there is one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "A roommate's name, or 'everyone'."}},
            "required": ["name"],
        },
    },
    {
        "name": "add_to_shopping_list",
        "description": (
            "Add an item to the suite's shared shopping list. Use when someone says "
            "to buy/get something or to put it on the list (e.g. 'add milk', 'we need bread'). "
            "Add one item per call; call it multiple times for several items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "The thing to buy, e.g. 'milk', 'bread'."},
                "qty": {"type": "string", "description": "Optional amount, free text, e.g. '2 bags', 'a dozen'."},
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
    {
        "name": "set_reminder",
        "description": (
            "Set a reminder or timer that Abdo sends later as a notification. Use for 'remind me "
            "in 35 minutes', 'timer 10 min', 'remind Sam tomorrow at 9 about the charger'. Pass "
            "EITHER in_minutes, OR a time (HH:MM, 24h) with an optional day exactly as said — never "
            "a computed date. Confirm the resolved time from the result once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind about, short, in the speaker's words."},
                "for": {"type": "string", "description": "A roommate's name if it's for someone else. Omit for the speaker."},
                "in_minutes": {"type": "integer", "description": "For timers: minutes from now."},
                "day": {"type": "string", "description": "The day as said ('today', 'tomorrow', 'Friday', 'next Monday') or an exact date from the reference. Default today."},
                "time": {"type": "string", "description": "Local time, HH:MM 24h, e.g. '21:00'."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List upcoming reminders (with ids), for everyone or one roommate.",
        "input_schema": {
            "type": "object",
            "properties": {"for": {"type": "string", "description": "Only this roommate's reminders (optional)."}},
            "required": [],
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a reminder by its id from list_reminders. Never guess an id.",
        "input_schema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
    },
    {
        "name": "set_status",
        "description": (
            "Set the speaker's status so the suite knows, e.g. 'in a midterm', 'studying, quiet "
            "please', 'away in Montreal'. Optional end: until_day and/or until_time (HH:MM) as said. "
            "If they're away in another city, pass it as `place`: until the status ends, their "
            "prayer times and briefing weather follow that city (travel mode)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The status, short."},
                "place": {"type": "string", "description": "A city if they're travelling, e.g. 'Montreal' (optional)."},
                "until_day": {"type": "string", "description": "When it ends, the day as said (optional)."},
                "until_time": {"type": "string", "description": "When it ends, HH:MM 24h (optional)."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "clear_status",
        "description": "Clear the speaker's status (they're back / done).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_prayer_times",
        "description": "Prayer times for a day (default today), computed for the suite's location.",
        "input_schema": {
            "type": "object",
            "properties": {"day": {"type": "string", "description": "The day as said (optional, default today)."}},
            "required": [],
        },
    },
    {
        "name": "get_briefing",
        "description": (
            "The speaker's daily briefing: weather, today's calendar, their packages, groceries, "
            "roommates' statuses, and remaining prayer times. Use for 'what's my day look like?'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "add_expense",
        "description": (
            "Record a shared expense, e.g. 'I paid $42 for groceries'. Split equally with everyone "
            "by default; use split_with to name exactly who shares it, or exclude to leave people "
            "out. Only call AFTER reading back the amount, payer, and split once and getting a yes. "
            "Never compute shares yourself: the result has them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What it was for, short."},
                "amount": {"type": "string", "description": "The total, e.g. '42.50' (dollars)."},
                "paid_by": {"type": "string", "description": "Who paid (a roommate's name). Omit if the speaker paid."},
                "split_with": {"type": "array", "items": {"type": "string"},
                               "description": "Exactly who shares it (names; include the payer if they share). Omit for everyone."},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "People to leave out of an everyone-split (names)."},
                "day": {"type": "string", "description": "When it was spent, as said (default today)."},
            },
            "required": ["description", "amount"],
        },
    },
    {
        "name": "get_balances",
        "description": "Who owes whom in the suite right now, and the fewest payments to settle up.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "record_payment",
        "description": (
            "Record money paid between roommates outside an expense, e.g. 'I paid Mo back $20' "
            "(settling up). Confirm once before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Who received the money."},
                "amount": {"type": "string", "description": "e.g. '20' or '12.50'."},
                "from": {"type": "string", "description": "Who paid it. Omit if the speaker paid."},
            },
            "required": ["to", "amount"],
        },
    },
    {
        "name": "list_expenses",
        "description": "Recent shared expenses with ids, amounts, who paid, and the split.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many (default 10)."}},
            "required": [],
        },
    },
    {
        "name": "delete_expense",
        "description": "Delete a mistaken expense by its id from list_expenses (only whoever paid or recorded it).",
        "input_schema": {
            "type": "object",
            "properties": {"expense_id": {"type": "integer"}},
            "required": ["expense_id"],
        },
    },
    {
        "name": "add_package",
        "description": (
            "Record an incoming package/delivery for someone in the suite, so everyone knows "
            "what's coming and whose it is. Use when someone says a package or order is on its way."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What it is, e.g. 'desk lamp'."},
                "owner": {"type": "string",
                          "description": "Whose package it is (a roommate's name). Omit if it's the speaker's own."},
                "carrier": {"type": "string", "description": "Carrier or store if mentioned, e.g. 'Amazon', 'Canada Post'."},
                "day": {"type": "string",
                        "description": ("When it's expected, exactly as said: a weekday, 'today'/'tomorrow', "
                                        "or an exact date from the date reference. Omit if unknown. "
                                        "Resolved in code — never compute a date yourself.")},
            },
            "required": ["description"],
        },
    },
    {
        "name": "get_packages",
        "description": (
            "List the suite's open packages (still expected, or arrived but not picked up), with "
            "whose each is and its id. Use when someone asks about packages or deliveries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"owner": {"type": "string", "description": "Only this roommate's packages (optional)."}},
            "required": [],
        },
    },
    {
        "name": "update_package",
        "description": (
            "Change an open package's status: 'arrived' (it's at the building), 'picked_up' (someone "
            "has it), or 'cancelled'. Use the package id from get_packages — never a guessed one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "package_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["arrived", "picked_up", "cancelled"]},
            },
            "required": ["package_id", "status"],
        },
    },
]


async def read_calendar(days_ahead: int):
    """Read upcoming events, retrying once on a degraded read. googleapiclient is
    blocking, so each attempt runs off the event loop. A persistent failure raises
    CalendarReadError — the caller turns that into an honest 'try again' and STOPS,
    never falling through to an empty-and-proceed."""
    try:
        return await asyncio.to_thread(calendar_svc.get_events, days_ahead)
    except calendar_svc.CalendarReadError:
        log.warning("calendar read failed; retrying once")
        return await asyncio.to_thread(calendar_svc.get_events, days_ahead)


# --- time resolution for reminders and statuses (shared with the web API) ----------
# Same rule as the calendar: the model passes what the person said ("in 35 minutes",
# "tomorrow" + "09:00"); the real local datetime is computed here, never by the model.

class TimeError_(ValueError):
    """A spoken time we can't turn into a real, future moment. The message is safe to show."""


def resolve_due(in_minutes=None, day: str | None = None, time: str | None = None,
                now: datetime | None = None) -> datetime:
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    if in_minutes is not None and (day or time):
        raise TimeError_("give either a number of minutes or a day/time, not both")
    if in_minutes is not None:
        try:
            minutes = int(in_minutes)
        except (TypeError, ValueError):
            raise TimeError_(f"'{in_minutes}' isn't a number of minutes")
        if not 0 < minutes <= 7 * 24 * 60:
            raise TimeError_("a timer has to be between 1 minute and 7 days")
        return now + timedelta(minutes=minutes)
    if not time:
        raise TimeError_("I need either 'in N minutes' or a time (HH:MM)")
    try:
        d = calendar_svc.resolve_date(day or "today", now=now)
        h, m = calendar_svc._parse_time(time)
        due = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    except (ValueError, IndexError):
        raise TimeError_(f"couldn't understand '{day or 'today'} {time}'")
    if due <= now:
        raise TimeError_(f"{due.strftime('%A %H:%M')} has already passed; ask if they mean a later day")
    return due


def resolve_until(day: str | None = None, time: str | None = None,
                  now: datetime | None = None) -> datetime | None:
    """When a status ends: None = until cleared; a day alone = the end of that day."""
    if not day and not time:
        return None
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    if time:
        return resolve_due(day=day, time=time, now=now)
    try:
        d = calendar_svc.resolve_date(day, now=now)
    except ValueError:
        raise TimeError_(f"couldn't understand the day '{day}'")
    until = datetime(d.year, d.month, d.day, 23, 59, tzinfo=tz)
    if until <= now:
        raise TimeError_("that day has already passed")
    return until


def _when(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(settings.timezone)).strftime("%A %d %B at %H:%M")


async def _money_tool(name: str, tool_input: dict, member_id: int | None) -> str:
    from app import money

    async def who(person: str | None, default=member_id):
        if not person:
            return default
        mid = await db.get_member_id_by_name(person)
        if mid is None:
            raise money.MoneyError(f"nobody in the suite is called '{person}'")
        return mid

    names = {m["id"]: m["name"] for m in await db.all_members()}
    try:
        if name == "add_expense":
            amount = money.parse_amount(tool_input["amount"])
            payer = await who(tool_input.get("paid_by"))
            if payer is None:
                return "ERROR: couldn't tell who paid. Ask the user."
            if tool_input.get("split_with"):
                among = [await who(n) for n in tool_input["split_with"]]
            else:
                among = list(names)
            for n in tool_input.get("exclude") or []:
                ex = await who(n)
                among = [m for m in among if m != ex]
            shares = money.split(amount, among)
            try:
                day = calendar_svc.resolve_date(tool_input.get("day") or "today")
            except ValueError:
                return f"ERROR: couldn't understand the day '{tool_input.get('day')}'. Ask the user."
            eid = await db.add_expense(tool_input["description"].strip(), amount, payer, member_id, day, shares)
            parts = ", ".join(f"{names[m]} {money.fmt(c)}" for m, c in shares.items())
            return (f"Recorded expense #{eid}: {tool_input['description'].strip()}, {money.fmt(amount)} "
                    f"paid by {names[payer]}, split: {parts}.")

        if name == "record_payment":
            amount = money.parse_amount(tool_input["amount"])
            frm = await who(tool_input.get("from"))
            to = await who(tool_input["to"], default=None)
            if frm is None or to is None:
                return "ERROR: couldn't tell who paid whom. Ask the user."
            if frm == to:
                return "ERROR: a payment needs two different people."
            sid = await db.add_settlement(frm, to, amount, member_id,
                                          datetime.now(ZoneInfo(settings.timezone)).date())
            return f"Recorded payment #{sid}: {names[frm]} paid {names[to]} {money.fmt(amount)}."
    except money.MoneyError as e:
        return f"ERROR: {e}. Ask the user; do NOT guess."

    if name == "get_balances":
        expenses, shares, settlements = await db.money_ledger()
        net = money.balances(list(names), expenses, shares, settlements)
        lines = [f"- {names[m]}: " + ("all square" if v == 0 else
                                       f"is owed {money.fmt(v)}" if v > 0 else f"owes {money.fmt(v)}")
                 for m, v in net.items()]
        plan = money.settle_up(net)
        lines.append("To settle up: " + ("; ".join(f"{names[f]} pays {names[t]} {money.fmt(c)}"
                                                   for f, t, c in plan) if plan else "nothing to do."))
        return "\n".join(lines)

    if name == "list_expenses":
        rows = await db.recent_expenses(int(tool_input.get("limit") or 10))
        if not rows:
            return "No expenses recorded yet."
        return "\n".join(
            f"- #{r['id']} {r['spent_on'].strftime('%a %d %b')}: {r['description']}, {money.fmt(r['amount_cents'])} "
            f"paid by {r['paid_by_name']}, split {', '.join(s['name'] for s in r['shares'])}" for r in rows)

    if name == "delete_expense":
        desc = await db.delete_expense(tool_input["expense_id"], member_id)
        if desc is None:
            return (f"ERROR: no expense #{tool_input['expense_id']} that this person paid or recorded. "
                    f"Do NOT claim it was deleted; call list_expenses for real ids.")
        return f"Deleted expense #{tool_input['expense_id']} ({desc})."
    return f"Unknown tool: {name}"


def _package_line(r) -> str:
    who = r["owner_name"] or "someone"
    what = r["description"] + (f" ({r['carrier']})" if r["carrier"] else "")
    if r["status"] == "arrived":
        state = "ARRIVED, waiting to be picked up"
    elif r["expected_on"]:
        state = f"expected {r['expected_on'].strftime('%A %d %B')}"
    else:
        state = "expected (no date given)"
    return f"- #{r['id']} {what} for {who}: {state}"


async def run_tool(name: str, tool_input: dict, member_id: int | None) -> str:
    """Run one client-side tool. member_id is the speaker (None if unidentified)."""
    if name == "remember_fact":
        content, category = tool_input["content"], tool_input["category"]
        vec = await embeddings.embed(content, input_type="search_document")
        if not embeddings.is_valid(vec):
            # Never store a fact with a junk embedding — that's the silent recall
            # killer. Fail honestly so the model tells the user instead of "saved".
            log.error("remember_fact: invalid embedding (dim=%s) for %r",
                      len(vec) if hasattr(vec, "__len__") else "?", content)
            return ("ERROR: couldn't store that right now (embedding failed). Tell the "
                    "user plainly it wasn't saved and to try again; do NOT claim it was saved.")
        # Dedup: if a near-identical fact already exists, update it in place rather
        # than inserting a second row (the wifi double-save).
        near = await db.nearest_fact(vec)
        if near and near["distance"] is not None and near["distance"] < FACT_DEDUP_DISTANCE:
            await db.update_fact(near["id"], category, content, vec)
            return "Updated the existing fact (it was already on file)."
        await db.add_fact(category, content, vec, member_id)
        return "Stored."

    if name == "recall_facts":
        query = tool_input["query"]
        log.info("recall_facts: query=%r", query)
        vec = await embeddings.embed(query, input_type="search_query")
        rows = await db.search_facts(vec, k=4)
        if not rows:
            # Visible in Railway logs: 0 rows means either the store is empty or a
            # query embedding/encoding problem — not the old is_valid drop bug.
            log.info("recall_facts: 0 rows for query=%r", query)
            return "No matching household facts found."
        top = rows[0]
        log.info("recall_facts: %d row(s) for query=%r; top id=%s distance=%.4f",
                 len(rows), query, top["id"], top["distance"])
        return "\n".join(f"- [{r['category']}] {r['content']}" for r in rows)

    if name == "get_calendar":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        try:
            events = await read_calendar(tool_input.get("days_ahead", 7))
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
        return "\n".join(_format_event(e) for e in events)

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
        # "Where's X?" is usually answered better by X's status ("in a midterm till 3")
        # than by a map pin, so the status rides along with the location, in code. The
        # model once ignored a status sitting in its context and said "not sharing location".
        who = tool_input["name"]
        tz = ZoneInfo(settings.timezone)
        statuses = {s["name"].lower(): s for s in await db.current_statuses()}

        def status_of(person: str) -> str:
            s = statuses.get(person.lower())
            if not s:
                return ""
            until = f" (until {s['until_at'].astimezone(tz).strftime('%a %H:%M')})" if s["until_at"] else ""
            return f"{s['name']}'s status: {s['text']}{until}"

        if who.lower() in ("everyone", "all"):
            rows = await db.get_all_locations()
            lines = [_describe_location(r["name"], r["lat"], r["lng"], r["updated_at"])
                     + (f"; {status_of(r['name'])}" if status_of(r["name"]) else "") for r in rows]
            sharing = {r["name"].lower() for r in rows}
            lines += [status_of(s["name"]) + " (not sharing location)"
                      for k, s in statuses.items() if k not in sharing]
            return "\n".join(lines) if lines else "No one is sharing their location or a status right now."
        row = await db.get_location(who)
        status = status_of(row["name"] if row else who)
        if not row:
            if status:
                return f"{status}. (Not sharing a live location.)"
            return f"{who} isn't sharing a location and hasn't set a status."
        place = _describe_location(row["name"], row["lat"], row["lng"], row["updated_at"])
        return f"{place}; {status}" if status else place

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

    if name == "add_package":
        expected_on = None
        if tool_input.get("day"):
            try:
                expected_on = calendar_svc.resolve_date(tool_input["day"])
            except ValueError:
                return (f"ERROR: couldn't understand the day '{tool_input['day']}'. Ask the user "
                        f"which day (a weekday, today/tomorrow, or a date); do NOT guess one.")
        owner_id, owner_label = member_id, "the speaker"
        if tool_input.get("owner"):
            owner_id = await db.get_member_id_by_name(tool_input["owner"])
            if owner_id is None:
                return (f"ERROR: nobody in the suite is called '{tool_input['owner']}'. Ask whose "
                        f"package it is; do NOT record it under a guessed name.")
            owner_label = tool_input["owner"]
        pid = await db.add_package(tool_input["description"], tool_input.get("carrier"),
                                   owner_id, expected_on)
        when = f"expected {expected_on.strftime('%A %d %B')}" if expected_on else "no expected date"
        return f"Recorded package #{pid}: {tool_input['description']} for {owner_label}, {when}."

    if name == "get_packages":
        owner_id = None
        if tool_input.get("owner"):
            owner_id = await db.get_member_id_by_name(tool_input["owner"])
            if owner_id is None:
                return f"ERROR: nobody in the suite is called '{tool_input['owner']}'."
        rows = await db.get_open_packages(owner_id)
        if not rows:
            return "No open packages." if owner_id is None else \
                f"No open packages for {tool_input['owner']}."
        return "\n".join(_package_line(r) for r in rows)

    if name == "update_package":
        status = tool_input["status"]
        if status not in ("arrived", "picked_up", "cancelled"):
            return f"ERROR: unknown status '{status}'. Use arrived, picked_up, or cancelled."
        desc = await db.update_package_status(tool_input["package_id"], status)
        if desc is None:
            return (f"ERROR: no open package #{tool_input['package_id']}. Do NOT claim it "
                    f"changed; call get_packages to see the real ids.")
        return f"Package #{tool_input['package_id']} ({desc}) is now {status.replace('_', ' ')}."

    if name == "set_reminder":
        text = (tool_input.get("text") or "").strip()
        if not text:
            return "ERROR: what should the reminder say? Ask the user."
        target_id, target = member_id, "the speaker"
        if tool_input.get("for"):
            target_id = await db.get_member_id_by_name(tool_input["for"])
            if target_id is None:
                return (f"ERROR: nobody in the suite is called '{tool_input['for']}'. Ask who it's "
                        f"for; do NOT set it under a guessed name.")
            target = tool_input["for"]
        if target_id is None:
            return "ERROR: couldn't tell who this reminder is for. Ask the user."
        try:
            due = resolve_due(tool_input.get("in_minutes"), tool_input.get("day"), tool_input.get("time"))
        except TimeError_ as e:
            return f"ERROR: {e}. Ask the user; do NOT guess a time."
        rid = await db.add_reminder(target_id, text, due, member_id)
        return f"Reminder #{rid} set for {target}: '{text}', {_when(due)} (local time)."

    if name == "list_reminders":
        who = None
        if tool_input.get("for"):
            who = await db.get_member_id_by_name(tool_input["for"])
            if who is None:
                return f"ERROR: nobody in the suite is called '{tool_input['for']}'."
        rows = await db.upcoming_reminders(who)
        if not rows:
            return "No upcoming reminders."
        return "\n".join(
            f"- #{r['id']} for {r['member_name']}: '{r['text']}', {_when(r['due_at'])}"
            + (f" (set by {r['created_by_name']})" if r["created_by_name"] and r["created_by"] != r["member_id"] else "")
            for r in rows)

    if name == "cancel_reminder":
        text = await db.cancel_reminder(tool_input["reminder_id"], member_id)
        if text is None:
            return (f"ERROR: no pending reminder #{tool_input['reminder_id']} for or by this person. "
                    f"Do NOT claim it was cancelled; call list_reminders for real ids.")
        return f"Cancelled reminder #{tool_input['reminder_id']} ('{text}')."

    if name == "set_status":
        if member_id is None:
            return "ERROR: couldn't tell whose status this is. Ask the user."
        try:
            until = resolve_until(tool_input.get("until_day"), tool_input.get("until_time"))
        except TimeError_ as e:
            return f"ERROR: {e}. Ask the user."
        place = None
        if tool_input.get("place"):
            from app import location
            try:
                place = await location.geocode(tool_input["place"])
            except location.PlaceNotFound as e:
                return f"ERROR: {e}. Ask which city they mean."
            except Exception:
                log.exception("geocoding failed")
                return "ERROR: couldn't look up that place right now. Set the status without it or try again."
        await db.set_status(member_id, tool_input["text"].strip(), until, place)
        out = f"Status set: '{tool_input['text'].strip()}'" + (f" until {_when(until)}." if until else " (until cleared).")
        if place:
            out += (f" Travel mode: {place['label']}. Their prayer times and briefing weather follow "
                    f"it until the status ends. Confirm the city with them.")
        return out

    if name == "clear_status":
        if member_id is None:
            return "ERROR: couldn't tell whose status this is."
        return "Status cleared." if await db.clear_status(member_id) else "There was no status to clear."

    if name == "get_prayer_times":
        from app import location, prayer
        if not prayer.is_configured():
            return "Prayer times aren't set up (the home location is missing)."
        try:
            day = calendar_svc.resolve_date(tool_input.get("day") or "today")
        except ValueError:
            return f"ERROR: couldn't understand the day '{tool_input.get('day')}'. Ask which day."
        loc = await location.location_for(member_id) if member_id else location.home()
        where = "" if loc is None or loc.is_home else f" in {loc.label} (the speaker's travel location)"
        return f"Prayer times{where} for {day.strftime('%A %d %B')}:\n{prayer.describe_day(day, loc)}"

    if name in ("add_expense", "get_balances", "record_payment", "list_expenses", "delete_expense"):
        return await _money_tool(name, tool_input, member_id)

    if name == "get_briefing":
        from app import briefing   # imported here: briefing imports this module
        member = await db.get_member(member_id) if member_id else None
        if member is None:
            return "ERROR: couldn't tell who's asking."
        title, body = await briefing.build(member)
        return f"{title}\n{body}"

    return f"Unknown tool: {name}"
