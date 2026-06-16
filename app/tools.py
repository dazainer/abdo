from app import db, embeddings

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

    return f"Unknown tool: {name}"
