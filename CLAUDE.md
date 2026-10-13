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
app/  → main.py (app setup, GET /api/health version, serves web/dist as an SPA, starts the
        scheduler), scheduler.py + notify.py + briefing.py + prayer.py (proactive Abdo),
        money.py (cents math) + location.py (travel mode) + timetable.py (classes, free time) +
        quest.py (Quest parser) + pantry.py (fridge, shopping pings) + polls.py + transit.py (buses),
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
  go through `lib/bidi.tsx`: each line gets its own `dir`, and each sentence (or clause after a
  spaced dash) is a `<bdi>`, so an English sentence inside an Arabic reply keeps its "?" at its
  own end. Don't put `unicode-bidi: plaintext` on those lines (it ignores the isolates and flips
  the line to LTR), and don't use regex lookbehind in frontend code (older iOS Safari can't
  parse it; the whole app goes blank).
- **Design system** (Impeccable): `PRODUCT.md` is the product brief, the Today surface brief and
  direction contract live in `.impeccable/surfaces/`. Today is the home screen (`/`; `/today`
  redirects for old notification links); chat is `/chat`; buses are their own screen
  (`/buses`, from the bus button on Today), never stops on the line. Dark theme only. Every fact
  appears once on Today: today's things are stops on the line, later things are under "Coming
  up". Greetings come from `briefing.greeting()` (the suite's own phrases, weekday/weekend and
  morning/evening, fixed per person per half-day). Prayer names are MSA (الظهر، العشاء), not
  dialect.
