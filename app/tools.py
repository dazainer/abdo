from app import db

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

    return f"Unknown tool: {name}"
