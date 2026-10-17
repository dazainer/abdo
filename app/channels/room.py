"""The room device's channel (Phase 8a): the always-on speaker in the common room.

Like every channel it's a thin adapter: verify the device, identify the speaker, call
handle_turn on the shared "room" thread, hand back the reply as text and speech.

  POST /api/room/turn   (Authorization: Bearer <ROOM_DEVICE_TOKEN>)
    multipart: audio (WAV, what was said after the wake word)   or   text (for testing)
               speaker (a member's name, from on-device speaker ID; optional)
               confidence (0-1, how sure speaker ID is; optional)
    -> {"heard": bool, "transcript", "speaker" (name or null), "action": "reply"|"mute",
        "reply", "audio" (base64 MP3 or null)}

Speaker ID never authorizes anything: below ROOM_SPEAKER_MIN_CONFIDENCE the turn is
"Someone", and Abdo asks who it is when it matters. "اسكت يا عبده" is handled here, before
the brain: the device mutes itself and plays its own clip.
"""
import base64
import hmac
import logging
import re

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile

from app import db, stt, tts, turn
from app.config import settings

log = logging.getLogger("abdo")
router = APIRouter(prefix="/api/room")

THREAD = "room"
MAX_AUDIO_BYTES = 3 * 1024 * 1024

# "Abdo, be quiet": the whole request is a mute command (with or without his name).
MUTE_RE = re.compile(r"^\W*(?:(?:بقولك\s+)?يا\s+عبده\W*)?(?:اسكت|اسكت\s+شوية|اسكت\s+خالص|mute|be quiet|shh+)(?:\s+يا\s+عبده)?\W*$",
                     re.IGNORECASE)


def _authorized(authorization: str | None) -> bool:
    token = settings.room_device_token
    if not token or not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), token)


def is_mute(text: str) -> bool:
    return bool(MUTE_RE.match(text.strip()))


async def _speaker(name: str | None, confidence: float | None):
    """The members row speaker ID is sure about, or None (an unidentified room speaker)."""
    if not name or confidence is None or confidence < settings.room_speaker_min_confidence:
        return None
    mid = await db.get_member_id_by_name(name)
    return await db.get_member(mid) if mid else None


@router.post("/turn")
async def room_turn(audio: UploadFile | None = File(default=None), text: str | None = Form(default=None),
                    speaker: str | None = Form(default=None), confidence: float | None = Form(default=None),
                    authorization: str | None = Header(default=None)):
    if not settings.room_device_token:
        raise HTTPException(503, "the room device isn't set up (ROOM_DEVICE_TOKEN)")
    if not _authorized(authorization):
        raise HTTPException(401, "not the room device")

    said = (text or "").strip()
    if not said and audio is not None:
        data = await audio.read()
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "recording too long")
        if data:
            try:
                said = await stt.transcribe(data, audio.filename or "room.wav", audio.content_type or "audio/wav")
            except Exception:
                log.exception("room STT failed")
                said = ""
    if not said:
        return {"heard": False, "transcript": "", "speaker": None, "action": "reply", "reply": "", "audio": None}

    member = await _speaker(speaker, confidence)
    who = member["name"] if member else None
    if is_mute(said):
        return {"heard": True, "transcript": said, "speaker": who, "action": "mute", "reply": "", "audio": None}

    reply = await turn.handle_turn(member=member, text=said, channel="room", thread=THREAD, voice=True,
                                   speaker_confidence=confidence)
    audio_b64 = None
    if settings.elevenlabs_api_key and settings.tts_voice_id:
        try:
            audio_b64 = base64.b64encode(await tts.synthesize_mp3(reply)).decode()
        except Exception:
            log.exception("room TTS failed")
    return {"heard": True, "transcript": said, "speaker": who, "action": "reply", "reply": reply, "audio": audio_b64}
