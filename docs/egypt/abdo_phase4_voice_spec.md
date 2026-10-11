# Abdo — Phase 4 (Part 1) Build Spec: Voice over Telegram

Egyptian Arabic in, Egyptian Arabic out — entirely through Telegram voice notes. No wall tablet, no Raspberry Pi (those stay in the hardware brainstorm). This is the "wow" layer: you talk to Abdo and it talks back in an Egyptian voice.

## 0. Readiness gate — do not start until these hold

Voice is not a new capability; it's a new I/O channel over the **exact same brain**. Abdo will speak its text replies verbatim, so voice amplifies every rough edge. Before building this, confirm:

- Replies are **short and speakable** (one or two sentences) — long text answers are miserable to hear.
- Tool selection is **reliable** — Abdo calls `recall_facts` / `get_calendar` etc. instead of hallucinating answers.
- Persona is consistent across Egyptian Arabic, English, and Franco/Arabizi.
- Facts and recent memory **survive a redeploy**; tool failures degrade to an honest "couldn't reach X" rather than crashing or going silent.
- The calendar write-confirm step actually blocks a misheard edit.

## 1. The shape of it

```
voice note in -> download OGG from Telegram -> STT (Egyptian) -> text
   -> brain.think()  <- UNCHANGED, exactly as if the text were typed
   -> reply text -> TTS (Egyptian) -> OGG/Opus -> sendVoice
```

- **Modality matching:** voice in -> voice out; text in -> text out (Phase 1 path, unchanged).
- **The brain, tools, DB, and persona are untouched.** This is the *one* phase that edits the webhook/handler rather than just adding tools — because it's plumbing for a new channel, not a new ability. Everything that makes Abdo *Abdo* is reused as-is.

## 2. Provider — DECIDED: ElevenLabs (Scribe STT + Flash v2.5 TTS)

You leaned Munsit, and it's genuinely excellent at *understanding* Egyptian — but two findings rule it out here: it's an enterprise/government product (ministries, call centers), so access is sales-gated rather than a self-serve API key; and its native TTS voices are Gulf/Emirati, so it would answer your Cairo family in a Gulf accent — the exact "out of place" you want to avoid.

ElevenLabs wins on your three axes: self-serve and mature (build it today), lowest latency (Flash v2.5 ~75 ms, half the credit cost), and an Egyptian-capable voice out. One key covers both legs — **Scribe** for speech-to-text (handles Egyptian) and **Flash v2.5** for text-to-speech.

One honest caveat that directly serves "not out of place": ElevenLabs' Arabic is *officially* tuned for Gulf (Saudi/UAE), so the Egyptian accent comes from **the voice you pick, not the default**. Browse the Voice Library, filter for Arabic/Egyptian, and **audition** a few for a genuine Cairene accent (the glottal stop, the hard "g") before committing a `tts_voice_id` — or clone an Egyptian speaker (see Latency & smoothness). STT and TTS are isolated in `stt.py` / `tts.py`, so this stays swappable if you ever want to A/B another voice. **Verify current endpoints, model IDs, and voice IDs against the ElevenLabs docs** — they drift.

## 3. Config additions (`app/config.py`)

```python
    elevenlabs_api_key: str | None = None
    tts_voice_id: str | None = None             # an Egyptian-accent voice (Voice Library or a clone)
    tts_model: str = "eleven_flash_v2_5"        # fastest (~75ms) and half the credit cost
    stt_model: str = "scribe_v1"
    stt_language: str = "ar"                    # Arabic (expect code-switching with English)
    voice_replies: bool = True                  # reply with voice when the user sent voice
    reply_text_alongside_voice: bool = True     # send text first (instant), then the voice note
```

## 4. Dependencies

- `httpx` — already present (used for the REST calls).
- **ffmpeg** as a *system* package. Telegram voice notes are OGG/Opus and `sendVoice` requires OGG/Opus, but ElevenLabs returns MP3 (its output formats are mp3, pcm, ulaw — no native opus), so you must transcode. On Railway, add a `nixpacks.toml`:
  ```toml
  [phases.setup]
  aptPkgs = ["ffmpeg"]
  ```
  (or use a Dockerfile that `apt-get install -y ffmpeg`).

## 5. Telegram: download voice + send voice (modify `app/telegram.py`)

```python
async def get_file_path(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/getFile", json={"file_id": file_id})
        r.raise_for_status()
        return r.json()["result"]["file_path"]

async def download_file(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content

async def send_voice(chat_id: int, ogg_bytes: bytes) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{API}/sendVoice",
            data={"chat_id": str(chat_id)},
            files={"voice": ("abdo.ogg", ogg_bytes, "audio/ogg")},
        )

async def send_recording(chat_id: int) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{API}/sendChatAction",
                          json={"chat_id": chat_id, "action": "record_voice"})
```

Extend `parse_update` to recognize voice messages (it already returns a typed payload from Phase 3):

