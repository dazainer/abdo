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
    """Return a typed payload dict, or None for updates we ignore.

    {"kind": "text", "chat_id", "from_user", "text"}
    {"kind": "location", "chat_id", "from_user", "lat", "lng"}

    Live-location updates arrive as `edited_message`, so read that too.
    """
    msg = update.get("message") or update.get("edited_message")
    if not msg or "from" not in msg:
        return None  # ignore channel posts / updates without a sender
    base = {"chat_id": msg["chat"]["id"], "from_user": msg["from"]}
    if "text" in msg:
        return {**base, "kind": "text", "text": msg["text"]}
    if "location" in msg:
        loc = msg["location"]
        return {**base, "kind": "location", "lat": loc["latitude"], "lng": loc["longitude"]}
    return None  # ignore other update types (photos, voice, etc.) for now
