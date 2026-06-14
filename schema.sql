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
