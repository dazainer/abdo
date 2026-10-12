# CLAUDE.md — Abdo

Abdo (عبده) is a conversational assistant for a four-person student suite in **Waterloo, Ontario**
(Zain, Mo, Kateb, Nour). He speaks Egyptian colloquial Arabic mixed with English, the way the suite does. He's reached
through **Telegram** today; his own web app and an always-on room device are planned.

This file is the project's durable memory. The plan of record is **`docs/ROADMAP.md`**;
phase-by-phase build briefs live in `docs/`.

> **History.** Abdo was first built for the Khalil family in New Cairo. That version is
> frozen at git tag **`egypt-final`** / branch **`egypt`** (specs archived in `docs/egypt/`).
> Phase 5 (the move to the suite) is **built on `main` but not yet deployed**: Railway
> still runs the Egypt code until the cutover in `docs/abdo_phase5_rehome_spec.md` §1.

## The one principle that governs everything

**Every capability is a Claude tool.** The request flow is fixed:

```
channel (Telegram today; web app + room device later)
   → identify the member → handle_turn → build system prompt → Claude (with tools)
   → tool loop runs actions against Postgres → reply on the same channel → log to Postgres
```

Adding a feature = adding a tool definition (`tools.py`) + its backing query (`db.py`) + any
tables (`schema.sql`). **Never refactor the core loop to add a feature: extend the tool set.**
Channels (`app/channels/`) are thin adapters: verify, identify the member, pick a `thread`
key, call `turn.handle_turn`, deliver the reply. `handle_turn` is the single entry point: it
runs `brain.think`, logs the turn **afterwards** (so history never holds the current message
twice), and never raises. The brain only knows the channel as a label in its context, plus
`voice=True` for shorter, spoken replies. History is keyed by `thread`: `dm:<member_id>` is a
member's personal thread **shared by their Telegram DM and the web app**; `tg:<chat_id>` is a
Telegram group; `room` (Phase 8) is the shared speaker. User turns reach the model prefixed
with the speaker's name. The web app's lists/packages/calendar screens are plain views over
the same tables (same `db`/`calendar_svc` functions as the tools). Only chat goes through
Claude.

## Stack

- Python 3.11+, **FastAPI** + uvicorn
- **httpx** (Telegram, ElevenLabs), **anthropic** SDK (async), **asyncpg** (Postgres, raw SQL)
- **pydantic-settings** (config); **Postgres + pgvector** on **Railway** (Hobby)
- Brain: Anthropic API, `claude-haiku-5-5`, `effort: low`, prompt caching, server-side
  `web_search_20250305`. Dependencies are pinned in `requirements.txt` (anthropic 0.109.1).
- Embeddings: Cohere `embed-v4.0` at **1024 dims**.
- Voice: ElevenLabs Scribe (STT) + Flash v2.5 (TTS); ffmpeg for OGG/Opus.

## Project layout

