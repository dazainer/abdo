# Abdo (عبده)

A conversational household assistant that speaks **Egyptian Arabic mixed with English**,
knows everyone in the household, and does real things for them: remembers household facts,
manages a shared Google Calendar, tracks packages and the shopping list, knows who's home,
searches the web, and talks back in a Cairene voice.

It started as an assistant for my family in Cairo and now runs a four-person student suite
in Waterloo. Today it lives in Telegram (text and voice notes); a web app and an always-on
room speaker are on the [roadmap](docs/ROADMAP.md).

<!-- TODO: demo GIF / screenshot of a mixed Arabic-English voice exchange -->

## Architecture

```mermaid
flowchart LR
    TG[Telegram<br/>text · voice · live location] --> CH[Channel adapter<br/>secrets · identity · thread]
    WEB[Web app<br/>planned] -.-> CH
    ROOM[Room speaker<br/>planned] -.-> CH
    CH --> TURN[handle_turn]
    TURN --> BRAIN[Claude tool loop<br/>Haiku 5.5]
    BRAIN <--> TOOLS[Tools]
    TOOLS --> PG[(Postgres + pgvector)]
    TOOLS --> GCAL[Google Calendar]
    TOOLS --> COH[Cohere embeddings]
    BRAIN --> WS[Web search<br/>server-side tool]
    TURN --> LOG[(Conversation log<br/>thread · channel · speaker)]
```

**Every capability is a Claude tool.** Adding a feature means adding a tool definition,
its query, and any table. The core loop never changes. Channels are thin adapters. The brain
doesn't know whether a turn came from a typed message or a voice note, apart from a flag that
keeps spoken replies short.

## Engineering decisions worth a look

- **Dates are resolved in code, not by the model.** LLMs get weekday arithmetic wrong
  ("Wednesday" → a Thursday). The model passes the day exactly as spoken ("next Friday",
  "بكرة", "7ad"), and [`calendar_svc.resolve_date`](app/calendar_svc.py) turns it into a real
  local date. Event weekdays are rendered by code too, so the model never computes one.
- **Tool failures are honest, never silent.** A degraded calendar read raises instead of
  returning "no events". An empty-but-wrong answer once led the model to create duplicate
  events. Tool errors go back to the model as explicit `ERROR:` results ("do NOT claim it
  worked"), and the model may only say "done" after a tool reports success.
- **Voice is an I/O channel, not a feature.** Speech-to-text produces text for the same brain;
  text-to-speech speaks the reply. The round trip runs as a background task after the webhook
  returns `200`, because Telegram retries slow webhooks, which caused duplicate replies.
  Replies containing digits are routed to a TTS model that normalizes numbers, and the voice
  prompt spells passwords out character by character.
- **Built for cost.** A Haiku-class model with low reasoning effort and prompt caching. The
  system prompt is split into a byte-identical persona block (cached together with the tool
  definitions, about 10K tokens) and a small per-request context block. A test guards the
  invariant, because one timestamp in the static block would silently disable every cache hit.
- **RAG that doesn't silently miss.** Facts are embedded asymmetrically (document vs query),
  deduplicated by cosine distance, and never stored without a validated 1024-dim vector.
  [`embeddings.is_valid`](app/embeddings.py) accepts pgvector's numpy arrays, a bug that once
  made every stored fact look invalid.
- **Speaker-aware memory.** Conversations are keyed by thread (a DM, a group chat, later the
  shared room), and each logged turn records its channel, modality and speaker. Abdo can follow
  a group conversation and knows who asked what.

## Stack

Python 3.11+ · FastAPI · asyncpg (raw SQL, no ORM) · Postgres + pgvector · Anthropic API
(Claude Haiku 5.5, server-side web search) · Cohere Embed v4 · Google Calendar API ·
ElevenLabs (Scribe STT, Flash TTS) · ffmpeg · deployed on Railway.

## Running it locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in keys; see comments in the file
psql "$DATABASE_URL" -f schema.sql
cp seed.example.sql seed.sql  # add your household, then:
psql "$DATABASE_URL" -f seed.sql
.venv/bin/uvicorn app.main:app --reload
```

Register the Telegram webhook with `scripts/set_webhook.sh https://<your-app>.up.railway.app`.

**Tests:** `.venv/bin/python tests/smoke.py` runs the turn logic, tool loop, tools, prompt
invariants and webhook routing offline, with every network service and the database faked.

## Project layout

```
app/
  main.py            FastAPI app + health/version endpoint
  channels/          channel adapters (telegram.py)
  turn.py            handle_turn — the single entry point for every channel
  brain.py           Claude tool loop (model, effort, caching, refusal/fallback handling)
  prompts.py         persona (static, cached) + per-request context
  tools.py           tool definitions + dispatcher
  db.py              raw SQL queries
  calendar_svc.py    Google Calendar + deterministic date resolution
  embeddings.py      Cohere embeddings + validation
  stt.py, tts.py     ElevenLabs speech in/out
docs/                roadmap and phase specs (docs/egypt/ = the original family edition)
tests/smoke.py       offline test suite
```
