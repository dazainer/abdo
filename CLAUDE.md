# CLAUDE.md — Abdo

Abdo (عبده) is a conversational assistant for a four-person student suite in **Waterloo, Ontario**
(Zain, Mo, Kateb, Nour). He speaks Egyptian colloquial Arabic mixed with English, the way the suite does. He's reached
through **Telegram** today; his own web app and an always-on room device are planned.

This file is the project's durable memory. The plan of record is **`docs/ROADMAP.md`**;
phase-by-phase build briefs live in `docs/`.

> **Transition in progress.** Abdo was built for the Khalil family in New Cairo. That
> version is frozen at git tag **`egypt-final`** / branch **`egypt`** (specs archived in
> `docs/egypt/`). Until Phase 5 is built, the code on `main` still reflects Egypt: Cairo
> timezone default, dog feeding, orders, `family_members`, Haiku 4.5. Phase 5 changes all of that.

## The one principle that governs everything

**Every capability is a Claude tool.** The request flow is fixed:

```
channel (Telegram today; web app + room device later)
   → identify the member → handle_turn → build system prompt → Claude (with tools)
   → tool loop runs actions against Postgres → reply on the same channel → log to Postgres
```

Adding a feature = adding a tool definition (`tools.py`) + its backing query (`db.py`) + any
tables (`schema.sql`). **Never refactor the core loop to add a feature: extend the tool set.**
Channels are thin adapters; the brain never knows which channel a turn came from (beyond
`voice=True` shortening replies for the ear). Phase 5 introduces `handle_turn` as the single
entry point all channels call. Until then the Telegram webhook in `main.py` calls `brain.think` directly.

## Stack

- Python 3.11+, **FastAPI** + uvicorn
- **httpx** (Telegram, ElevenLabs), **anthropic** SDK (async), **asyncpg** (Postgres, raw SQL)
- **pydantic-settings** (config); **Postgres + pgvector** on **Railway** (Hobby)
- Brain: Anthropic API. Current code: `claude-haiku-4-5-20251001`. **Phase 5 target: `claude-haiku-5-5`**
  with `effort: low` and prompt caching.
- Embeddings: Cohere `embed-v4.0` at **1024 dims**.
- Voice: ElevenLabs Scribe (STT) + Flash v2.5 (TTS); ffmpeg for OGG/Opus.

## Project layout

```
app/  → main.py (FastAPI + Telegram webhook), config.py, db.py, telegram.py, brain.py,
        tools.py, prompts.py, embeddings.py, calendar_svc.py, geo.py, stt.py, tts.py
        (Phase 5 adds turn.py and channels/telegram.py; main.py becomes app setup only)
schema.sql, seed.sql, requirements.txt, Procfile, nixpacks.toml, .env.example
scripts/ → set_webhook.sh, backfill_embeddings.py
tests/smoke.py → offline smoke suite (network + DB faked)
docs/ → ROADMAP.md, phase specs; docs/egypt/ = archived Egypt-era specs
```

## Conventions

- Async throughout (FastAPI, asyncpg, AsyncAnthropic).
- Raw SQL via asyncpg at this scale: **no ORM**.
- Secrets only via env vars (see `.env.example`); never hardcode tokens or keys. Never commit
  `.env`, service-account JSON, database dumps, or notes containing household secrets.
- **The GitHub repo is public (it's on Zain's resume). Keep the code household-agnostic:** no
  full names, residence name, addresses, coordinates, Telegram ids, or real passwords in code,
  tests, prompts, or docs. People live in the `members` table, place/calendar in env vars. Use
  obviously fake examples in prompts and tests. Private details go in the gitignored `local/`
  folder (`local/household.md` has the real roster, ids, and calendar).
- Abdo's persona lives in `prompts.py`. Behavior/voice changes go there, not scattered through the code.
- Conversation history is persisted to `messages` (Phase 5: keyed by `thread`, with channel + speaker).
- Bump the `VERSION` marker in `main.py` (served at `GET /`) with each deploy-worthy change.
- Run `.venv/bin/python tests/smoke.py` before committing; add scenarios for new tools.

## Critical gotchas — IMPORTANT

- **Timezone**: all "today"/scheduling logic uses the configured `TIMEZONE`, never UTC. The suite
  is `America/Toronto` (DST applies: test around the switch dates). Never hard-code a zone in SQL.
- **Telegram + Arabic**: send **plain text** (no `parse_mode`) by default. Arabic plus Markdown
  special characters causes silent HTTP 400s.
- **Webhook latency**: Telegram retries a slow webhook → duplicate replies. Return `200` fast;
  anything slow (voice round-trip) runs in a `BackgroundTasks` job. Never 500 and never go
  silent: reply with an honest error and return `200`.
