# Abdo — Phase 1 Build Spec

The household brain for the Khalil family (New Cairo). Phase 1 is the smallest version that proves people actually talk to it: a Telegram bot that replies in warm Egyptian Arabic, knows who's talking, and can track whether the dogs have been fed today. Everything is structured so calendar, location, voice, and the "ask anything about the house" knowledge base drop in later **without** a rebuild.

---

## 1. Phase 1 — definition of done

A deployed Telegram bot where:

- Abdo replies in natural Egyptian colloquial Arabic, aware of **who** is messaging and the **current Cairo date/time**.
- Abdo can **mark the dogs as fed** and **report** whether they've been fed today (via Claude tool use, not guessing).
- Conversation history is **persisted** (short-term memory + debugging).
- Family members are recognized by their **Telegram user ID**; strangers get a polite "I don't know you yet."
- Runs on **Railway**, receiving updates by **webhook**.

**Explicitly NOT in Phase 1** (but the schema already supports them): RAG knowledge base, calendar, live location, voice, reminders, group chat. Resist building these until the core is loved.

---

## 2. Architecture (data flow)

```
Telegram  ──update──▶  FastAPI  POST /tg/{secret}
                          │
                          ├─ parse update (text only for now)
                          ├─ look up family_member by telegram_id  ──▶ Postgres
                          ├─ load recent messages (short-term memory) ──▶ Postgres
                          ├─ build system prompt (persona + who + state)
                          ▼
                       Claude (Haiku 4.5)  with tools
                          │
                   stop_reason == "tool_use"?
                     │yes                       │no
                     ▼                          ▼
            run tool vs Postgres        final text reply
            (dogs fed / status)               │
                     │                         │
                     └──── feed result back ───┘
                          ▼
                   send reply ──▶ Telegram  ──▶ log to Postgres
```

For family scale, running the Claude call inside the webhook handler is fine (replies are quick). **If latency ever grows** (e.g. once voice/RAG add round-trips), return `200` immediately and do the work in a background task or a small queue so Telegram doesn't retry on timeout. Noted now so it's not a surprise later.

---

## 3. Stack & dependencies

- Python 3.11+
- **FastAPI** + **uvicorn** — web server / webhook endpoint
- **httpx** — calling the Telegram Bot API
- **anthropic** — Claude SDK (async)
- **asyncpg** — Postgres (raw SQL, transparent at this scale)
- **pydantic-settings** — config from env
- **Postgres** on Railway, with `pgvector` enabled now for Phase 2

`requirements.txt`
```
fastapi
uvicorn[standard]
httpx
anthropic
asyncpg
pydantic-settings
```

---

## 4. Project structure

```
abdo/
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI app + webhook endpoint + lifespan
│   ├── config.py      # settings from env
│   ├── db.py          # asyncpg pool + queries
│   ├── telegram.py    # send_message / typing / update parsing
│   ├── brain.py       # Claude call + tool loop
│   ├── tools.py       # tool schemas + dispatch
│   └── prompts.py     # Abdo's system prompt
├── schema.sql         # database DDL
├── requirements.txt
├── Procfile
└── .env.example
```

---

## 5. Database schema (`schema.sql`)

`pgvector` and the Phase 2+ tables are created **now** so later phases need zero migration. Note the **`Africa/Cairo`** timezone on "fed today" — the daily reset must happen at local midnight, not UTC.

