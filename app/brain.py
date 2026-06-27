import logging

from anthropic import AsyncAnthropic
from app.config import settings
from app.tools import TOOLS, run_tool
from app.prompts import build_system_prompt
from app import db

log = logging.getLogger("abdo")
client = AsyncAnthropic(api_key=settings.anthropic_api_key)

# Haiku drives every turn now — including the calendar. The old Sonnet escalation
# was a stopgap for Haiku flaking on calendar writes (wrong weekday, create-vs-update
# duplicates, invented ids, false "done"). Those came from date arithmetic and a
# false-empty read, NOT model size: dates are now resolved deterministically in
# Python (calendar_svc.resolve_date) and a degraded read can no longer masquerade
# as an empty calendar. With the failure modes fixed in code, the keyword heuristic
# only inflated cost, so it's retired.
HAIKU = "claude-haiku-4-5-20251001"


# Server-side web search (Anthropic-hosted): Claude runs the query and synthesizes
# the answer in the SAME turn, so it's just another entry in the tools list — there's
# no client-side handler in the tool loop (results come back as server_tool_use /
# web_search_tool_result blocks, not a client tool_use the loop has to run). `max_uses`
# caps searches per turn to keep token cost down at family scale. Haiku 4.5 isn't a
# dynamic-filtering model, so we pin the stable _20250305 version (GA — no beta header).
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}

# Custom (client-side) tools + the server-side web search, in one stable list. Kept as
# a module constant so the tool set — which renders at the front of the prompt — stays
# byte-identical across requests and doesn't needlessly bust the prompt cache.
ALL_TOOLS = TOOLS + [WEB_SEARCH_TOOL]


# Safety cap on the tool loop: bounds worst-case latency and API cost, and
# guarantees the loop terminates even if the model keeps emitting tool calls.
# Legit multi-step turns (find id -> delete -> move) use only a handful.
MAX_TOOL_ROUNDS = 8


async def think(member, chat_id: int, user_text: str, voice: bool = False) -> str:
    history = await db.recent_messages(chat_id, limit=10)
    messages = [{"role": r["role"], "content": r["content"]} for r in history]
    messages.append({"role": "user", "content": user_text})

    # `voice=True` (the brain doesn't otherwise know how the text arrived) shortens
    # replies for the ear. Tools, DB, and persona are otherwise identical to text.
    system = build_system_prompt(
        member_name=member["name"],
        member_role=member["role"],
        family_roster=await db.roster_string(),
        voice=voice,
    )

    # Tool loop: Claude may call a tool; we run it and feed the result back.
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.messages.create(
            model=HAIKU,
            max_tokens=600,
            system=system,
            tools=ALL_TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "pause_turn":
            # A server-side tool (web_search) hit its internal iteration cap mid-turn.
            # Re-send the assistant turn so the server resumes where it left off — do
            # NOT add a user "continue" message; the trailing server_tool_use is the cue.
            messages.append({"role": "assistant", "content": resp.content})
            continue

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        out = await run_tool(block.name, block.input, member["id"])
                    except Exception:
                        # A failing tool (bad calendar id, Cohere/Google down) must
                        # not crash the webhook or go silent. Hand the model an
                        # honest error so it degrades to "couldn't reach X" instead
                        # of pretending it worked.
                        log.exception("tool %s(%s) raised", block.name, block.input)
                        out = (f"ERROR: the '{block.name}' tool failed and could not "
                               f"complete. Tell the user plainly you couldn't do it "
                               f"right now; do NOT pretend it worked.")
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

        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            # Model ended the turn with no text -> we used to send "" and Telegram
            # rejected it (HTTP 400 empty), so the user just saw silence.
            log.warning("empty model reply (stop_reason=%s, model=%s)",
                        resp.stop_reason, HAIKU)
            return "آسف، حصل عندي لخبطة بسيطة — ممكن تعيد اللي قلته؟"
        return text

    # Hit the cap — stop rather than loop forever; ask them to simplify.
    return "آسف، الطلب ده طوّل عليّا شوية. ممكن نقسّمه خطوة خطوة؟"
