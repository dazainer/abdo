"""Generate Abdo's pre-recorded room-device lines (Phase 8a) from room/clips.json.

Each line is synthesized once in Abdo's ElevenLabs voice and saved as MP3 under
room/clips/<category>/<id>.mp3, with room/clips/manifest.json mapping every file to its text.
Clips that already exist are skipped, so re-running only pays for new or changed lines.
The timer chime is generated locally (no credits).

    .venv/bin/python scripts/make_clips.py --dry-run     # list the lines and the credit cost
    .venv/bin/python scripts/make_clips.py               # generate what's missing
    .venv/bin/python scripts/make_clips.py --only ack    # one category
    .venv/bin/python scripts/make_clips.py --redo ack/ack-03   # regenerate one clip

Reads ELEVENLABS_API_KEY and TTS_VOICE_ID from .env (the same voice Abdo uses everywhere).
"""
import argparse
import asyncio
import hashlib
import json
import math
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIST = ROOT / "room" / "clips.json"
OUT = ROOT / "room" / "clips"
# Clips are generated once, so they use the higher-quality model (1 credit per character).
CLIP_MODEL = "eleven_multilingual_v2"


def lines(spec: dict) -> list[dict]:
    """Every clip as {"id", "category", "text"} in a stable order."""
    out = []
    for cat in ("ack", "thinking", "mute", "unmute"):
        for i, text in enumerate(spec[cat], 1):
            out.append({"id": f"{cat}-{i:02d}", "category": cat, "text": text})
    for key, text in spec["error"].items():
        out.append({"id": f"error-{key}", "category": "error", "text": text})
    for prayer, name in spec["prayer"]["names"].items():
        for i, who in enumerate(spec["prayer"]["addressees"], 1):
            out.append({"id": f"{prayer}-{i:02d}", "category": "prayer", "text": f"{name} {who}"})
    return out


def chime(path: Path, rate: int = 22050) -> None:
    """A soft two-note chime for timers (E6 then A6, each with a quick fade)."""
    samples = []
    for freq, dur in ((1318.5, 0.18), (1760.0, 0.32)):
        n = int(rate * dur)
        for k in range(n):
            env = min(1.0, k / (rate * 0.01)) * math.exp(-4.0 * k / n)
            samples.append(0.45 * env * math.sin(2 * math.pi * freq * k / rate))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(s * 32767)) for s in samples))


async def synth(text: str) -> bytes:
    import httpx
    from app.config import settings
    if not settings.elevenlabs_api_key or not settings.tts_voice_id:
        raise SystemExit("ELEVENLABS_API_KEY and TTS_VOICE_ID must be set in .env")
    from app.tts import _VOICE_SETTINGS
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{settings.tts_voice_id}",
            headers={"xi-api-key": settings.elevenlabs_api_key, "accept": "audio/mpeg",
                     "content-type": "application/json"},
            json={"text": text, "model_id": CLIP_MODEL, "voice_settings": _VOICE_SETTINGS},
        )
        r.raise_for_status()
        return r.content


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="list what would be generated and the cost")
    ap.add_argument("--only", help="one category: ack, thinking, error, mute, unmute, prayer")
    ap.add_argument("--redo", action="append", default=[], help="regenerate <category>/<id> even if it exists")
    args = ap.parse_args()

    spec = json.loads(LIST.read_text(encoding="utf-8"))
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    todo = []
    for c in lines(spec):
        if args.only and c["category"] != args.only:
            continue
        rel = f"{c['category']}/{c['id']}.mp3"
        known = manifest.get(rel)
        fresh = (OUT / rel).exists() and known and known["hash"] == text_hash(c["text"])
        if fresh and rel.removesuffix(".mp3") not in args.redo:
            continue
        todo.append((rel, c))

    chars = sum(len(c["text"]) for _, c in todo)
    print(f"{len(todo)} clip(s) to generate, {chars} characters (about {chars} credits on {CLIP_MODEL}).")
    for rel, c in todo:
        print(f"  {rel:28} {c['text']}")
    if args.dry_run:
        return

    for rel, c in todo:
        path = OUT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(await synth(c["text"]))
        manifest[rel] = {"category": c["category"], "text": c["text"], "hash": text_hash(c["text"])}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved {rel}")

    timer = OUT / "sounds" / "timer.wav"
    if not timer.exists():
        chime(timer)
        manifest["sounds/timer.wav"] = {"category": "sound", "text": "(timer chime)", "hash": "local"}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  saved sounds/timer.wav (local, no credits)")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