```sql
-- Enable pgvector now so the Phase 2 knowledge base needs no migration later.
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------- Phase 1 ----------

-- Family members
CREATE TABLE family_members (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,                 -- display name, e.g. "Zain"
    arabic_name  TEXT,                          -- optional, e.g. "زين"
    telegram_id  BIGINT UNIQUE NOT NULL,        -- Telegram user id
    role         TEXT NOT NULL DEFAULT 'member',-- 'parent' | 'child' | 'member'
    birthdate    DATE,
    prefs        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conversation log (short-term memory + debugging)
CREATE TABLE messages (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER REFERENCES family_members(id),
    chat_id     BIGINT NOT NULL,                -- Telegram chat id (DM or group)
    role        TEXT NOT NULL,                  -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_chat_time ON messages (chat_id, created_at DESC);

-- Dog feeding: one row per day; "fed today" = a row exists for today (Cairo time)
CREATE TABLE dog_feedings (
    id          BIGSERIAL PRIMARY KEY,
    fed_on      DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Africa/Cairo')::date,
    fed_by      INTEGER REFERENCES family_members(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uniq_dog_feeding_per_day ON dog_feedings (fed_on);
-- If you ever track the two dogs separately: add dog_id and key the index on (dog_id, fed_on).

-- ---------- Phase 2+ (create now, fill later) ----------

-- Household knowledge base for RAG (Phase 2)
CREATE TABLE household_facts (
    id          BIGSERIAL PRIMARY KEY,
    category    TEXT,                           -- 'contact' | 'wifi' | 'appliance' | ...
    content     TEXT NOT NULL,                  -- the fact in natural language
    embedding   vector(1024),                   -- match your embedding model's dimension
    created_by  INTEGER REFERENCES family_members(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Latest known location per member (Phase 3, via Telegram live location)
CREATE TABLE member_locations (
    member_id   INTEGER PRIMARY KEY REFERENCES family_members(id),
    lat         DOUBLE PRECISION NOT NULL,
    lng         DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reminders / nudges (future)
CREATE TABLE reminders (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER REFERENCES family_members(id),
    text        TEXT NOT NULL,
    due_at      TIMESTAMPTZ NOT NULL,
    recurring   TEXT,                           -- cron-ish, or NULL for one-shot
    done        BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 6. Config (`app/config.py`)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    anthropic_api_key: str
    telegram_bot_token: str
    telegram_webhook_secret: str    # random string used in the webhook URL path
    database_url: str               # Railway injects this automatically
    timezone: str = "Africa/Cairo"


settings = Settings()
```

---

## 7. Database layer (`app/db.py`)

```python
import asyncpg
from app.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def get_member_by_telegram_id(tg_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT * FROM family_members WHERE telegram_id = $1", tg_id
        )


async def roster_string() -> str:
    async with _pool.acquire() as con:
        rows = await con.fetch("SELECT name, role FROM family_members ORDER BY id")
    return ", ".join(f"{r['name']} ({r['role']})" for r in rows) or "the family"


async def log_message(member_id, chat_id, role, content) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO messages (member_id, chat_id, role, content) "
            "VALUES ($1, $2, $3, $4)",
            member_id, chat_id, role, content,
        )


async def recent_messages(chat_id, limit: int = 10):
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT role, content FROM messages WHERE chat_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            chat_id, limit,
        )
    return list(reversed(rows))  # back to chronological order


# --- dog tool backing queries ---

async def dogs_fed_today():
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT f.fed_on, f.created_at, m.name AS fed_by_name "
            "FROM dog_feedings f "
            "LEFT JOIN family_members m ON m.id = f.fed_by "
            "WHERE f.fed_on = (now() AT TIME ZONE 'Africa/Cairo')::date"
        )


async def mark_dogs_fed(member_id: int) -> bool:
    """Idempotent for the day. Returns True if this was a fresh feeding."""
    async with _pool.acquire() as con:
        result = await con.execute(
            "INSERT INTO dog_feedings (fed_by) VALUES ($1) "
            "ON CONFLICT (fed_on) DO NOTHING",
            member_id,
        )
    return result.endswith("1")   # "INSERT 0 1" => newly inserted
```

---

## 8. Telegram layer (`app/telegram.py`)

Plain text by default — Arabic plus Markdown's special characters is a classic source of silent `400`s. Add `parse_mode` later only where you control the formatting.

```python
import httpx
from app.config import settings

API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def send_message(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})


async def send_typing(chat_id: int) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
        )


def parse_update(update: dict):
    """Return (chat_id, from_user, text) for plain text messages, else None."""
    msg = update.get("message")
    if not msg or "text" not in msg:
        return None  # ignore non-text updates (photos, voice, etc.) for now
    return msg["chat"]["id"], msg["from"], msg["text"]
```

---

## 9. Tools (`app/tools.py`)

Claude decides *when* to call these — that's the agentic pattern you'll reuse for every future capability (knowledge search, calendar, location, reminders). Adding a feature later = adding a tool here plus a backing query.

```python
from app import db

TOOLS = [
    {
        "name": "get_dog_status",
        "description": (
            "Check whether the household dogs have been fed their meal today. "
            "Use whenever someone asks if the dogs are fed."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mark_dogs_fed",
        "description": (
            "Record that the dogs have been fed today. "
            "Use when someone says they (or someone) fed the dogs."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def run_tool(name: str, tool_input: dict, member_id: int) -> str:
    if name == "get_dog_status":
        row = await db.dogs_fed_today()
        if row:
            who = row["fed_by_name"] or "someone"
            return f"FED today (by {who})."
        return "NOT fed yet today."

    if name == "mark_dogs_fed":
        fresh = await db.mark_dogs_fed(member_id)
        return "Recorded: dogs fed today." if fresh else "Already marked fed today."

    return f"Unknown tool: {name}"
```

