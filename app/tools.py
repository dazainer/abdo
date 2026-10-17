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
                "urgent": {"type": "boolean",
                           "description": ("True when they stress it matters (ضروري، مهم قوي/جداً، "
                                           "urgent, don't let me forget): it repeats up to 3 times, 2 "
                                           "minutes apart, until they tap it or message you.")},
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
        "name": "who_is_free",
        "description": (
            "When the roommates are free on a day, from their class timetables, exams, and who's "
            "away. Use for 'when are we all free tonight?', 'is Mo free at 3?', 'can we do dinner "
            "Friday?'. Times are computed in code: report them, never estimate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "The day as said (default today)."},
                "from_time": {"type": "string", "description": "Window start HH:MM (optional; 'tonight' ≈ 17:00)."},
                "to_time": {"type": "string", "description": "Window end HH:MM (optional, default 23:00)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_schedule",
        "description": "A roommate's classes and exams on a day (default: the speaker, today).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Whose schedule (omit for the speaker)."},
                "day": {"type": "string", "description": "The day as said (default today)."},
            },
            "required": [],
        },
    },
    {
        "name": "add_exam",
        "description": (
            "Add a quiz, midterm, or final so the suite knows (it's shared with everyone). Pass the "
            "day as said and the time HH:MM. Confirm the resolved details once before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course": {"type": "string", "description": "e.g. 'MATH 136'."},
                "kind": {"type": "string", "enum": ["quiz", "midterm", "final"]},
                "day": {"type": "string", "description": "The day as said, or an exact date from the reference."},
                "time": {"type": "string", "description": "Start, HH:MM 24h."},
                "end_time": {"type": "string", "description": "End HH:MM (optional)."},
                "room": {"type": "string", "description": "Optional."},
                "for": {"type": "string", "description": "Whose exam, if not the speaker's."},
            },
            "required": ["course", "kind", "day", "time"],
        },
    },
    {
        "name": "list_exams",
        "description": "Upcoming quizzes/midterms/finals (next 30 days) for everyone or one roommate, with ids.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Only this roommate's (optional)."}},
            "required": [],
        },
    },
    {
        "name": "delete_exam",
        "description": "Remove an exam by id from list_exams (only whoever it's for or who added it).",
        "input_schema": {"type": "object", "properties": {"exam_id": {"type": "integer"}}, "required": ["exam_id"]},
    },
    {
        "name": "pause_classes",
        "description": (
            "The speaker's classes don't meet for a stretch of days (reading week, a holiday, "
            "'skip my lectures this week'): every class from from_day to to_day (inclusive) is "
            "dropped from their schedule, free-time answers and briefing. Exams still count. Whole "
            "days only. Pass the days as said or as exact dates from the date reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_day": {"type": "string"},
                "to_day": {"type": "string", "description": "Last day off; omit for a single day."},
                "label": {"type": "string", "description": "e.g. 'Reading week'."},
            },
            "required": ["from_day"],
        },
    },
    {
        "name": "list_class_breaks",
        "description": "The speaker's class breaks that haven't ended yet, with ids.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "resume_classes",
        "description": "Remove one of the speaker's class breaks by id (from list_class_breaks), so those classes count again.",
        "input_schema": {"type": "object", "properties": {"break_id": {"type": "integer"}}, "required": ["break_id"]},
    },
    {
        "name": "add_to_fridge",
        "description": (
            "Note something in the fridge/cupboard, optionally with when it expires, e.g. 'we bought "
            "milk, expires Friday', 'my yogurt goes off in 3 days'. Shared by default; mine=true if "
            "it's the speaker's own. Pass the expiry day as said, never a computed date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "expires_day": {"type": "string", "description": "Expiry day as said ('Friday', 'tomorrow'), optional."},
                "expires_in_days": {"type": "integer", "description": "Or: expires in N days, optional."},
                "mine": {"type": "boolean", "description": "True if it's the speaker's own, not shared."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "list_fridge",
        "description": "What's in the fridge (with ids and expiry, soonest first). Use for 'what's in the fridge', 'anything going off?'.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fridge_item_done",
        "description": (
            "Mark a fridge item finished or thrown out, by its id from list_fridge. rebuy=true also "
            "adds it to the shopping list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "integer"}, "rebuy": {"type": "boolean"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "going_shopping",
        "description": (
            "The speaker is heading out to shop: notify the other roommates so they can add to the "
            "list, and return the current list. Use for 'I'm going to Walmart', 'heading to the store'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"store": {"type": "string", "description": "Where, if said (e.g. 'Walmart')."}},
            "required": [],
        },
    },
    {
        "name": "create_poll",
        "description": (
            "Start a poll for the suite (single choice, 2-6 options), e.g. 'where are we eating: "
            "shawarma, pizza, or sushi?'. Everyone else gets notified. Confirm the question and options once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question", "options"],
        },
    },
    {
        "name": "vote",
        "description": (
            "Vote in a poll, change a vote, or remove it: the option as said or its number, or "
            "remove=true to take the vote back. Defaults to the newest open poll; pass poll_id if there are several."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"option": {"type": "string"}, "poll_id": {"type": "integer"},
                           "remove": {"type": "boolean", "description": "Take back the speaker's vote."}},
            "required": [],
        },
    },
    {
        "name": "poll_results",
        "description": "Open polls (and ones closed in the last few days) with votes, who voted what, and who hasn't voted.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "close_poll",
        "description": (
            "Close a poll (only whoever asked it), or delete=true to remove it for everyone. "
            "Defaults to their newest poll."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"poll_id": {"type": "integer"}, "delete": {"type": "boolean"}},
            "required": [],
        },
    },
    {
        "name": "next_bus",
        "description": (
            "Next buses/ION for the suite's saved trips, from GRT (when each leaves, when it gets "
            "there, whether it's live or just scheduled). trip: a saved trip as said (e.g. 'groceries', "
            "'mall', 'mall home'). route: a route number to filter ('301' is the ION). Omit trip for "
            "the stop by home. Never state a bus time this tool didn't return."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"trip": {"type": "string"}, "route": {"type": "string"}},
            "required": [],
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


# The app's screens (Today, the briefing) read the same 7-day window over and over; a
# Google round-trip is the slowest part of opening Today. They may reuse a read for a
# minute. Abdo's own calendar tools always read fresh, and every write clears this.
CALENDAR_CACHE_SECONDS = 60
_calendar_cache: dict[int, tuple[float, list]] = {}


def invalidate_calendar_cache() -> None:
    _calendar_cache.clear()


async def read_calendar(days_ahead: int, *, cached: bool = False):
    """Read upcoming events, retrying once on a degraded read. googleapiclient is
    blocking, so each attempt runs off the event loop. A persistent failure raises
    CalendarReadError — the caller turns that into an honest 'try again' and STOPS,
    never falling through to an empty-and-proceed. A failed read is never cached."""
    import time as _t
    hit = _calendar_cache.get(days_ahead) if cached else None
    if hit and _t.monotonic() - hit[0] < CALENDAR_CACHE_SECONDS:
        return hit[1]
    try:
        events = await asyncio.to_thread(calendar_svc.get_events, days_ahead)
    except calendar_svc.CalendarReadError:
        log.warning("calendar read failed; retrying once")
        events = await asyncio.to_thread(calendar_svc.get_events, days_ahead)
    _calendar_cache[days_ahead] = (_t.monotonic(), events)
    return events


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


async def _schedule_tool(name: str, tool_input: dict, member_id: int | None) -> str:
    from app import timetable
    tz = ZoneInfo(settings.timezone)

    def day_of(raw):
        return calendar_svc.resolve_date(raw or "today")

    try:
        if name == "who_is_free":
            day = day_of(tool_input.get("day"))
            frm = time_of(tool_input.get("from_time"))
            to = time_of(tool_input.get("to_time"))
            return timetable.describe_free(await timetable.who_is_free(day, frm, to))

        if name == "get_schedule":
            who = member_id
            if tool_input.get("name"):
                who = await db.get_member_id_by_name(tool_input["name"])
                if who is None:
                    return f"ERROR: nobody in the suite is called '{tool_input['name']}'."
            if who is None:
                return "ERROR: whose schedule? Ask the user."
            day = day_of(tool_input.get("day"))
            start = datetime.combine(day, datetime.min.time(), tz)
            classes = await db.classes_for(who)
            breaks = await db.class_breaks(who, since=day)
            busy = timetable.busy_on(day, classes, await db.exams_between(start, start + timedelta(days=1), who), tz, breaks)
            label = day.strftime("%A %d %B")
            off = timetable.on_break(day, who, breaks)
            if off and not busy:
                return f"No classes on {label} ({off['label']})."
            if not classes and not busy:
                return f"No timetable on file and nothing scheduled for {label}."
            if not busy:
                return f"Nothing scheduled on {label}."
            return f"{label}:\n" + "\n".join(f"- {b.start.strftime('%H:%M')}–{b.end.strftime('%H:%M')} {b.what}" for b in busy)

        if name == "add_exam":
            kind = (tool_input.get("kind") or "").lower()
            if kind not in timetable.KINDS:
                return "ERROR: an exam is a quiz, midterm, or final."
            who = member_id
            if tool_input.get("for"):
                who = await db.get_member_id_by_name(tool_input["for"])
                if who is None:
                    return f"ERROR: nobody in the suite is called '{tool_input['for']}'."
            if who is None:
                return "ERROR: whose exam? Ask the user."
            d = day_of(tool_input["day"])
            h, m = calendar_svc._parse_time(tool_input["time"])
            starts = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            ends = None
            if tool_input.get("end_time"):
                eh, em = calendar_svc._parse_time(tool_input["end_time"])
                ends = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz)
                if ends <= starts:
                    return "ERROR: the exam ends before it starts. Ask the user."
            eid = await db.add_exam(who, tool_input["course"].strip().upper(), kind, starts, ends,
                                    (tool_input.get("room") or "").strip() or None, member_id)
            return f"Added exam #{eid}: {tool_input['course'].strip().upper()} {kind}, {_when(starts)}."

        if name == "list_exams":
            who = None
            if tool_input.get("name"):
                who = await db.get_member_id_by_name(tool_input["name"])
                if who is None:
                    return f"ERROR: nobody in the suite is called '{tool_input['name']}'."
            now = datetime.now(tz)
            rows = await db.exams_between(now - timedelta(hours=3), now + timedelta(days=30), who)
            if not rows:
                return "No exams in the next 30 days."
            return "\n".join(f"- #{r['id']} {r['name']}: {r['course']} {r['kind']}, {_when(r['starts_at'])}"
                             + (f", {r['room']}" if r["room"] else "") for r in rows)

        if name == "pause_classes":
            if member_id is None:
                return "ERROR: couldn't tell whose classes. Ask the user."
            first = day_of(tool_input["from_day"])
            last = day_of(tool_input["to_day"]) if tool_input.get("to_day") else first
            if last < first:
                return "ERROR: the last day is before the first. Ask the user which days."
            if (last - first).days > 120:
                return "ERROR: that's longer than a term. Ask the user which days."
            label = (tool_input.get("label") or "Break").strip()[:60] or "Break"
            bid = await db.add_class_break(member_id, first, last, label)
            span = first.strftime("%A %d %B") + ("" if last == first else f" to {last.strftime('%A %d %B')}")
            return f"Break #{bid} ({label}): no classes {span}. Exams still count."

        if name == "list_class_breaks":
            rows = await db.class_breaks(member_id, since=datetime.now(tz).date())
            if not rows:
                return "No class breaks coming up."
            return "\n".join(f"- #{r['id']} {r['label']}: {r['starts_on'].strftime('%a %d %b')} to "
                             f"{r['ends_on'].strftime('%a %d %b')}" for r in rows)

        if name == "resume_classes":
            if not await db.delete_class_break(tool_input["break_id"], member_id):
                return (f"ERROR: no break #{tool_input['break_id']} for this person. Do NOT claim it was "
                        f"removed; call list_class_breaks for real ids.")
            return f"Removed break #{tool_input['break_id']}; those classes count again."

        if name == "delete_exam":
            course = await db.delete_exam(tool_input["exam_id"], member_id)
            if course is None:
                return (f"ERROR: no exam #{tool_input['exam_id']} for or added by this person. Do NOT claim "
                        f"it was removed; call list_exams for real ids.")
            return f"Removed exam #{tool_input['exam_id']} ({course})."
    except (ValueError, IndexError) as e:
        return f"ERROR: couldn't understand that day or time ({e}). Ask the user; do NOT guess."
    return f"Unknown tool: {name}"


