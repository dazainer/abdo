# Abdo (عبده) — Project Summary

## 1. Overview

Abdo (عبده) is a conversational household assistant for the **Khalil family** in New Cairo, Egypt. It speaks Egyptian colloquial Arabic (العامية المصرية), knows the family members by name and role, and is accessed **entirely through Telegram** — there is no web UI or wall display yet. Family members interact by **text, voice notes, and shared live location**; Abdo answers in the same modality (voice in → voice out). Every capability — feeding the dogs, remembering household facts, the calendar, the shopping list, tracking deliveries, web search — is exposed to the language model as a tool, so the assistant is driven by one fixed request loop rather than bespoke feature code.

## 2. Tech stack + hosting

- **Language:** Python 3.11+
- **Web framework:** FastAPI + uvicorn (single webhook server)
- **HTTP client:** httpx (Telegram Bot API, ElevenLabs STT/TTS)
- **Database:** PostgreSQL + **pgvector**, driven with **asyncpg** (raw SQL, no ORM)
- **LLM SDK:** `anthropic` (AsyncAnthropic)
- **Config:** pydantic-settings (env vars, `.env`)
- **Hosting:** Railway (Hobby plan); DB is Railway Postgres
- **System dependency:** `ffmpeg` (installed via `nixpacks.toml` aptPkgs) for MP3→OGG/Opus transcoding

**Model IDs currently in use:**

| Role | Provider | Model ID |
|------|----------|----------|
| Brain (every turn) | Anthropic | `claude-haiku-4-5-20251001` |
| Web search (server-side tool) | Anthropic | `web_search_20250305` (max 3 uses/turn) |
| Embeddings | Cohere | `embed-v4.0` at **1024 dims** |
| STT | ElevenLabs Scribe | `scribe_v2` (env-swappable; `scribe_v1` removed 2026-07-09) |
| TTS | ElevenLabs Flash | `eleven_flash_v2_5` (default) |
| TTS fallback for digit-bearing replies | ElevenLabs | `eleven_multilingual_v2` |

Note: the earlier **Sonnet 4.6 escalation** for calendar turns is **retired** — Haiku 4.5 now drives everything; calendar reliability was fixed in code, not by a bigger model.

## 3. Architecture

**The one principle: every capability is a Claude tool.** Adding a feature = a tool definition (`tools.py`) + its backing query (`db.py`) + any tables (`schema.sql`). The core loop is never refactored to add a feature; the tool set is extended and the webhook handler stays thin.

**Request flow (end to end):**

```
Telegram webhook (POST /tg/{secret})
  → FastAPI verifies path secret + X-Telegram-Bot-Api-Secret-Token header
  → parse update (text | location | voice)
  → load member by telegram_id from Postgres (unknown sender → polite refusal)
  → build system prompt (persona + roster + date reference + voice hint)
  → brain.think(): Anthropic Haiku with tools
      → tool loop (max 8 rounds): model calls tool → run_tool() hits Postgres/
        Google/Cohere/web → result fed back → repeat until a text answer
  → reply via Telegram
  → log user + assistant turns to `messages`
```

**Voice as an I/O channel, not a tool.** `brain.py` / `tools.py` / `db.py` / the persona are unchanged for voice. STT (`stt.py`) turns a voice note into text the same brain answers; TTS (`tts.py`) speaks the reply back. `voice=True` only shortens the reply and adds spoken-delivery guidance. The voice round-trip (STT → brain → TTS) runs in a **FastAPI BackgroundTask after returning `200`**, because it takes several seconds — longer than Telegram's webhook patience, which would otherwise trigger retries and duplicate replies.

**Special update handling:**
- **Location** (live-location edits arrive as `edited_message`, streaming every few seconds): recorded silently via `upsert_location`, returns `200`, **never** replies. Only the `where_is` tool reports position.
- **Voice**: dispatched to the background task; `200` returned immediately.
- **Errors**: the handler never 500s or goes silent (both cause Telegram retries/duplicates) — it replies with an honest Arabic error and returns `200`.

## 4. File / module map

**`app/`**
- `main.py` — FastAPI app; `GET /` health/version marker; `POST /tg/{secret}` webhook (two-layer secret check, routes text/location/voice); `handle_voice()` background job.
- `config.py` — pydantic-settings `Settings`: all env vars and their defaults.
- `db.py` — asyncpg pool + all raw-SQL queries (members, messages, dogs, facts, locations, shopping list, orders).
- `telegram.py` — Telegram Bot API client: send message/typing/recording/voice, file download, and `parse_update()` (typed text/location/voice payloads).
- `brain.py` — the Anthropic tool loop: `think()`, model constant `HAIKU`, `WEB_SEARCH_TOOL`, `ALL_TOOLS`, `MAX_TOOL_ROUNDS = 8`, `pause_turn` handling for server-side web search.
- `tools.py` — the `TOOLS` list (all client-side tool schemas) + `run_tool()` dispatcher; event formatting (`_format_event`), location descriptions with fresh/stale tense, fact dedup constant.
- `prompts.py` — `build_system_prompt()`: full Egyptian-Arabic persona, dialect rules, grounding rules, date reference, and the voice hint.
- `embeddings.py` — Cohere Embed v4 client; `embed()` (asymmetric `search_document`/`search_query` input types) and `is_valid()` (type-robust 1024-dim check).
- `calendar_svc.py` — Google Calendar service account client; deterministic `resolve_date()`, time parsing, get/create/update/delete events; `CalendarReadError` / `EventNotFound`. Named `calendar_svc` to avoid shadowing stdlib `calendar`.
- `geo.py` — haversine distance from home; `describe()` → "home" or "X km from home".
- `stt.py` — ElevenLabs Scribe speech-to-text (`transcribe()`).
- `tts.py` — ElevenLabs Flash TTS + ffmpeg MP3→OGG/Opus transcode; digit-bearing replies routed to `eleven_multilingual_v2`; volume boost + limiter.
- `__init__.py` — package marker (empty).