```python
    if "voice" in msg:
        v = msg["voice"]
        return {**base, "kind": "voice", "file_id": v["file_id"], "duration": v.get("duration", 0)}
```

## 6. STT module (`app/stt.py`, new)

```python
import httpx
from app.config import settings

async def transcribe(audio_bytes: bytes) -> str:
    """Egyptian Arabic speech-to-text via ElevenLabs Scribe. Returns transcript text."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": settings.elevenlabs_api_key},
            data={"model_id": settings.stt_model, "language_code": settings.stt_language},
            files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()
```

Telegram's OGG/Opus is a common format STT accepts directly; if the provider rejects it, transcode to 16 kHz WAV with ffmpeg first (same pattern as the TTS converter below).

## 7. TTS module (`app/tts.py`, new)

```python
import asyncio
import subprocess
import httpx
from app.config import settings

async def synthesize(text: str) -> bytes:
    """Egyptian Arabic text-to-speech -> OGG/Opus bytes ready for sendVoice."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{settings.tts_voice_id}",
            headers={"xi-api-key": settings.elevenlabs_api_key,
                     "accept": "audio/mpeg", "content-type": "application/json"},
            json={"text": text, "model_id": settings.tts_model},
        )
        r.raise_for_status()
        mp3 = r.content
    return await asyncio.to_thread(_mp3_to_ogg, mp3)

def _mp3_to_ogg(mp3: bytes) -> bytes:
    # Telegram voice notes must be OGG/Opus.
    p = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
        input=mp3, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    return p.stdout
```

Flash v2.5 disables number/date normalization for speed, so "123" or dates may read oddly — this is already mitigated by the voice-mode prompt telling Abdo to write numbers as words. If a specific reply still reads wrong, route just that one through `eleven_multilingual_v2`.

## 8. Voice-mode persona hint (modify `app/prompts.py` + `app/brain.py`)

Abdo writes differently for the ear. Add a `voice` flag so spoken replies stay short and clean.

`build_system_prompt(..., voice: bool = False)` — append when `voice` is True:
```
This message arrived as a voice note, so your reply will be spoken aloud. Keep it short and natural for the ear: a sentence or two, plain conversational Egyptian, no lists, no markdown, numbers as words where it sounds natural.
```

`brain.think(member, chat_id, user_text, voice: bool = False)` — pass `voice` through to `build_system_prompt`. Backward-compatible: the text path keeps calling `think(...)` with the default `False`.

## 9. Webhook routing (modify `app/main.py`)

The voice round-trip (STT -> brain -> TTS) takes several seconds — **far longer than Telegram's webhook patience.** If you process it inline, Telegram times out and *retries*, producing duplicate replies. So the voice path must run **after** you return `200`. FastAPI `BackgroundTasks` is enough at family scale.

```python
from fastapi import BackgroundTasks
from app import stt, tts

@app.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request, background_tasks: BackgroundTasks,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    # ... secret checks unchanged ...
    update = await request.json()
    parsed = telegram.parse_update(update)
    if not parsed:
        return {"ok": True}

    member = await db.get_member_by_telegram_id(parsed["from_user"]["id"])
    if not member:
        await telegram.send_message(parsed["chat_id"], "أنا عبده 👋 بس لسه مش عارفك. كلّم Zain يضيفك للعيلة.")
        return {"ok": True}

    if parsed["kind"] == "location":
        await db.upsert_location(member["id"], parsed["lat"], parsed["lng"])
        return {"ok": True}

    if parsed["kind"] == "voice":
        background_tasks.add_task(handle_voice, member, parsed)   # run after responding
        return {"ok": True}

    # kind == "text" -> existing inline Phase 1 flow (fast enough to keep inline)
    ...


async def handle_voice(member, parsed):
    chat_id = parsed["chat_id"]
    await telegram.send_recording(chat_id)
    audio = await telegram.download_file(await telegram.get_file_path(parsed["file_id"]))
    text = await stt.transcribe(audio)
    if not text:
        await telegram.send_message(chat_id, "معلش، مسمعتش كويس — ممكن تعيد؟")
        return
    await db.log_message(member["id"], chat_id, "user", text)
    reply = await brain.think(member, chat_id, text, voice=True)
    await db.log_message(member["id"], chat_id, "assistant", reply)
    # Smoothness: show the text the instant the brain finishes, then the spoken reply.
    if settings.reply_text_alongside_voice:
        await telegram.send_message(chat_id, reply)
    if settings.voice_replies:
        await telegram.send_voice(chat_id, await tts.synthesize(reply))
```

(`BackgroundTasks` runs the coroutine after the response is sent, in-process — fine here. A real job queue is overkill until you have many concurrent users.)

## 10. Latency & smoothness (keeping the trip short)

The voice trip is sequential — STT -> brain -> TTS — so shave each stage. Levers, biggest first:

