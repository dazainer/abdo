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


# Steadier delivery for a clear Cairene pace. speed ranges 0.7–1.2 (default 1.0);
# 1.0 is a natural conversational pace (~12% quicker than the earlier 0.9).
_VOICE_SETTINGS = {"stability": 0.55, "similarity_boost": 0.75, "speed": 1.0}

# Flash v2.5 deliberately skips number normalization, so digit strings ("2013",
# phone numbers, amounts) come out wrong. The voice-mode prompt writes ordinary
# numbers as words, but as a backstop ANY reply that still carries a digit is
# synthesized on multilingual_v2 (which pronounces numbers correctly) — a little
# extra latency only on number-bearing replies.
_NUMERIC_MODEL = "eleven_multilingual_v2"


def _pick_model(text: str) -> str:
    """Flash by default; multilingual_v2 for any digit-bearing reply (see above)."""
    return _NUMERIC_MODEL if any(c.isdigit() for c in text) else settings.tts_model


async def synthesize(text: str) -> bytes:
    """Egyptian Arabic text -> OGG/Opus bytes ready for sendVoice."""
    model = _pick_model(text)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{settings.tts_voice_id}",
            headers={
                "xi-api-key": settings.elevenlabs_api_key,
                "accept": "audio/mpeg",
                "content-type": "application/json",
            },
            json={"text": text, "model_id": model, "voice_settings": _VOICE_SETTINGS},
        )
        r.raise_for_status()
        mp3 = r.content
    # ffmpeg is blocking; keep it off the event loop.
    return await asyncio.to_thread(_mp3_to_ogg, mp3)


def _mp3_to_ogg(mp3: bytes) -> bytes:
    """Transcode MP3 -> OGG/Opus (Telegram voice notes must be OGG/Opus).

    `volume=1.5` lifts amplitude (~+50%); ElevenLabs output sits below full scale
    and was too quiet on phones. `alimiter` catches the louder peaks so the boost
    stays clean instead of clipping into distortion.
    """
    p = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-af", "volume=1.5,alimiter=limit=0.97",
         "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
        input=mp3, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    return p.stdout
