import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
from app.config import settings
from app import db, telegram, brain, stt, tts

# Bump this with each deploy-worth change. Surfaced at GET / so we can confirm
# from outside exactly which code Railway is actually running.
VERSION = "2026-06-27-web-search-and-dictation-carveout"

# uvicorn doesn't attach a handler to the root logger, so a bare getLogger()
# at INFO emits nothing. Attach our own handler so app logs actually appear.
log = logging.getLogger("abdo")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:abdo: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)
log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Abdo starting up, version=%s", VERSION)
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"ok": True, "name": "Abdo", "version": VERSION}


@app.post("/tg/{secret}")
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
        await telegram.send_message(
            chat_id, "أنا عبده 👋 بس لسه مش عارفك. كلّم Zain يضيفك للعيلة."
        )
        return {"ok": True}

    if parsed["kind"] == "location":
        # Live-location edits stream in frequently — record silently, never reply.
        await db.upsert_location(member["id"], parsed["lat"], parsed["lng"])
        return {"ok": True}

    if parsed["kind"] == "voice":
        # STT -> brain -> TTS takes several seconds — far longer than Telegram's
        # webhook patience. Process AFTER returning 200, or Telegram retries and
        # the family gets Abdo answering twice.
        background_tasks.add_task(handle_voice, member, parsed)
        return {"ok": True}

    text = parsed["text"]
    await telegram.send_typing(chat_id)
    await db.log_message(member["id"], chat_id, "user", text)
    try:
        reply = await brain.think(member, chat_id, text)
        await db.log_message(member["id"], chat_id, "assistant", reply)
    except Exception:
        # Never go silent or 500 (which makes Telegram retry and duplicate).
        # Reply honestly and return 200 so the update isn't redelivered.
        log.exception("brain.think failed for chat %s", chat_id)
        reply = "آسف، حصلت مشكلة عندي دلوقتي. جرّب تاني بعد شوية 🙏"
    await telegram.send_message(chat_id, reply)
    return {"ok": True}


async def handle_voice(member, parsed):
    """Voice round-trip, run in the background after the webhook returned 200.

    Voice is an I/O channel, not a new ability: STT produces text, the SAME brain
    answers it (voice=True only shortens the reply for the ear), TTS speaks it back.
    Modality matching: voice in -> voice out.
    """
    chat_id = parsed["chat_id"]
    # Cap monologues so STT and the brain aren't chewing on a long recording.
    if parsed.get("duration", 0) > settings.max_voice_seconds:
        await telegram.send_message(
            chat_id, "الرسالة الصوتية طويلة شوية — ممكن تبعتها أقصر؟")
        return

    await telegram.send_recording(chat_id)
    try:
        audio = await telegram.download_file(
            await telegram.get_file_path(parsed["file_id"]))
        text = await stt.transcribe(audio)
    except Exception:
        log.exception("voice STT/download failed for chat %s", chat_id)
        await telegram.send_message(chat_id, "معلش، مسمعتش كويس — ممكن تعيد؟")
        return

    if not text:
        # Empty/garbled transcript — ask to repeat instead of feeding junk to the brain.
        await telegram.send_message(chat_id, "معلش، مسمعتش كويس — ممكن تعيد؟")
        return

    await db.log_message(member["id"], chat_id, "user", text)
    try:
        reply = await brain.think(member, chat_id, text, voice=True)
        await db.log_message(member["id"], chat_id, "assistant", reply)
    except Exception:
        log.exception("brain.think failed (voice) for chat %s", chat_id)
        reply = "آسف، حصلت مشكلة عندي دلوقتي. جرّب تاني بعد شوية 🙏"

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