- **Short replies.** TTS time scales with reply length, so the voice-mode prompt capping replies at a sentence or two is the single biggest win, and it's free.
- **Flash v2.5 for TTS** (~75 ms, half the credits). The right default for a snappy assistant; only switch a specific reply to `eleven_multilingual_v2` if that one needs the extra quality.
- **Text-first, voice-follows.** Send Abdo's reply as text the instant the brain finishes — it feels immediate and gives a skimmable answer — then send the voice note once TTS returns. `reply_text_alongside_voice` does exactly this.
- **Haiku brain** (already the default) keeps the middle stage fast.
- **In-memory, 200 first.** No disk round-trips; the background-task pattern returns `200` instantly, so the user sees "recording…" right away.
- **Cap long voice notes** (e.g. ignore > 60 s) so STT and the brain aren't chewing on a monologue.

**Cost (cost-friendly):** On Flash, ~1000 characters is roughly a minute of audio and costs half the credits of Multilingual. There's a free tier for prototyping; Starter (~$5/mo) covers solo testing and includes instant voice cloning + STT; once the family leans on it daily you'll likely want Creator-tier credits or usage-based overage. Short replies keep this small — set a spending cap in the ElevenLabs dashboard.

**Voice cloning (the authenticity play):** For the most "it's ours" feel, ElevenLabs can clone a voice from a short sample. Clone a willing family member's Cairene voice and Abdo speaks in it — the gold standard for "not out of place," and a natural thing to revisit alongside the hardware.

## 11. Gotchas

- **Latency / duplicate replies (the big one).** Always return `200` first and process voice in the background, or Telegram retries and the family gets Abdo answering twice.
- **Audio formats.** In = OGG/Opus, `sendVoice` out = OGG/Opus, ElevenLabs = MP3 -> transcode with ffmpeg (and make sure ffmpeg is actually in the build). Sending an MP3 to `sendVoice` won't render as a voice note.
- **Egyptian voice, not default Arabic.** The accent comes from the chosen `tts_voice_id` — audition for a Cairene voice (or clone one); don't ship the default Arabic voice, which leans Gulf/MSA.
- **Empty / garbled STT.** If the transcript is empty or nonsense, ask the person to repeat — don't feed junk to the brain.
- **Keep replies short.** The voice-mode prompt enforces this; without it, Abdo reads paragraphs aloud.
- **Code-switching.** Set STT language to Arabic but expect English words mixed in; don't hard-fail on them.
- **One voice.** Pick a single `tts_voice_id` and keep it — Abdo's voice is part of its identity.

## 12. Build checklist

- [ ] ElevenLabs account + API key; pick/audition (or clone) an Egyptian `tts_voice_id`
- [ ] Add ffmpeg to the Railway build (`nixpacks.toml` aptPkgs)
- [ ] Env: `ELEVENLABS_API_KEY`, `TTS_VOICE_ID`, `TTS_MODEL=eleven_flash_v2_5`, `STT_MODEL=scribe_v1`, `STT_LANGUAGE=ar`, `VOICE_REPLIES=true`, `REPLY_TEXT_ALONGSIDE_VOICE=true`
- [ ] `telegram.py`: `get_file_path` / `download_file` / `send_voice` / `send_recording`; voice case in `parse_update`
- [ ] `stt.py` and `tts.py`
- [ ] `prompts.py` + `brain.py`: voice-mode flag for short spoken replies
- [ ] `main.py`: route voice -> `handle_voice` via `BackgroundTasks`, text-first then voice
- [ ] Test: voice note in Egyptian Arabic -> text appears instantly, spoken reply follows in an Egyptian voice
- [ ] Test: a voice note that triggers a tool (e.g. "الكلاب اتأكلوا؟") works end-to-end
- [ ] Test: silence / gibberish -> "ممكن تعيد؟"; long voice note -> still one reply

## 13. CLAUDE.md update

- Flip **current phase** to Phase 4 (voice).
- Env additions: `ELEVENLABS_API_KEY`, `TTS_VOICE_ID`, `TTS_MODEL`, `STT_MODEL`, `STT_LANGUAGE`, `VOICE_REPLIES`, `REPLY_TEXT_ALONGSIDE_VOICE`.
- Note: "The webhook handles **text, location, and voice**. Voice runs in a **background task** (STT -> brain -> TTS) and returns `200` first to avoid Telegram retry/duplicate replies."
- Note: "Voice is an I/O channel, not a tool — `brain.py`/`tools.py` are unchanged. STT/TTS isolated in `stt.py`/`tts.py` (ElevenLabs: Scribe + Flash v2.5, swappable). The Egyptian accent comes from the chosen `tts_voice_id`, not the default. ffmpeg is a required system dependency for OGG/Opus transcoding."
- Gotcha: "`sendVoice` requires OGG/Opus; ElevenLabs returns MP3 -> ffmpeg transcode. Flash v2.5 skips number normalization, so Abdo writes numbers as words in voice mode."