- **Today opens from a snapshot** (`web/src/lib/snapshot.ts`): the last me/today/briefing/
  exams/calendar answers are kept in localStorage and shown at once, then refetched. A 401
  always wins over the saved "me", and logout clears it. The briefing also runs its calendar
  read and weather fetch concurrently, and the app's calendar views share a 60 s read cache
  (`read_calendar(..., cached=True)`; Abdo's tools always read fresh; writes clear it).
- **Proactive sends (`scheduler.py`) are claimed in the DB before delivery**: a reminder row is
  flipped to `done` with `UPDATE … WHERE NOT done RETURNING`, daily sends insert into
  `sent_log` (`ON CONFLICT DO NOTHING RETURNING`). Railway's zero-downtime deploys briefly run
  two instances, so never deliver without claiming first. Prayers send only within 20 min of
  their time; a briefing missed by noon is skipped (never send stale). Everything goes through
  `notify.deliver()`: push if it lands, else Telegram, never both. Fajr is silent.
- **Money is integer cents end to end** (`app/money.py`): `Decimal` parsing, exact equal splits
  (remainder cents to the lowest ids), balances that sum to zero, fewest-transfers settle-up.
  The model never does money arithmetic; tools return shares/balances. Deletes are soft and
  only by the payer/recorder (payments: by someone involved).
- **Travel mode** (`app/location.py`): a status may carry a geocoded place (Open-Meteo
  geocoding). While it's unexpired, that member's prayers (scheduler groups members by
  location, each computed in its own timezone), Today prayer times, briefing weather, and
  `get_prayer_times` use the place. Everyone else stays home. `location_for()` is the single
  source of truth.
- **Timetables are parsed deterministically** (`app/quest.py`, no model): Quest's list view, with
  fields on separate lines or tab rows, continuation rows (a second date inherits the section
  above), DD/MM vs MM/DD decided per schedule, 24h or AM/PM. TST slots / one-day rows are
  proposed as EXAMS; TBA/online and dropped/waitlisted courses are skipped with a reason.
  Nothing saves until the member reviews; `validate()` re-checks every field; re-imports don't
  duplicate exams. Test fixtures in `tests/fixtures/` copy the real structure with made-up
  courses, rooms, and names: never commit a real schedule. Free time (`who_is_free`) is computed
  in code from classes (weekday + term range), exams (2 h if no end), and travel ("away" members
  don't block "together"). Exams are quiz/midterm/final only and always shared. A term must be
  2 weeks to 7 months long (catches a day/month swap).
- **Fridge, shopping pings, polls** (`app/pantry.py`, `app/polls.py`): expiry labels computed in
  code; the briefing only shows shared items and your own (never someone else's food).
  "Going shopping" pings everyone else, max once per 30 min per person. Polls: single choice,
  2-6 options, one vote each (change it, or tap your choice again to take it back), closed or
  deleted only by the asker. Closed polls shrink to a results line and drop off after 24 h. Open
  polls ride in Abdo's context so "I want shawarma" becomes a vote.
- **Buses** (`app/transit.py`) come from the GraphQL API behind GRT's departures site
  (`GRT_API_URL`), not GTFS: GRT's GTFS host is down and its realtime is off for everyone. It's
  undocumented, so keep it polite (per-trip query, 60 s cache, fetched only on demand) and never
  invent a time when it fails. A trip matches only if the departure's own trip reaches a
  `to_stop` after the boarding stop. Real stop ids stay in `local/bus_trips.sql`.
- **History is short-term memory, not a to-do list**: `brain._history_messages` keeps the last
  10 turns from the last 3 hours, user turns stamped "[HH:MM] Name:". The persona says answer
  the newest message only. Without this, Abdo re-offered things the person had dropped and
  "corrected" old replies unprompted. Next lever if he still drifts: `EFFORT = "medium"`.
- **`where_is` returns location AND status** (computed in code): the model once ignored a
  status in its context and answered "where's Zain?" with "not sharing location".
- **Prayer times are computed locally** (`prayer.py`, adhanpy, ISNA + standard Asr via
  `PRAYER_METHOD` / `PRAYER_ASR`). Always pass the **local calendar date**: adhanpy reads the
  day from a UTC timestamp, so a Toronto evening would give tomorrow's times.
- **Reminder/status times are resolved in code** (`tools.resolve_due` / `resolve_until`,
  shared by the tools and the web API), exactly like calendar dates.
- **Tests never read the real `.env`**: `tests/smoke.py` chdirs into `tests/` first. Keep it,
  or real keys/push config leak into the suite.
- **Webhook secret never in logs**: `main.RedactWebhookSecret` rewrites `/tg/<secret>` in
  uvicorn's access log. Keep it if logging changes.
- **iPhone audio in the web app** (`web/src/lib/recorder.ts`): iOS suspends/interrupts the
  AudioContext after the mic is used, and `resume()` outside a tap can stay pending until the
  NEXT tap. Never await it unbounded (that queued replies and played them all at once on the
  next mic press); `playMp3` gives up after 400 ms and the bubble keeps a Play button. Call
  `unlockAudio()` in every tap that can lead to audio (mic down AND up, Play). On mic release
  the ORDER matters: release the mic (`Recorder.stop()` stops the tracks synchronously), THEN
  `unlockAudio({ fresh: true })`, both inside the tap. Unlocking first let the closing record
  session interrupt the new context. One voice at a time. `navigator.audioSession.type =
  "playback"` lets it play with the ringer on silent.
- **Installed PWAs keep old code in memory**: `main.tsx` reloads once when a new service
  worker takes over and checks for updates on every return to the foreground. Settings shows
  "App version <build time>", the first thing to check when a phone bug "came back".
- **Prayer times were verified** against the Aladhan API for the suite's location (within 1–2
  min). A consistent offset against someone's Athan app means the app's location is wrong.
- **Secrets are whitespace-stripped** (`str_strip_whitespace` in `config.py`), and a rejected
  webhook logs a warning. A Railway/`.env` secret mismatch shows up in Telegram's
  `getWebhookInfo` as `last_error_message: 403`. After rotating `TELEGRAM_WEBHOOK_SECRET`,
  update Railway AND re-run `scripts/set_webhook.sh`, in that order.
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
- **Phase 6 (done, deployed 2026-10-11)** → `docs/abdo_phase6_webapp_spec.md`: web app (React
  PWA), Telegram-code login, shared DM thread, push infrastructure, Dockerfile.
- **Phase 7a (current: deployed 2026-10-12; briefing card added after the first live briefing)** → `docs/abdo_phase7a_proactive_spec.md`:
  scheduler, reminders/timers, morning briefing, prayer-time notifications, statuses, Today tab. **Phase 7b** (`docs/abdo_phase7b_spec.md`, four parts): 7b.1 money + travel mode and 7b.2
  timetables + exams (deployed 2026-10-12), **7b.3 fridge + going shopping + polls, and the
  deterministic Quest parser** (deployed), **7b.4 buses + a declutter pass (current: built, awaiting deploy)**. **Phase 8** → always-on room
  device (wake word, speaker ID; prove on a Mac before buying hardware). Specs written when each starts.
