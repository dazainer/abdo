import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from app.config import settings
from app.tools import TOOLS, run_tool
from app.prompts import build_system_prompt, exams_line, statuses_line
from app import db

log = logging.getLogger("abdo")
client = AsyncAnthropic(api_key=settings.anthropic_api_key)

# One model drives every turn. Calendar flakiness was fixed in code (dates resolved
# deterministically, degraded reads never treated as empty), not by a bigger model,
# so there's no escalation — don't reintroduce one to paper over a grounding bug.
MODEL = "claude-haiku-5-5"

# Haiku 5.5 runs adaptive thinking by default (effort defaults to medium). Chat and
# voice want snappy replies, so effort is low; raise to "medium" if grounding slips
# (skipped recall_facts, double calendar confirms). Thinking tokens count toward
# max_tokens, so the cap leaves room for thinking AND a short reply.
EFFORT = "low"
MAX_TOKENS = 2048

# Server-side web search (Anthropic-hosted): Claude runs the query and synthesizes
# the answer in the SAME turn, so it's just another entry in the tools list — there's
# no client-side handler in the tool loop (results come back as server_tool_use /
# web_search_tool_result blocks, not a client tool_use the loop has to run). `max_uses`
# caps searches per turn to keep cost down. The _20260209 dynamic-filtering variant
# isn't offered on Haiku, so this pins the basic _20250305 version (GA, no beta header).
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}

# Custom (client-side) tools + the server-side web search, in one stable list. Kept as
# a module constant so the tool set — which renders at the front of the prompt and is
# part of the cached prefix — stays byte-identical across requests.
ALL_TOOLS = TOOLS + [WEB_SEARCH_TOOL]

# Safety cap on the tool loop: bounds worst-case latency and API cost, and
# guarantees the loop terminates even if the model keeps emitting tool calls.
# Legit multi-step turns (find id -> delete -> move) use only a handful.
MAX_TOOL_ROUNDS = 8

EMPTY_REPLY = "آسف، حصل عندي لخبطة بسيطة — ممكن تعيد اللي قلته؟"
REFUSAL_REPLY = "معلش، دي مش هقدر أساعد فيها. لو في حاجة تانية قولي."
TOO_LONG_REPLY = "آسف، الطلب ده طوّل عليّا شوية. ممكن نقسّمه خطوة خطوة؟"


def _history_messages(rows) -> list[dict]:
    """Stored turns -> API messages. User turns carry the speaker's name so Abdo can
    follow shared threads (a group chat, the room); assistant turns stay bare.
    The API needs the first message to be a user turn, so a leading assistant row
    (cut off by the history limit) is dropped."""
    msgs = []
    for r in rows:
        if r["role"] == "user":
            msgs.append({"role": "user", "content": f"{r['speaker'] or 'Someone'}: {r['content']}"})
        elif msgs:
            msgs.append({"role": "assistant", "content": r["content"]})
    return msgs


def _log_usage(resp) -> None:
    """Cache reads should be non-zero on back-to-back requests; if they stay 0,
    something in the tools/static-persona prefix is varying between requests."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    log.info("usage: in=%s out=%s cache_read=%s cache_write=%s",
             getattr(u, "input_tokens", None), getattr(u, "output_tokens", None),
             getattr(u, "cache_read_input_tokens", None),
             getattr(u, "cache_creation_input_tokens", None))


async def think(member, thread: str, user_text: str, *, channel: str = "telegram",
                voice: bool = False) -> str:
    """Answer one user turn. `member` is None for an unidentified speaker (room device).
    The caller logs the turn AFTER this returns, so history never contains the
    current message twice."""
    name = member["name"] if member else "Someone"
    member_id = member["id"] if member else None

    history = await db.recent_messages(thread, limit=10)
    messages = _history_messages(history)
    messages.append({"role": "user", "content": f"{name}: {user_text}"})

    # Built once per turn and never changed between tool rounds: thinking blocks are
    # only valid while everything sent before them is unchanged.
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    soon = await db.exams_between(now - timedelta(hours=3), now + timedelta(days=2))
    system = build_system_prompt(member_name=name, roster=await db.roster_string(),
                                 channel=channel, voice=voice,
                                 statuses=statuses_line(await db.current_statuses(), tz),
                                 exams=exams_line(soon, tz, now))

    # Tool loop: Claude may call a tool; we run it and feed the result back.
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=ALL_TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
        )
        _log_usage(resp)

        if resp.stop_reason == "refusal":
            # Haiku has no server-side fallback model; answer honestly and in character.
            log.warning("model refused (stop_details=%s)", getattr(resp, "stop_details", None))
            return REFUSAL_REPLY

        if resp.stop_reason == "pause_turn":
            # A server-side tool (web_search) hit its internal iteration cap mid-turn.
            # Re-send the assistant turn so the server resumes where it left off — do
            # NOT add a user "continue" message; the trailing server_tool_use is the cue.
            messages.append({"role": "assistant", "content": resp.content})
            continue

        if resp.stop_reason == "tool_use":
            # Append the assistant content UNMODIFIED (thinking blocks included).
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    is_error = False
                    try:
                        out = await run_tool(block.name, block.input, member_id)
                    except Exception:
                        # A failing tool (bad calendar id, Cohere/Google down) must
                        # not crash the turn or go silent. Hand the model an honest
                        # error so it degrades to "couldn't reach X" instead of
                        # pretending it worked.
                        log.exception("tool %s(%s) raised", block.name, block.input)
                        is_error = True
                        out = (f"ERROR: the '{block.name}' tool failed and could not "
                               f"complete. Tell the user plainly you couldn't do it "
                               f"right now; do NOT pretend it worked.")
                    # Ground truth for debugging: what the tool actually returned,
                    # so we can tell a tool bug from the model mis-narrating it.
                    log.info("tool %s(%s) -> %r", block.name, block.input, out)
                    result = {"type": "tool_result", "tool_use_id": block.id, "content": out}
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
            messages.append({"role": "user", "content": results})
            continue  # let Claude answer now that it has the tool output

        # Responses can start with thinking blocks — read text blocks by type only.
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            # No text: either the model ended the turn silently, or thinking used up
            # max_tokens. Never send "" (Telegram rejects it and the user sees silence).
            log.warning("empty model reply (stop_reason=%s, model=%s)", resp.stop_reason, MODEL)
            return EMPTY_REPLY
        return text

    # Hit the cap — stop rather than loop forever; ask them to simplify.
    return TOO_LONG_REPLY
