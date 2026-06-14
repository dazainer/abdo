import httpx
from app.config import settings

API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def send_message(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})


async def send_typing(chat_id: int) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
        )


def parse_update(update: dict):
    """Return (chat_id, from_user, text) for plain text messages, else None."""
    msg = update.get("message")
    if not msg or "text" not in msg:
        return None  # ignore non-text updates (photos, voice, etc.) for now
    return msg["chat"]["id"], msg["from"], msg["text"]