async def _fridge_tool(name: str, tool_input: dict, member_id: int | None) -> str:
    from app import pantry
    today = datetime.now(ZoneInfo(settings.timezone)).date()

    if name == "add_to_fridge":
        item = (tool_input.get("item") or "").strip()
        if not item:
            return "ERROR: what's the item? Ask the user."
        expires = None
        if tool_input.get("expires_in_days") is not None:
            try:
                n = int(tool_input["expires_in_days"])
            except (TypeError, ValueError):
                return "ERROR: expires_in_days must be a number."
            if not 0 <= n <= 365:
                return "ERROR: that expiry is out of range. Ask the user."
            expires = today + timedelta(days=n)
        elif tool_input.get("expires_day"):
            try:
                expires = calendar_svc.resolve_date(tool_input["expires_day"])
            except ValueError:
                return f"ERROR: couldn't understand '{tool_input['expires_day']}'. Ask the user; do NOT guess."
        owner = member_id if tool_input.get("mine") else None
        iid = await db.add_pantry_item(item[:80], owner, expires, member_id)
        e = pantry.expiry(expires, today)
        return f"Added #{iid} {item} ({'yours' if owner else 'shared'})" + (f", expires {e['label']}." if e["label"] else ".")

    if name == "list_fridge":
        return pantry.describe(await db.open_pantry(), today)

    if name == "fridge_item_done":
        item = await db.use_pantry_item(tool_input["item_id"])
        if item is None:
            return (f"ERROR: no fridge item #{tool_input['item_id']}. Do NOT claim it changed; call "
                    f"list_fridge for real ids.")
        out = f"Marked {item} as finished."
        if tool_input.get("rebuy"):
            added = await db.add_shopping_item(item, None, member_id)
            out += " Added it to the shopping list." if added else " It's already on the shopping list."
        return out

    if name == "going_shopping":
        member = await db.get_member(member_id) if member_id else None
        if member is None:
            return "ERROR: couldn't tell who's going."
        try:
            n = await pantry.going_shopping(member, tool_input.get("store"))
        except pantry.TooSoon:
            return "Already told everyone in the last half hour; not pinging them again."
        items = await db.get_shopping_list()
        listing = ", ".join(i["item"] + (f" ({i['qty']})" if i["qty"] else "") for i in items) or "empty"
        return f"Told {n} roommate(s). Current list: {listing}."
    return f"Unknown tool: {name}"


