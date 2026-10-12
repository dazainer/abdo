"""Telegram channel: a thin adapter from Bot API webhooks to turn.handle_turn.

Owns only Telegram concerns — secret checks, update parsing, identity by Telegram id,
location pings, the voice background job, and delivery. The brain never knows a
turn came from Telegram (beyond the channel label in its context).
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app import db, stt, telegram, tts, turn
from app.config import settings

log = logging.getLogger("abdo")
router = APIRouter()

UNKNOWN_SENDER = "أنا عبده 👋 مش عارفك لسه — ask Zain to add you."
DIDNT_HEAR = "معلش، مسمعتش كويس — ممكن تعيد؟"
TOO_LONG_VOICE = "الرسالة الصوتية طويلة شوية — ممكن تبعتها أقصر؟"


def thread_for(parsed: dict, member) -> str:
    """A private chat is the member's personal thread, shared with the web app
    (dm:<member_id>), so Abdo remembers across devices. A group chat is its own
    shared thread (tg:<chat_id>)."""
    if parsed.get("chat_type", "private") == "private":
        return f"dm:{member['id']}"
    return f"tg:{parsed['chat_id']}"


@router.post("/tg/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Two-layer check: secret in the URL path + Telegram's secret_token header.
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(403, "bad path secret")
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(403, "bad header secret")

    update = await request.json()
    parsed = telegram.parse_update(update)
    if not parsed:
        return {"ok": True}  # ignore update types we don't handle

    chat_id = parsed["chat_id"]

    member = await db.get_member_by_telegram_id(parsed["from_user"]["id"])
    if not member:
        await telegram.send_message(chat_id, UNKNOWN_SENDER)
        return {"ok": True}

    if parsed["kind"] == "location":
        # Live-location edits stream in frequently — record silently, never reply.
        await db.upsert_location(member["id"], parsed["lat"], parsed["lng"])
        return {"ok": True}

    if parsed["kind"] == "voice":
        # STT -> brain -> TTS takes several seconds — far longer than Telegram's
        # webhook patience. Process AFTER returning 200, or Telegram retries and
        # the suite gets Abdo answering twice.
        background_tasks.add_task(handle_voice, member, parsed)
        return {"ok": True}

    # Text is fast enough to answer inline. handle_turn never raises, so this
    # always returns 200 (a 500 would make Telegram redeliver the update).
    await telegram.send_typing(chat_id)
    reply = await turn.handle_turn(member=member, text=parsed["text"],
                                   channel="telegram", thread=thread_for(parsed, member))
    await telegram.send_message(chat_id, reply)
    return {"ok": True}


async def handle_voice(member, parsed):
    """Voice round-trip, run in the background after the webhook returned 200.

    Voice is an I/O channel, not a new ability: STT produces text, the SAME turn
    logic answers it (voice=True only shortens the reply for the ear), TTS speaks
    it back. Modality matching: voice in -> voice out.
    """
    chat_id = parsed["chat_id"]
    # Cap monologues so STT and the brain aren't chewing on a long recording.
    if parsed.get("duration", 0) > settings.max_voice_seconds:
        await telegram.send_message(chat_id, TOO_LONG_VOICE)
        return

    await telegram.send_recording(chat_id)
    try:
        audio = await telegram.download_file(await telegram.get_file_path(parsed["file_id"]))
        text = await stt.transcribe(audio)
    except Exception:
        log.exception("voice STT/download failed for chat %s", chat_id)
        await telegram.send_message(chat_id, DIDNT_HEAR)
        return

    if not text:
        # Empty/garbled transcript — ask to repeat instead of feeding junk to the brain.
        await telegram.send_message(chat_id, DIDNT_HEAR)
        return

    reply = await turn.handle_turn(member=member, text=text, channel="telegram",
                                   thread=thread_for(parsed, member), voice=True)

    # Text-first: show the reply the instant the brain finishes (feels immediate,
    # gives a skimmable answer), then send the spoken note once TTS returns.
    if settings.reply_text_alongside_voice:
        await telegram.send_message(chat_id, reply)
    if settings.voice_replies:
        try:
            await telegram.send_voice(chat_id, await tts.synthesize(reply))
        except Exception:
            # TTS/ffmpeg failed — the text reply already went out, so degrade silently
            # to text-only rather than leaving the user with nothing.
            log.exception("voice TTS failed for chat %s", chat_id)
            if not settings.reply_text_alongside_voice:
                await telegram.send_message(chat_id, reply)
