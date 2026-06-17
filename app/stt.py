"""Speech-to-text: Egyptian Arabic via ElevenLabs Scribe.

Isolated here so the provider stays swappable — the brain never knows how the
text was produced. Telegram voice notes are OGG/Opus, which Scribe accepts
directly (all major audio formats). Code-switching into English is expected;
language is set to Arabic but we don't hard-fail on mixed words.
"""
import httpx

from app.config import settings

ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe an OGG/Opus voice note. Returns the transcript ("" if blank)."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            ENDPOINT,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            data={"model_id": settings.stt_model, "language_code": settings.stt_language},
            files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()