async def _poll_tool(name: str, tool_input: dict, member_id: int | None) -> str:
    from app import polls
    if member_id is None:
        return "ERROR: couldn't tell who's asking."
    member = await db.get_member(member_id)
    try:
        if name == "create_poll":
            pid = await polls.create(tool_input.get("question"), tool_input.get("options") or [], member)
            return f"Poll #{pid} started and everyone else was notified."

        open_polls = [t for t in await polls.overview() if not t["closed"]]
        if name == "poll_results":
            recent = await polls.overview()
            return "\n".join(polls.describe(t) for t in recent) if recent else "No polls right now."

        def pick(want_mine: bool = False):
            pid = tool_input.get("poll_id")
            pool = [t for t in open_polls if not want_mine or t["created_by"] == member_id]
            if pid is not None:
                return next((t for t in pool if t["id"] == pid), None)
            return pool[0] if pool else None

        if name == "vote":
            t = pick()
            if t is None:
                return "ERROR: there's no open poll to vote in."
            if tool_input.get("remove"):
                if await db.remove_vote(t["id"], member_id):
                    return f"Removed their vote in poll #{t['id']}."
                return f"They hadn't voted in poll #{t['id']}."
            idx = polls.match_option(t, tool_input.get("option"))
            if idx is None:
                return (f"ERROR: '{tool_input.get('option')}' isn't one of the options ("
                        + ", ".join(o["text"] for o in t["options"]) + "). Ask which one.")
            await db.cast_vote(t["id"], member_id, idx)
            return f"Voted {t['options'][idx]['text']} in poll #{t['id']}."

        if name == "close_poll" and tool_input.get("delete"):
            pid = tool_input.get("poll_id")
            mine = [t for t in await polls.overview() if t["created_by"] == member_id]
            target = next((t for t in mine if t["id"] == pid), None) if pid is not None else (mine[0] if mine else None)
            if target is None or not await db.delete_poll(target["id"], member_id):
                return "ERROR: no poll of theirs to delete (only whoever asked can delete it)."
            return f"Deleted poll #{target['id']} for everyone."

        if name == "close_poll":
            t = pick(want_mine=True)
            if t is None or not await db.close_poll(t["id"], member_id):
                return "ERROR: no open poll of theirs to close (only whoever asked can close it)."
            closed = next(x for x in await polls.overview() if x["id"] == t["id"])
            win = polls.leaders(closed)
            return f"Closed poll #{t['id']}. " + (f"Winner: {' / '.join(win)}." if win else "No votes.")
    except polls.PollError as e:
        return f"ERROR: {e}. Ask the user."
    return f"Unknown tool: {name}"


