"""The one entry point every channel calls (Telegram today; web app and room device later).

A channel's job is only: verify the request, identify the member, pick a thread key,
call handle_turn, and deliver the reply. Everything that makes Abdo *Abdo* — the
brain, tools, persona, conversation log — sits behind this function.
"""
import logging

from app import brain, db

log = logging.getLogger("abdo")

FALLBACK = "آسف، حصلت مشكلة عندي دلوقتي. جرّب تاني بعد شوية 🙏"


async def handle_turn(*, member, text: str, channel: str, thread: str,
                      voice: bool = False, speaker_confidence: float | None = None) -> str:
    """Run one turn and log it. Never raises: a failure becomes an honest fallback
    line, because a channel that goes silent or 500s gets retried (duplicate replies).

    thread: conversation key — 'tg:<chat_id>', 'web:<member_id>', or 'room'.
    member: the speaker's members row, or None for an unidentified room speaker.
    speaker_confidence: room speaker-ID score; None when identity came from a login.
    """
    if member:
        # Saying anything to Abdo means they've seen their urgent reminder: stop the repeats.
        try:
            await db.ack_reminders(member["id"])
        except Exception:
            log.exception("couldn't acknowledge reminders for member %s", member["id"])
    try:
        reply = await brain.think(member, thread, text, channel=channel, voice=voice)
    except Exception:
        log.exception("brain.think failed (thread=%s)", thread)
        reply = FALLBACK

    # Logged after the brain runs, so the history it loaded didn't already contain
    # this message (it used to arrive twice).
    member_id = member["id"] if member else None
    modality = "voice" if voice else "text"
    try:
        await db.log_message(thread=thread, channel=channel, role="user", member_id=member_id,
                             content=text, modality=modality,
                             speaker_confidence=speaker_confidence)
        await db.log_message(thread=thread, channel=channel, role="assistant",
                             member_id=member_id, content=reply, modality=modality)
    except Exception:
        # Losing a log row must not cost the user their reply.
        log.exception("failed to log turn (thread=%s)", thread)
    return reply