- **Model strings drift**: confirm model IDs against Anthropic's docs before pinning. No model
  escalation to paper over grounding bugs: fix grounding in code and prompt (the Sonnet
  escalation was retired for this reason).
- **Haiku 5.5 specifics (Phase 5)**: thinking is on by default and **counts toward
  `max_tokens`** (keep it ≥ 2048); responses can start with `thinking` blocks (read by type);
  pass assistant content back unmodified inside the tool loop; no `temperature`/`top_p`/`top_k`,
  no prefill (both 400); handle `stop_reason == "refusal"` (no server-side fallback on Haiku).
- **Prompt caching (Phase 5)**: the cached prefix is `tools` + the static persona block. Anything
  per-request (speaker, date/time, roster, voice hint) goes in the second, uncached system
  block. A timestamp in the static block silently kills every cache hit.
- **Embedding dimension is fixed at 1024**: `household_facts.embedding` is `vector(1024)`.
  `search_document` on store, `search_query` on recall: never swap them. If recall misses a
  known fact, suspect a NULL/bad embedding row → `scripts/backfill_embeddings.py`.
- **Railway `DATABASE_URL` is not auto-shared**: set it on the app service as a reference
  `${{<PostgresServiceName>.DATABASE_URL}}`, or asyncpg crashes at startup on an empty host.
- **Calendar** via a service account the calendar is shared with ("Make changes to events").
  Scope `calendar.events` (deliberately not `calendar`). `googleapiclient` is blocking → wrap
  in `asyncio.to_thread`. Module is `calendar_svc` (not `calendar`: stdlib shadowing). Dates are
  resolved **in code** (`calendar_svc.resolve_date`); the model passes the spoken day + HH:MM,
  never a computed date. A failed read is never treated as an empty calendar. Abdo confirms
  once before any create/edit/delete (persona rule).
- **Never reply per location update**: live-location edits stream in every few seconds.
  Record and return `200`; only `where_is` reports position, with fresh/stale tense baked in by code.
- **Voice is an I/O channel, not a tool**: STT produces text the same brain answers; TTS
  speaks the reply. The Egyptian accent comes from `TTS_VOICE_ID`, **not** the default Arabic
  voice (which leans Gulf/MSA). `scribe_v1` is gone; use `scribe_v2`. Flash v2.5 can't normalize
  numbers: `tts.py` routes digit-bearing replies to `eleven_multilingual_v2`, and the voice
  prompt writes numbers as words and spells codes out (dictation carve-out: no filler inside a
  password/code). ElevenLabs is the biggest running cost: prefer pre-recorded clips and the
  TTS cache (Phase 8) over new synthesis.
- **`sendVoice` needs OGG/Opus; ElevenLabs returns MP3** → ffmpeg transcode (`nixpacks.toml`
  `aptPkgs`). An MP3 sent to `sendVoice` won't render as a voice note.
- **Unpinned dependencies**: `requirements.txt` is unpinned until Phase 5 pins it. A rebuild can
  pull a breaking major (e.g. `anthropic` 1.x → `httpx2`).

## Commands

- Install: `pip install -r requirements.txt`
- Run locally: `uvicorn app.main:app --reload`
- Smoke tests: `.venv/bin/python tests/smoke.py`
- Apply schema: run `schema.sql` against `DATABASE_URL`; seed members with `seed.sql`
- Register webhook: `scripts/set_webhook.sh https://<app>.up.railway.app` (reads `.env`; base URL only)
- Telegram user id: message `@userinfobot`

## Build phases

Ship one phase at a time. **Build only the current phase**, and read its spec before starting.
The full plan, decisions log, and later-phase outlines are in `docs/ROADMAP.md`.

- **Phases 1–4 (done, Egypt edition)** → `docs/egypt/`: Telegram bot, persona, identity, RAG
  facts, Google Calendar, live location, Egyptian voice, shopping list, orders, web search.
- **Phase 5 (current)** → `docs/abdo_phase5_rehome_spec.md`: move to the Waterloo suite: fresh DB, suite
  members, `America/Toronto`, persona rewrite, brain/channel separation (`handle_turn`),
  speaker-aware logs, Haiku 5.5 + prompt caching, packages, new Telegram bot.
- **Phase 6** → web app (PWA). **Phase 7** → scheduler, daily briefing, prayer times, timers,
  status, expenses, timetables, deadlines, groceries, polls, buses. **Phase 8** → always-on room
  device (wake word, speaker ID; prove on a Mac before buying hardware). Specs written when each starts.