async def _bus_tool(tool_input: dict) -> str:
    from app import transit
    trips = await db.bus_trips()
    if not trips:
        return "No bus trips are saved yet, so there's nothing to look up. Say so; don't guess times."
    names = ", ".join(t["name"] for t in trips)
    said = (tool_input.get("trip") or "").strip()
    if said:
        trip = transit.find_trip(trips, said)
        if trip is None:
            return f"ERROR: no saved trip matches '{said}'. Saved trips: {names}. Ask which one."
    else:
        trip = next((t for t in trips if not t["to_stops"]), trips[0])
    route = (tool_input.get("route") or "").strip().upper().removeprefix("ROUTE ").strip()
    if route:
        trip = {**dict(trip), "routes": [route]}
    try:
        deps = await transit.next_for(trip, transit.now())
    except transit.TransitError:
        return "ERROR: couldn't reach GRT's departures right now. Say so; do NOT guess times."
    out = transit.describe(trip, deps)
    if deps and not any(d["live"] for d in deps):
        out += " These are scheduled times (no live tracking right now)."
    return out + f" Other saved trips: {names}."


# A backup for the model's `urgent` flag: these words in the reminder itself make it urgent.
URGENT_WORDS = ("ضروري", "ضرورى", "مهم قوي", "مهم قوى", "مهم جدا", "مهم جداً", "اوعى تنسى",
                "متنسانيش", "urgent", "important", "asap", "don't let me forget")