**`scripts/`**
- `backfill_embeddings.py` — scan `household_facts`, re-embed rows with missing/invalid embeddings (`--all`, `--dry-run`). Fixes silent recall misses from NULL/bad vectors.
- `set_webhook.sh` — register/verify the Telegram webhook from `.env` values; masks the secret in output.

## 5. Database schema (`schema.sql`)

`CREATE EXTENSION vector` is enabled up front. All later-phase tables exist already, so adding phases needs no migrations.

| Table | Purpose |
|-------|---------|
| `family_members` | One row per person: name, arabic_name, unique `telegram_id`, role (parent/child/member), birthdate, `prefs` JSONB. Identity + roster. |
| `messages` | Conversation log (short-term memory + debugging): member_id, chat_id, role, content, timestamp. Indexed on (chat_id, created_at DESC). |
| `dog_feedings` | One row per day (unique index on `fed_on`); "fed today" = a row exists for today in Cairo time. `fed_by` → member. |
| `household_facts` | RAG knowledge base: category, content, `embedding vector(1024)`, created_by. |
| `member_locations` | Latest live location per member (PK member_id): lat, lng, updated_at. |
| `shopping_items` | Shared shopping list: item, qty, added_by, bought flag + bought_by/bought_at. Partial unique index on `lower(item) WHERE NOT bought` (one open entry per item). |
| `orders` | Incoming online orders/deliveries: description, expected_on, ordered_by, `paid` (prepaid vs COD), tip_note, status (pending/arrived/cancelled). Indexed on expected_on. |
| `reminders` | Reminders/nudges (schema present, **not yet wired to any tool** — future proactive nudges). |

## 6. Full tool inventory

**Client-side tools (in `tools.py`, dispatched by `run_tool`):**

- `get_dog_status` — check whether the dogs have been fed today.
- `mark_dogs_fed` — record that the dogs were fed today (idempotent per day).
- `remember_fact` — store a durable household fact (embeds, dedups by cosine distance, updates in place if near-identical).
- `recall_facts` — vector-search stored facts before answering "I don't know".
- `get_calendar` — read upcoming shared-calendar events (default 7 days).
- `create_event` — add a calendar event (after confirmation; passes spoken day, not a computed date).
- `update_event` — change/reschedule an event by id (preserves duration on a move).
- `delete_event` — delete an event by id (after confirmation).
- `where_is` — report a member's live location (or `everyone`), with fresh/stale tense baked in.
- `add_to_shopping_list` — add one item to the shared list.
- `get_shopping_list` — show open shopping items.
- `mark_item_bought` — mark one item bought/removed.
- `clear_shopping_list` — mark everything bought (after confirmation).
- `add_order` — record an incoming order/delivery (what, when, who, prepaid vs COD, tip).
- `get_orders` — list pending orders expected on a day (default today).
- `mark_order_arrived` — flip a pending order to arrived.

**Server-side tool (Anthropic-hosted, no client handler):**

- `web_search` (`web_search_20250305`, max 3 uses/turn) — current/outside info (weather, news, prices, hours); results synthesized in the same turn.

## 7. External integrations + auth

- **Telegram Bot API** — bot token (`TELEGRAM_BOT_TOKEN`) in the API base URL. Webhook secured two ways: a random secret embedded in the URL path (`/tg/{secret}`) **and** the same value echoed in the `X-Telegram-Bot-Api-Secret-Token` header; both must match `TELEGRAM_WEBHOOK_SECRET`.
- **Anthropic** — `ANTHROPIC_API_KEY` via AsyncAnthropic. Drives the brain and the server-side web search.
- **Cohere** — `COHERE_API_KEY` via AsyncClientV2 for Embed v4 (1024-dim). Store uses `search_document`, recall uses `search_query`; swapping them wrecks retrieval.
- **ElevenLabs** — one `ELEVENLABS_API_KEY` covers both Scribe (STT) and Flash (TTS); passed as the `xi-api-key` header. The Egyptian accent comes from the chosen `TTS_VOICE_ID`, not the model.
- **Google Calendar** — a **service account** the family calendar is shared with ("Make changes to events"). Key JSON in `GOOGLE_SERVICE_ACCOUNT_JSON`; scope is `calendar.events` (least-privilege writable, deliberately not the broader `calendar`). Blocking `googleapiclient` calls are wrapped in `asyncio.to_thread`.

