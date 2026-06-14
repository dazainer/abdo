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

The brain is the Anthropic API: **Haiku 4.5** as the everyday driver, escalating specific calls to **Sonnet 4.6** only when reasoning genuinely demands it.

## Stack

- Python 3.11+, **FastAPI** + uvicorn (webhook server)
- **httpx** (Telegram Bot API), **anthropic** SDK (async), **asyncpg** (Postgres, raw SQL)
- **pydantic-settings** (config); **Postgres + pgvector** on **Railway**
- Interface: Telegram only. Deploy target: Railway (Hobby plan).

## Project layout

```
app/  → main.py (FastAPI + webhook), config.py, db.py,
        telegram.py, brain.py, tools.py, prompts.py
schema.sql, requirements.txt, Procfile, .env.example
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
- **Model strings drift**: confirm current model IDs at https://docs.claude.com/en/docs/about-claude/models before pinning. Currently `claude-haiku-4-5-20251001` and `claude-sonnet-4-6`.

## Commands

- Install: `pip install -r requirements.txt`
- Run locally: `uvicorn app.main:app --reload`
- Apply schema: run `schema.sql` against `DATABASE_URL`
- Register webhook (once): `curl ".../setWebhook?url=https://<app>.up.railway.app/tg/<SECRET>&secret_token=<SECRET>"`
- Get your Telegram user id: message `@userinfobot`

## Build phases

Ship one phase at a time. **Build only the current phase.** Its detailed brief is in `docs/` — read that file before starting work on the phase. (The specs are intentionally *not* imported here, so they don't load into every session; open them on demand.)

- **Phase 1 (current)** → `docs/phase1_spec.md`: Telegram bot, Egyptian-Arabic persona, per-member identity, dog-feeding status, deployed on Railway.
- **Phase 2** → household knowledge base (RAG over `pgvector`).
- **Phase 3** → shared Google Calendar awareness + Telegram live location.
- **Phase 4** → Egyptian voice (Whisper-Egyptian STT + EGTTS) and proactive nudges. (Wall tablet deferred.)

`schema.sql` already includes the later-phase tables, so adding these phases needs **no migrations** — just new tools, queries, and prompt updates.