def is_urgent(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in URGENT_WORDS)


def time_of(raw):
    """'19:00' -> time, None -> None. Raises ValueError for junk."""
    if not raw:
        return None
    from datetime import time as _time
    h, m = calendar_svc._parse_time(raw)
    return _time(h, m)


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
        try:
            ev = await asyncio.to_thread(
                calendar_svc.create_event,
                tool_input["summary"], tool_input["day"], tool_input["time"],
                tool_input.get("end_time"),
            )
        except calendar_svc.CalendarReadError as e:
            log.warning("create_event failed: %s", e)
            return ("ERROR: couldn't reach the calendar to add that right now. Tell the user to "
                    "try again in a moment; do NOT claim it was added.")
        invalidate_calendar_cache()
        if ev.get("existing"):
            return (f"Done: it's on the calendar ({ev['start']} {ev['summary']}, id: {ev['id']}). It was "
                    f"already there, so nothing was duplicated. Tell the user it's on the calendar.")
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
        invalidate_calendar_cache()
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
        invalidate_calendar_cache()
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
        urgent = bool(tool_input.get("urgent")) or is_urgent(text)
        rid = await db.add_reminder(target_id, text, due, member_id, urgent)
        return (f"Reminder #{rid} set for {target}: '{text}', {_when(due)} (local time)."
                + (" Urgent: it repeats up to 3 times, 2 minutes apart, until they tap it or "
                   "message you." if urgent else ""))

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

    if name in ("who_is_free", "get_schedule", "add_exam", "list_exams", "delete_exam",
                "pause_classes", "list_class_breaks", "resume_classes"):
        return await _schedule_tool(name, tool_input, member_id)

    if name in ("add_to_fridge", "list_fridge", "fridge_item_done", "going_shopping"):
        return await _fridge_tool(name, tool_input, member_id)

    if name in ("create_poll", "vote", "poll_results", "close_poll"):
        return await _poll_tool(name, tool_input, member_id)

    if name == "next_bus":
        return await _bus_tool(tool_input)

    if name == "get_briefing":
        from app import briefing   # imported here: briefing imports this module
        member = await db.get_member(member_id) if member_id else None
        if member is None:
            return "ERROR: couldn't tell who's asking."
        title, body = await briefing.build(member)
        return f"{title}\n{body}"

    return f"Unknown tool: {name}"
