from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Header, HTTPException
from app.config import settings
from app import db, telegram, brain


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"ok": True, "name": "Abdo"}


@app.post("/tg/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
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

    text = parsed["text"]
    await telegram.send_typing(chat_id)
    await db.log_message(member["id"], chat_id, "user", text)
    reply = await brain.think(member, chat_id, text)
    await db.log_message(member["id"], chat_id, "assistant", reply)
    await telegram.send_message(chat_id, reply)
    return {"ok": True}
