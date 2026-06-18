# CLAUDE.md — Abdo


Abdo (عبده) is a conversational household assistant for the Khalil family in New Cairo, Egypt. It speaks Egyptian colloquial Arabic, knows the family, and is accessed **entirely through Telegram** (no web UI or wall display YET). This file is the project's durable memory; detailed, phase-by-phase build briefs live in `docs/` — see "Build phases" below.

## The one principle that governs everything

**Every capability is a Claude tool.** The request flow is fixed:

```
Telegram webhook → FastAPI → load member + state from Postgres
   → build system prompt → Claude (with tools)
   → tool loop runs actions against Postgres → reply via Telegram → log to Postgres
```

Adding a feature = adding a tool definition (`tools.py`) + its backing query (`db.py`) + any tables (`schema.sql`). **Never refactor the core loop to add a feature — extend the tool set.** Keep the webhook handler thin.

The webhook now handles **text, location, and voice** updates; location pings (live location, arriving as `edited_message`) are recorded silently with no reply. Voice notes run in a **background task** (STT → brain → TTS) and return `200` first, to avoid Telegram retry/duplicate replies.

The brain is the Anthropic API: **Haiku 4.5 for everything**. The earlier Sonnet 4.6 escalation (a keyword heuristic for calendar turns) is **retired** — the calendar flakiness was date arithmetic + a false-empty read, fixed in code (deterministic date resolution + read guards) and prompt discipline, not model size. Reliability fixes stay on Haiku; don't reintroduce escalation to paper over a grounding bug.

## Stack

- Python 3.11+, **FastAPI** + uvicorn (webhook server)
- **httpx** (Telegram Bot API), **anthropic** SDK (async), **asyncpg** (Postgres, raw SQL)
- **pydantic-settings** (config); **Postgres + pgvector** on **Railway**
- Embeddings: Cohere `embed-v4.0` at **1024 dims** (must match `household_facts.embedding vector(1024)`).
- Interface: Telegram only. Deploy target: Railway (Hobby plan).

## Project layout

```
app/  → main.py (FastAPI + webhook), config.py, db.py,
        telegram.py, brain.py, tools.py, prompts.py, embeddings.py,
        calendar_svc.py, geo.py, stt.py, tts.py
schema.sql, requirements.txt, Procfile, nixpacks.toml, .env.example
docs/ → phase-by-phase build specs
```

## Conventions

- Async throughout (FastAPI, asyncpg, AsyncAnthropic).
- Raw SQL via asyncpg at this scale — **no ORM**.
- Secrets only via env vars (see `.env.example`); never hardcode tokens or keys.
- Abdo's persona lives in `prompts.py`. Behavior/voice changes go there, not scattered through the code.
- Conversation history is persisted to `messages` for short-term memory and debugging.

## Critical gotchas — IMPORTANT

