"""Speech-to-text: Egyptian Arabic via ElevenLabs Scribe.

Isolated here so the provider stays swappable — the brain never knows how the
text was produced. Scribe accepts all major audio formats directly: Telegram voice
notes (OGG/Opus), Chrome/Android recordings (WebM/Opus), and Safari/iOS recordings
(MP4/AAC). Code-switching into English is expected; language is set to Arabic but
we don't hard-fail on mixed words.
"""
import httpx

from app.config import settings

ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg",
                     content_type: str = "audio/ogg") -> str:
    """Transcribe a recording. Returns the transcript ("" if blank)."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            ENDPOINT,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            data={"model_id": settings.stt_model, "language_code": settings.stt_language},
            files={"file": (filename, audio_bytes, content_type)},
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()