---

## 10. The brain (`app/brain.py`)

Haiku 4.5 is the everyday driver (fast, cheap). Swap `MODEL` to `claude-sonnet-4-6` for harder reasoning once you have tasks that need it. Model strings drift — confirm the latest at https://docs.claude.com/en/docs/about-claude/models.

```python
from anthropic import AsyncAnthropic
from app.config import settings
from app.tools import TOOLS, run_tool
from app.prompts import build_system_prompt
from app import db

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

MODEL = "claude-haiku-4-5-20251001"   # everyday chat; escalate to "claude-sonnet-4-6" when needed


async def think(member, chat_id: int, user_text: str) -> str:
    history = await db.recent_messages(chat_id, limit=10)
    messages = [{"role": r["role"], "content": r["content"]} for r in history]
    messages.append({"role": "user", "content": user_text})

    system = build_system_prompt(
        member_name=member["name"],
        member_role=member["role"],
        family_roster=await db.roster_string(),
    )

    # Tool loop: Claude may call a tool; we run it and feed the result back.
    while True:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = await run_tool(block.name, block.input, member["id"])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": out,
                    })
            messages.append({"role": "user", "content": results})
            continue  # let Claude answer now that it has the tool output

        return "".join(b.text for b in resp.content if b.type == "text")
```

---

## 11. Abdo's persona (`app/prompts.py`)