- **Timezone**: all "today" / daily-reset logic uses `Africa/Cairo`, NOT UTC. The dog-feeding daily reset depends on this.
- **Telegram + Arabic**: send **plain text** (no `parse_mode`) by default — Arabic combined with Markdown's special characters causes silent HTTP 400s. Add `parse_mode` only where the formatting is fully controlled.
- **Webhook latency**: Telegram retries if the webhook is slow. Family-scale Claude calls inside the handler are fine for now; if a later phase adds latency (voice, RAG), return `200` immediately and do the work in a background task.
- **Model strings drift**: confirm current model IDs at https://docs.claude.com/en/docs/about-claude/models before pinning. Currently `claude-haiku-4-5-20251001` drives everything (no Sonnet escalation).
- **Embedding dimension is fixed at 1024**: `household_facts.embedding` is `vector(1024)`; only swap to another 1024-dim embedder. Also: document vs query input types (`search_document` on store, `search_query` on recall) must not be swapped — getting them backwards silently wrecks retrieval.
- **Railway `DATABASE_URL` is not auto-shared**: set it on the app service as a reference → `${{Postgres.DATABASE_URL}}` (exact service name, or it resolves to an empty host and asyncpg crashes at startup).
- **Calendar read + write** via a service account the family calendar is shared with (needs "Make changes to events" sharing). Scope is `calendar.events` (least-privilege writable — deliberately not the broader `calendar`, which can delete calendars). `googleapiclient` is blocking, so wrap its calls in `asyncio.to_thread`. Module is `calendar_svc` (not `calendar`) to avoid shadowing the stdlib. Abdo confirms before any create/edit (persona rule, not a code gate).
- **Never reply per location update**: live-location edits stream in every few seconds — record and return `200`; only the `where_is` tool reports position. Answers show `updated HH:MM` because sharing can stop and leave stale coordinates.
- **Voice is an I/O channel, not a tool**: `brain.py`/`tools.py`/`db.py`/persona are unchanged — STT just produces text the brain answers, and TTS speaks the reply back. STT/TTS are isolated in `stt.py`/`tts.py` (ElevenLabs: Scribe + Flash v2.5, swappable). The Egyptian accent comes from the chosen `tts_voice_id`, **not** the default Arabic voice (which leans Gulf/MSA) — audition or clone a Cairene one. The voice round-trip MUST run in a `BackgroundTasks` job after returning `200`, or Telegram times out and retries → duplicate replies. Modality matching: voice in → voice out (`voice=True` only shortens the reply for the ear).
- **Grounding > guessing**: the system prompt forces `recall_facts` before "I don't know", and calendar weekdays are resolved **in code** (`tools._format_event` renders the weekday; the model never computes one). A fact is never stored without a valid 1024-dim embedding (`embeddings.is_valid`), and `remember_fact` dedups (cosine distance < `FACT_DEDUP_DISTANCE`) by updating in place instead of inserting a duplicate. If recall ever misses a known fact, suspect a NULL/bad embedding row → re-run `scripts/backfill_embeddings.py`.
- **STT model**: `scribe_v1` is deprecated/removed **2026-07-09** — default is now `scribe_v2` (env-swappable via `STT_MODEL`). Flash v2.5 still can't normalize numbers, so `tts.py` routes any **digit-bearing** reply to `eleven_multilingual_v2` and the voice prompt spells passwords/codes out as words.
- **New `orders` table**: the orders feature added a table to `schema.sql` that isn't in the deployed DB yet — apply it (`CREATE TABLE orders …`) against `DATABASE_URL` before the order tools work in prod.
- **`sendVoice` needs OGG/Opus; ElevenLabs returns MP3** → `tts.py` transcodes with **ffmpeg** (a required system package — `nixpacks.toml` `aptPkgs`). Sending MP3 to `sendVoice` won't render as a voice note. Flash v2.5 skips number/date normalization, so the voice-mode prompt makes Abdo write numbers as words.

## Commands

- Install: `pip install -r requirements.txt`
- Run locally: `uvicorn app.main:app --reload`
- Apply schema: run `schema.sql` against `DATABASE_URL`
- Register webhook (once): `scripts/set_webhook.sh https://<app>.up.railway.app` (reads `.env`; pass the base URL only — don't repeat `.up.railway.app`)
- Seed the first family member: run `seed.sql` (with your Telegram id) against the DB
- Get your Telegram user id: message `@userinfobot`

## Build phases

Ship one phase at a time. **Build only the current phase.** Its detailed brief is in `docs/` — read that file before starting work on the phase. (The specs are intentionally *not* imported here, so they don't load into every session; open them on demand.)

- **Phase 1 (done)** → `docs/abdo_phase1_spec.md`: Telegram bot, Egyptian-Arabic persona, per-member identity, dog-feeding status, deployed on Railway.
- **Phase 2 (done)** → `docs/abdo_phase2_spec.md`: household knowledge base (RAG over `pgvector`, Cohere `embed-v4.0`).
- **Phase 3 (done)** → `docs/abdo_phase3_spec.md`: shared Google Calendar awareness + Telegram live location.
- **Phase 4 (current)** → `docs/abdo_phase4_voice_spec.md`: Egyptian voice over Telegram (ElevenLabs Scribe STT + Flash v2.5 TTS). Voice round-trip runs in a background task. Proactive nudges still to come. (Wall tablet deferred.)

`schema.sql` already includes the later-phase tables, so adding these phases needs **no migrations** — just new tools, queries, and prompt updates.
