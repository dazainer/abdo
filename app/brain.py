import logging

from anthropic import AsyncAnthropic
from app.config import settings
from app.tools import TOOLS, run_tool
from app.prompts import build_system_prompt
from app import db

log = logging.getLogger("abdo")
client = AsyncAnthropic(api_key=settings.anthropic_api_key)

MODEL = "claude-haiku-4-5-20251001"   # everyday chat; escalate to "claude-sonnet-4-6" when needed

# Safety cap on the tool loop: bounds worst-case latency and API cost, and
# guarantees the loop terminates even if the model keeps emitting tool calls.
# Legit multi-step turns (find id -> delete -> move) use only a handful.
MAX_TOOL_ROUNDS = 8


async def think(member, chat_id: int, user_text: str) -> str:
    history = await db.recent_messages(chat_id, limit=10)
    messages = [{"role": r["role"], "content": r["content"]} for r in history]
    messages.append({"role": "user", "content": user_text})

    system = build_system_prompt(
        member_name=member["name"],
        member_role=member["role"],
        family_roster=await db.roster_string(),
    )

    # Tool loop: Claude may call a tool; we run it and feed the result back.
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = await run_tool(block.name, block.input, member["id"])
                    # Ground truth for debugging: what the tool actually returned,
                    # so we can tell a tool bug from the model mis-narrating it.
                    log.info("tool %s(%s) -> %r", block.name, block.input, out)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": out,
                    })
            messages.append({"role": "user", "content": results})
            continue  # let Claude answer now that it has the tool output

        return "".join(b.text for b in resp.content if b.type == "text")

    # Hit the cap — stop rather than loop forever; ask them to simplify.
    return "آسف، الطلب ده طوّل عليّا شوية. ممكن نقسّمه خطوة خطوة؟"
