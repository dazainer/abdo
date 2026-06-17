import logging

import httpx
from app.config import settings

log = logging.getLogger("abdo")
API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def send_message(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}
        )
    # Telegram rejects (HTTP 400) on empty text or bad content and we used to
    # swallow it — the user just saw no reply. Log it loudly instead of failing
    # the webhook (raising here would make Telegram retry and duplicate work).
    if resp.status_code != 200:
        log.error("sendMessage failed %s: %s (text=%r)",
                  resp.status_code, resp.text, text)


async def send_typing(chat_id: int) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
        )


async def send_recording(chat_id: int) -> None:
    """The 'recording voice…' bubble — shown while the voice round-trip runs."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{API}/sendChatAction",
            json={"chat_id": chat_id, "action": "record_voice"},
        )


async def get_file_path(file_id: str) -> str:
    """Resolve a Telegram file_id to the relative path used by the file API."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/getFile", json={"file_id": file_id})
        r.raise_for_status()
        return r.json()["result"]["file_path"]


async def download_file(file_path: str) -> bytes:
    """Download a Telegram file (the file API uses a different base URL)."""
    url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def send_voice(chat_id: int, ogg_bytes: bytes) -> None:
    """Send a voice note. Telegram renders it as a voice bubble only for OGG/Opus."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API}/sendVoice",
            data={"chat_id": str(chat_id)},
            files={"voice": ("abdo.ogg", ogg_bytes, "audio/ogg")},
        )
    if resp.status_code != 200:
        log.error("sendVoice failed %s: %s", resp.status_code, resp.text)


def parse_update(update: dict):
    """Return a typed payload dict, or None for updates we ignore.

    {"kind": "text", "chat_id", "from_user", "text"}
    {"kind": "location", "chat_id", "from_user", "lat", "lng"}
    {"kind": "voice", "chat_id", "from_user", "file_id", "duration"}

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
    if "voice" in msg:
        v = msg["voice"]
        return {**base, "kind": "voice", "file_id": v["file_id"], "duration": v.get("duration", 0)}
    return None  # ignore other update types (photos, etc.) for now