## 8. Environment variables

| Var | Controls |
|-----|----------|
| `ANTHROPIC_API_KEY` | Anthropic API auth (brain + web search). Required. |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API auth. Required. |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook URL path secret + header secret (both checked). Required. |
| `DATABASE_URL` | Postgres connection. On Railway set as a reference `${{Postgres.DATABASE_URL}}` (not auto-shared). Required. |
| `COHERE_API_KEY` | Cohere Embed v4 auth (facts KB). Optional (facts disabled if unset). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google service-account key JSON as a string. Optional (calendar off if unset). |
| `GOOGLE_CALENDAR_ID` | The shared calendar id. Optional. |
| `HOME_LAT` / `HOME_LNG` | Home coordinates for the geofence / distance-from-home. Optional. |
| `TIMEZONE` | Timezone for all "today"/reset logic. Default `Africa/Cairo`. |
| `ELEVENLABS_API_KEY` | ElevenLabs STT + TTS auth. Optional (voice off if unset). |
| `TTS_VOICE_ID` | The Cairene voice used for TTS. Optional. |
| `TTS_MODEL` | TTS model. Default `eleven_flash_v2_5`. |
| `STT_MODEL` | STT model. Default `scribe_v2`. |
| `STT_LANGUAGE` | STT language. Default `ar`. |
| `VOICE_REPLIES` | Reply with voice when the user sent voice. Default `true`. |
| `REPLY_TEXT_ALONGSIDE_VOICE` | Send text first, then the voice note. Default `true`. |
| `MAX_VOICE_SECONDS` | Ignore voice notes longer than this. Default `60`. |

## 9. Feature / phase history

- **Phase 1 (done):** Telegram bot, Egyptian-Arabic persona, per-member identity, dog-feeding daily status (Cairo-time reset), deployed on Railway. Tables: `family_members`, `messages`, `dog_feedings`.
- **Phase 2 (done):** Household knowledge base — RAG over pgvector with Cohere Embed v4 (1024-dim), `remember_fact` / `recall_facts`, dedup + valid-embedding guards. Table: `household_facts`.
- **Phase 3 (done):** Shared Google Calendar awareness (read + create/update/delete via service account, deterministic date resolution) and Telegram live location (`where_is` with fresh/stale tense). Tables: `member_locations`.
- **Phase 4 (current):** Egyptian voice over Telegram — ElevenLabs Scribe STT + Flash v2.5 TTS, background round-trip, text-first-then-voice, digit-aware model routing, ffmpeg transcode, Cairene spoken-delivery prompt (ellipsis/dash pauses, spelled-out codes). Proactive nudges still to come; wall tablet deferred.

**Later features layered on top (via new tools, no migration):**
- **Shared shopping list** — add/list/mark-bought/clear (`shopping_items`).
- **Orders / deliveries ("Fix 1")** — track incoming orders with prepaid/COD and tip (`orders`).
- **Web search** — Anthropic server-side `web_search` for current/outside info.

**Still in progress / not yet wired:** proactive nudges/reminders (the `reminders` table exists but no tool uses it); wall tablet / web UI deferred.

## 10. Known open or contained issues

- **Google Calendar degraded reads (Regional Access Boundary / `FAILED_PRECONDITION`):** a failed read must never be treated as an empty calendar — that would let the model create a duplicate of an event it just couldn't see. Contained by `CalendarReadError`: reads retry once, then return an explicit "couldn't reach the calendar, try again" error that stops the turn instead of proceeding on empty.
- **Silent recall misses from bad embeddings:** a fact stored with a NULL/zero/wrong-dimension embedding is invisible to vector search. Contained by the type-robust `embeddings.is_valid()` (accepts numpy ndarray/list), a store-time guard in `remember_fact`, and the `scripts/backfill_embeddings.py` repair script.
- **TTS number normalization:** Flash v2.5 skips number/date normalization, so digit strings read wrong. Contained by (a) the voice prompt writing ordinary numbers as words and spelling out codes, and (b) routing any digit-bearing reply through `eleven_multilingual_v2`.
- **`orders` table in prod:** the `orders` table was added to `schema.sql` and must be applied against the deployed `DATABASE_URL` before the order tools work in production (per CLAUDE.md — verify it's been run).
- **Empty model replies / Telegram 400s:** an empty text reply is caught in `brain.think()` and replaced with a graceful Arabic fallback so the user never sees silence; `send_message` logs non-200s loudly rather than failing the webhook.
- **Duplicate replies from webhook retries:** avoided by always returning `200` fast (voice in background, errors handled inline, `pause_turn` resumed correctly).

## 11. Current `GET /` version marker

```
VERSION = "2026-06-27-web-search-and-dictation-carveout"
```

`GET /` returns `{"ok": true, "name": "Abdo", "version": "2026-06-27-web-search-and-dictation-carveout"}` so the exact deployed code can be confirmed from outside.