```
app/  → main.py (app setup, GET /api/health version, serves web/dist as an SPA),
        channels/telegram.py (webhook adapter), channels/web.py (/api for the web app),
        auth.py (Telegram-code login + sessions), push.py (web push), turn.py
        (handle_turn), brain.py (tool loop), prompts.py (static persona + dynamic context),
        tools.py, db.py, telegram.py (Bot API client), calendar_svc.py, embeddings.py,
        geo.py, stt.py, tts.py, config.py
web/  → React + TypeScript PWA (Vite): src/pages, src/components, src/sw.ts (service
        worker), src/api.ts (typed client), styles.css (design tokens)
schema.sql (fresh DB), migrations/ (changes for the live DB), seed.example.sql (real
seed.sql is gitignored), requirements.txt (pinned), Dockerfile, .env.example, README.md
local/ → gitignored private notes: real roster, ids, calendar id, draft env
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
- Conversation history is persisted to `messages`, keyed by `thread`, with channel, speaker, and modality.
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
- **Haiku 5.5 specifics**: thinking is on by default and **counts toward `max_tokens`**
  (keep it ≥ 2048); responses can start with `thinking` blocks (read by type); pass assistant
  content back unmodified inside the tool loop, and never change `system`/`tools` between rounds
  of one turn; no `temperature`/`top_p`/`top_k`, no prefill (both 400); handle
  `stop_reason == "refusal"` (no server-side fallback on Haiku). If grounding slips, raise
  `brain.EFFORT` to `"medium"` before touching anything else.
- **Prompt caching**: the cached prefix is `tools` + `prompts.STATIC_PERSONA` (~10K tokens).
  Anything per-request (speaker, date/time, roster, channel, voice hint) goes in
  `dynamic_context()`, after the cache point. A timestamp or name in the static block silently
  kills every cache hit (the smoke suite guards this). Check `cache_read` in the `usage:` logs.
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
- **`sendVoice` needs OGG/Opus; ElevenLabs returns MP3** → ffmpeg transcode (installed
  in the `Dockerfile`). An MP3 sent to `sendVoice` won't render as a voice note.
- **Pinned dependencies**: bump versions deliberately and re-run the smoke suite. `anthropic`
  1.x moves to `httpx2` and is a separate migration. Frontend versions are pinned in
  `web/package-lock.json` (React Router 7, TypeScript 5.9 on purpose).
- **Web login** is a 6-digit code DM'd by Telegram (works inside installed iPhone apps, where
  OAuth/magic-link redirects land in Safari). Only hashes of codes/session tokens are stored
  (`SESSION_PEPPER`). `request-code` always answers `ok`: never reveal who's a member. Every
  mutating `/api` call needs the `X-Abdo: 1` header (CSRF), and the session cookie is
  HttpOnly + Secure + SameSite=Lax.
- **Don't let refetches unmount the login screen**: the `me` query has
  `refetchOnWindowFocus: false`. Switching to Telegram to read the code and back used to reset
  the code step.
- **Mixed Arabic/English text**: use `unicode-bidi: plaintext` so each line reads in its own
  direction. In lists, also pin `text-align: left`, or Arabic rows flush right. Chat bubbles
  keep `dir="auto"` alignment on purpose. A trailing "?" landing on the left in Arabic-first
  lines is correct bidi behaviour, not a bug.
- **Webhook secret never in logs**: `main.RedactWebhookSecret` rewrites `/tg/<secret>` in
  uvicorn's access log. Keep it if logging changes.
- **Service workers don't run in the desktop app's built-in preview browser**. Test the
  PWA/push in real Chrome or on a phone. iOS push only works from the Home Screen app (16.4+).
- **Deploy is the `Dockerfile`** (Node builds `web/`, Python 3.13 runs the app, ffmpeg
  installed). `GET /` serves the web app; the version marker is at `GET /api/health`. Don't
  add a Procfile or a Railway custom start command: Railway runs those without a shell, so
  `$PORT` arrives literally ("'$PORT' is not a valid integer"). The Dockerfile `CMD` goes
  through `sh -c` and expands it.

## Commands

- Install: `pip install -r requirements.txt` and `cd web && npm ci`
- Run locally: `uvicorn app.main:app --reload` (API) + `cd web && npm run dev` (Vite proxies `/api`)
- **Demo mode** (no DB/Claude/Telegram; fake data, login codes printed to the terminal):
  `cd web && npm run build && cd .. && .venv/bin/python tests/demo_server.py` → http://localhost:8000
- Smoke tests: `.venv/bin/python tests/smoke.py` · frontend: `cd web && npm run typecheck && npm test && npm run build`
- Apply a migration to the live DB: `psql "$URL" -v ON_ERROR_STOP=1 -f migrations/<file>.sql`
- Push keys (once): `.venv/bin/python scripts/gen_vapid.py` · app icons: `cd web && npm run icons`
- Apply schema: `psql "$DATABASE_URL" -f schema.sql`; seed members with the gitignored
  `seed.sql` (template: `seed.example.sql`); add a member's Telegram id later with an `UPDATE`
- Register webhook: `scripts/set_webhook.sh https://<app>.up.railway.app` (reads `.env`; base URL only)
- Telegram user id: message `@userinfobot`

## Build phases

Ship one phase at a time. **Build only the current phase**, and read its spec before starting.
The full plan, decisions log, and later-phase outlines are in `docs/ROADMAP.md`.

- **Phases 1–4 (done, Egypt edition)** → `docs/egypt/`: Telegram bot, persona, identity, RAG
  facts, Google Calendar, live location, Egyptian voice, shopping list, orders, web search.
- **Phase 5 (deployed 2026-10-11; calendar live check + roommates' ids pending)** →
  `docs/abdo_phase5_rehome_spec.md`: move to the Waterloo suite: fresh DB, suite members,
  `America/Toronto`, persona rewrite, brain/channel separation (`handle_turn`), speaker-aware
  logs, Haiku 5.5 + prompt caching, packages, new Telegram bot, public-repo hygiene.
  Watch item: occasional MSA sentences since the move. Note the triggering message if it recurs.
- **Phase 6 (current: built, awaiting deploy + phone checks)** → `docs/abdo_phase6_webapp_spec.md`:
  web app (React PWA), Telegram-code login, shared DM thread, push infrastructure, Dockerfile. **Phase 7** → scheduler, daily briefing, prayer times, timers,
  status, expenses, timetables, deadlines, groceries, polls, buses. **Phase 8** → always-on room
  device (wake word, speaker ID; prove on a Mac before buying hardware). Specs written when each starts.
