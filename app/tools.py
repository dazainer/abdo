import asyncio

from app import db, embeddings, calendar_svc, geo

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
            "has confirmed the details you read back to them. Times are Cairo local time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "The event title."},
                "start": {"type": "string",
                          "description": "Start in ISO 8601 Cairo local time, e.g. 2026-06-19T19:00:00."},
                "end": {"type": "string",
                        "description": "Optional end (same format). Defaults to one hour after start."},
            },
            "required": ["summary", "start"],
        },
    },
    {
        "name": "update_event",
        "description": (
            "Change an existing calendar event (title or time). Get the event's id from "
            "get_calendar first. Only call AFTER the person confirms the change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The id of the event to change (from get_calendar)."},
                "summary": {"type": "string", "description": "New title, if changing it."},
                "start": {"type": "string", "description": "New start (ISO 8601 Cairo local time), if changing it."},
                "end": {"type": "string", "description": "New end (ISO 8601 Cairo local time), if changing it."},
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
]


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
        # googleapiclient is blocking — keep it off the event loop.
        events = await asyncio.to_thread(calendar_svc.get_events, tool_input.get("days_ahead", 7))
        if not events:
            return "No events on the shared calendar in that window."
        return "\n".join(f"- {e['start']}: {e['summary']} (id: {e['id']})" for e in events)

    if name == "create_event":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        ev = await asyncio.to_thread(
            calendar_svc.create_event,
            tool_input["summary"], tool_input["start"], tool_input.get("end"),
        )
        return f"Created: {ev['start']} {ev['summary']} (id: {ev['id']})."

    if name == "update_event":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        ev = await asyncio.to_thread(
            calendar_svc.update_event,
            tool_input["event_id"], tool_input.get("summary"),
            tool_input.get("start"), tool_input.get("end"),
        )
        return f"Updated: {ev['start']} {ev['summary']} (id: {ev['id']})."

    if name == "delete_event":
        if not calendar_svc.is_configured():
            return "The shared calendar isn't connected yet."
        await asyncio.to_thread(calendar_svc.delete_event, tool_input["event_id"])
        return "Deleted the event."

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

    return f"Unknown tool: {name}"
