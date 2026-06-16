import logging

from anthropic import AsyncAnthropic
from app.config import settings
from app.tools import TOOLS, run_tool
from app.prompts import build_system_prompt
from app import db

log = logging.getLogger("abdo")
client = AsyncAnthropic(api_key=settings.anthropic_api_key)

HAIKU = "claude-haiku-4-5-20251001"   # everyday driver
SONNET = "claude-sonnet-4-6"          # escalate for calendar reasoning

# Calendar work — interpreting relative dates, choosing create vs update,
# tracking real event ids, and confirm-then-act — is exactly where Haiku flakes
# (wrong weekday, duplicate events, invented ids, false "done"). Route those
# turns to Sonnet. Heuristic keyword match across Arabic / English / Franco;
# over-triggering only costs a bit more, under-triggering risks a bad write.
_CALENDAR_HINTS = (
    # English
    "calendar", "event", "appointment", "schedule", "reschedule", "meeting", "remind",
    # Arabic — calendar nouns + schedule verbs (add/move/postpone/delete/cancel)
    "ميعاد", "معاد", "موعد", "مواعيد", "كالندر", "تقويم", "أجندة", "اجندة", "حدث",
    "احجز", "احجزلي", "زوّد", "زود", "ضيف", "أضيف", "أجّل", "أجل", "اجّل", "اجل",
    "غيّر", "غير", "انقل", "أنقل", "امسح", "إمسح", "الغي", "ألغي", "اتأجل", "اتلغى",
    # Franco / Arabizi
    "ma3ad", "me3ad", "mi3ad", "maw3ad", "mawa3eed", "8ayar", "ghayar", "2ayar",
    "zawed", "zood", "2def", "def ", "emsa7", "alghy", "2agel", "a2gel", "an2el",
)


def _pick_model(user_text: str) -> str:
    """Calendar-ish turns go to Sonnet; everything else stays on Haiku."""
    t = user_text.lower()
    return SONNET if any(h in t for h in _CALENDAR_HINTS) else HAIKU


# Safety cap on the tool loop: bounds worst-case latency and API cost, and
# guarantees the loop terminates even if the model keeps emitting tool calls.
# Legit multi-step turns (find id -> delete -> move) use only a handful.
MAX_TOOL_ROUNDS = 8


async def think(member, chat_id: int, user_text: str) -> str:
    model = _pick_model(user_text)
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
            model=model,
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
                        resp.stop_reason, model)
            return "آسف، حصل عندي لخبطة بسيطة — ممكن تعيد اللي قلته؟"
        return text

    # Hit the cap — stop rather than loop forever; ask them to simplify.
    return "آسف، الطلب ده طوّل عليّا شوية. ممكن نقسّمه خطوة خطوة؟"