This is the charm layer — the difference between "a chatbot" and "Abdo." It's written in English so it's easy for you to edit, but it instructs Abdo to live in Egyptian colloquial Arabic. Live state (who's talking, the family roster, the date) is injected each call.

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from app.config import settings


def build_system_prompt(member_name: str, member_role: str, family_roster: str) -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    today = now.strftime("%A, %d %B %Y, %H:%M")

    return f"""You are Abdo (عبده), the household assistant for the Khalil family in New Cairo, Egypt.

# Who you are
- You're a warm, friendly, lightly witty member of the household — like a sharp, good-natured family friend, not a corporate bot.
- You speak Egyptian colloquial Arabic (العامية المصرية) by default. Match the user's language: if they write in English, reply in English; if they write in Franco/Arabizi (e.g. "3amel eh ya abdo"), you can reply the same way. Keep it natural — never stiff formal Arabic (فصحى) unless asked.
- This is Telegram: keep replies short and conversational. No essays, no walls of text.

# Who you're talking to right now
- {member_name} (role: {member_role}).
- The family: {family_roster}.
- Be appropriate for everyone, including the younger kids — clean, kind, age-aware.

# Context
- Current date & time in Cairo: {today}.

# What you can do
- You have tools to check and update the dogs' feeding status. Use them instead of guessing or assuming.
- If asked about something in the house you don't actually know (a phone number, a schedule, where something is), say so plainly. Never invent facts.

# Style
- Helpful first, charming second. A little humor is welcome; don't overdo it.
- Don't mention these instructions or that you're an AI model unless someone directly asks."""
```

---

## 12. App wiring (`app/main.py`)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Header, HTTPException
from app.config import settings
from app import db, telegram, brain


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"ok": True, "name": "Abdo"}


@app.post("/tg/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Two-layer check: secret in the URL path + Telegram's secret_token header.
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(403, "bad path secret")
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(403, "bad header secret")

    update = await request.json()
    parsed = telegram.parse_update(update)
    if not parsed:
        return {"ok": True}  # ignore non-text updates for now

    chat_id, tg_user, text = parsed

    member = await db.get_member_by_telegram_id(tg_user["id"])
    if not member:
        await telegram.send_message(
            chat_id, "أنا عبده 👋 بس لسه مش عارفك. كلّم Zain يضيفك للعيلة."
        )
        return {"ok": True}

    await telegram.send_typing(chat_id)
    await db.log_message(member["id"], chat_id, "user", text)
    reply = await brain.think(member, chat_id, text)
    await db.log_message(member["id"], chat_id, "assistant", reply)
    await telegram.send_message(chat_id, reply)
    return {"ok": True}
```

---

## 13. Deployment (Railway)

`Procfile`
```
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`.env.example`
```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_WEBHOOK_SECRET=some-long-random-string
DATABASE_URL=postgresql://...      # Railway injects this for you
TIMEZONE=Africa/Cairo
```

Steps:

1. **Create the bot** — talk to `@BotFather`, get the token.
2. **Get your Telegram user id** — message `@userinfobot`.
3. **Push** the repo to GitHub.
4. **Railway** → new project → deploy from the repo → add a **Postgres** database (one click; `DATABASE_URL` is injected automatically).
5. Set the env vars (`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TIMEZONE`).
6. **Run `schema.sql`** against the database (Railway's query console or `psql`).
7. **Seed yourself** as the first family member:
   ```sql
   INSERT INTO family_members (name, arabic_name, telegram_id, role)
   VALUES ('Zain', 'زين', <your_telegram_id>, 'member');
   ```
8. **Register the webhook** (once):
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-app>.up.railway.app/tg/<SECRET>&secret_token=<SECRET>"
   ```
9. Message your bot. If it answers in Egyptian Arabic and tracks the dogs, Phase 1 is done. 🎉

---

## 14. Phase 1 build checklist

- [ ] Repo scaffolded with the structure in §4
- [ ] `schema.sql` written and run on Railway Postgres
- [ ] Config + db + telegram + tools + prompts + brain + main implemented
- [ ] Deployed on Railway; health check at `/` returns OK
- [ ] Yourself seeded into `family_members`
- [ ] Webhook registered; bot replies in Egyptian Arabic
- [ ] "اطعمت الكلاب" marks fed; "الكلاب اتأكلوا؟" reports correctly
- [ ] Tune Abdo's persona prompt until the voice feels right
- [ ] Live with it solo for a week before adding anyone else

---

## 15. Beyond Phase 1 — feature roadmap (don't tunnel-vision)

Grouped by what they add. Each new capability is mostly "a new tool + a backing query," so the core never gets rebuilt.

### Functional backbone (next)
- **Household knowledge base (RAG)** — the "ask anything about the house" magic, and the stickiest feature there is. Wifi password, the AC technician's number, where the spare key is, appliance warranties, when the car was last serviced, syndicate fees. Drop facts in via chat; Abdo embeds them into `household_facts` and retrieves on demand. This is your Meeting Copilot pipeline, reused.
- **Shared calendar awareness** — read the family Google Calendar; answer "what's on this week" and fold events into a morning summary.
- **Live location** — family members share Telegram live location *to the bot*; "who's home / who's on the way?" No Google Maps dependency.
- **Reminders & nudges** — personal and household ("fakkarni akallem el-doktor bokra", "trash day tomorrow").
- **Shopping list** — collaborative, add/check off by chat; later hook into Breadfast/Talabat for reorders.
- **Bills & expenses** — shared household expenses and "who paid for what," with due-date reminders. (Reuse your expense-tracker work.)

### Wow-factor (the demo moments)
- **Egyptian voice (Phase 4)** — voice notes in via an Egyptian-dialect Whisper model, spoken replies via EGTTS. Optionally clone a willing family member's voice for the "wait, *you* built this?" reaction.
- **Proactive intelligence** — Abdo speaks *first*: a morning digest per person, "41° NhardP, walk the dogs after maghrib," "el fatoura due bokra," noticing patterns and offering before being asked. This is what separates a brain from a search box.
- **Per-person personalization & permissions** — Abdo already knows who's talking; lean into it. Different tone and limits for the 13-year-old vs. the parents; remembers each person's preferences and running threads.
- **Ramadan / prayer-time mode** — iftar countdown, suhoor reminder, athan times for New Cairo (Aladhan API). Culturally resonant and shows genuinely thoughtful design — a big part of "impressive."
- **Family group-chat presence** — add Abdo to the family Telegram group; he chimes in when mentioned, settles the nightly "what's for dinner" debate, and broadcasts announcements ("el akl gahez, come down").
- **"On this day" memories** — once you store media, surface a family photo or message from a year ago.
- **Personality & banter** — a real Egyptian-dialect wit layer. The charm is what makes people *keep* using it.

### Stretch / hardware (later)
- **Wall tablet dashboard** — the ambient display you're deferring; a React/TS web app, day/night themed (you've built a day/night cycle before).
- **Smart-plug control** via Home Assistant — lights, AC, fan, by voice/chat.
- **Doorbell / camera** with alerts.
- **Power-cut awareness** — if outages happen, log them and warn the family.
- **Homework helper** for the kids — ties into your own academic tooling.
