"""Text-to-speech: Egyptian Arabic via ElevenLabs Flash v2.5 -> OGG/Opus.

Isolated here so the voice is swappable. Two things worth remembering:
  - The Egyptian accent comes from the chosen `tts_voice_id`, NOT the model —
    ElevenLabs' default Arabic leans Gulf/MSA. Audition (or clone) a Cairene voice.
  - ElevenLabs returns MP3; Telegram `sendVoice` renders a voice bubble only for
    OGG/Opus, so we transcode with ffmpeg (a required system dependency).
Flash v2.5 skips number/date normalization for speed, so the voice-mode prompt
tells Abdo to write numbers as words; if one reply still reads oddly, that single
reply can be routed through `eleven_multilingual_v2`.
"""
import asyncio
import subprocess

import httpx

from app.config import settings


async def synthesize(text: str) -> bytes:
    """Egyptian Arabic text -> OGG/Opus bytes ready for sendVoice."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{settings.tts_voice_id}",
            headers={
                "xi-api-key": settings.elevenlabs_api_key,
                "accept": "audio/mpeg",
                "content-type": "application/json",
            },
            json={"text": text, "model_id": settings.tts_model},
        )
        r.raise_for_status()
        mp3 = r.content
    # ffmpeg is blocking; keep it off the event loop.
    return await asyncio.to_thread(_mp3_to_ogg, mp3)


def _mp3_to_ogg(mp3: bytes) -> bytes:
    """Transcode MP3 -> OGG/Opus (Telegram voice notes must be OGG/Opus)."""
    p = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
        input=mp3, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    return p.stdout
