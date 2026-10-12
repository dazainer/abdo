-- Abdo schema (suite edition). Apply once to a fresh database:
--   psql "$DATABASE_URL" -f schema.sql
-- The Egypt-era schema lives at git tag egypt-final.

CREATE EXTENSION IF NOT EXISTS vector;

-- Suite members. Four peers, no roles. telegram_id is nullable: a member can exist
-- before they've messaged the bot, and web/room identities don't need Telegram.
CREATE TABLE members (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,          -- what everyone calls them, e.g. "Mo"
    full_name    TEXT,                          -- first + last name, for matching only
    arabic_name  TEXT,                          -- e.g. "مو"
    telegram_id  BIGINT UNIQUE,
    prefs        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- briefing time etc. (Phase 7)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conversation log (short-term memory + debugging), channel- and speaker-aware.
CREATE TABLE messages (
    id                  BIGSERIAL PRIMARY KEY,
    thread              TEXT NOT NULL,          -- 'tg:<chat_id>' | 'web:<member_id>' | 'room'
    channel             TEXT NOT NULL,          -- 'telegram' | 'web' | 'room'
    role                TEXT NOT NULL,          -- 'user' | 'assistant'
    member_id           INTEGER REFERENCES members(id),
        -- user turn: the speaker (NULL = unknown speaker on the room device)
        -- assistant turn: the member being answered
    speaker_confidence  REAL,                   -- room speaker-ID score; NULL when identity came from a login
    modality            TEXT NOT NULL DEFAULT 'text',   -- 'text' | 'voice'
    content             TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_thread_time ON messages (thread, created_at DESC);

-- Household knowledge base (RAG). Cohere embed-v4.0 at 1024 dims: only swap to
-- another 1024-dim embedder.
CREATE TABLE household_facts (
    id          BIGSERIAL PRIMARY KEY,
    category    TEXT,                           -- 'contact' | 'wifi' | 'appliance' | ...
    content     TEXT NOT NULL,                  -- the fact as one self-contained sentence
    embedding   vector(1024),
    created_by  INTEGER REFERENCES members(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Latest known location per member (Telegram live location).
CREATE TABLE member_locations (
    member_id   INTEGER PRIMARY KEY REFERENCES members(id),
    lat         DOUBLE PRECISION NOT NULL,
    lng         DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Shared shopping list: "open" = not yet bought.
CREATE TABLE shopping_items (
    id          BIGSERIAL PRIMARY KEY,
    item        TEXT NOT NULL,
    qty         TEXT,                           -- free text, optional ("2 bags")
    added_by    INTEGER REFERENCES members(id),
    bought      BOOLEAN NOT NULL DEFAULT false,
    bought_by   INTEGER REFERENCES members(id),
    bought_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- At most one open (unbought) entry per item name, case-insensitive.
CREATE UNIQUE INDEX uniq_open_shopping_item ON shopping_items (lower(item)) WHERE NOT bought;

-- Packages / deliveries for the suite.
CREATE TABLE packages (
    id           BIGSERIAL PRIMARY KEY,
    description  TEXT NOT NULL,                 -- "desk lamp"
    carrier      TEXT,                          -- "Amazon", "Canada Post", ...
    owner_id     INTEGER REFERENCES members(id),
    expected_on  DATE,                          -- NULL = unknown
    status       TEXT NOT NULL DEFAULT 'expected',  -- expected | arrived | picked_up | cancelled
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_packages_open ON packages (status) WHERE status IN ('expected', 'arrived');

-- ---------- Web app (Phase 6; live DBs get these via migrations/phase6_webapp.sql) ----------

-- One-time login codes, delivered by Telegram DM. Only the hash is stored.
CREATE TABLE login_codes (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    code_hash   TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    used        BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_login_codes_member ON login_codes (member_id, created_at DESC);

-- Web sessions (cookie holds a random token; only its hash is stored).
CREATE TABLE web_sessions (
    token_hash    TEXT PRIMARY KEY,
    member_id     INTEGER NOT NULL REFERENCES members(id),
    user_agent    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

-- Web push subscriptions (one per installed app / browser).
CREATE TABLE push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Proactive Abdo (Phase 7a; live DBs get these via migrations/phase7a_proactive.sql) ----------

-- Reminders / timers. The scheduler claims a due row (done = true) before delivering it.
CREATE TABLE reminders (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER REFERENCES members(id),  -- who gets reminded
    text        TEXT NOT NULL,
    due_at      TIMESTAMPTZ NOT NULL,
    recurring   TEXT,                            -- reserved (7a reminders are one-shot)
    done        BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  INTEGER REFERENCES members(id),  -- who asked for it
    sent_at     TIMESTAMPTZ
);
CREATE INDEX idx_reminders_due ON reminders (due_at) WHERE NOT done;

-- One current status per member ("in a midterm", "away in Toronto"), optionally expiring.
CREATE TABLE member_status (
    member_id   INTEGER PRIMARY KEY REFERENCES members(id),
    text        TEXT NOT NULL,
    until_at    TIMESTAMPTZ,                     -- NULL = until cleared
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Travel mode (7b.1): while the status lasts, this member's prayers/briefing use the place.
    place       TEXT,                            -- "Montreal, Canada" (from the geocoder)
    place_lat   DOUBLE PRECISION,
    place_lng   DOUBLE PRECISION,
    place_tz    TEXT                             -- IANA timezone of the place
);

-- Daily sends (briefing, each prayer) are claimed here first: one per member per local day.
CREATE TABLE sent_log (
    member_id   INTEGER NOT NULL REFERENCES members(id),
    kind        TEXT NOT NULL,                   -- 'briefing' | 'prayer:<name>'
    local_date  DATE NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, kind, local_date)
);

-- ---------- Money (Phase 7b.1; live DBs get these via migrations/phase7b1_money_travel.sql) ----------

-- A shared expense: who paid, how much (integer cents), split into exact shares below.
CREATE TABLE expenses (
    id            BIGSERIAL PRIMARY KEY,
    description   TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_by       INTEGER NOT NULL REFERENCES members(id),
    created_by    INTEGER REFERENCES members(id),
    spent_on      DATE NOT NULL,
    deleted       BOOLEAN NOT NULL DEFAULT false,   -- soft delete keeps history auditable
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE expense_shares (
    expense_id    BIGINT NOT NULL REFERENCES expenses(id),
    member_id     INTEGER NOT NULL REFERENCES members(id),
    share_cents   INTEGER NOT NULL CHECK (share_cents >= 0),
    PRIMARY KEY (expense_id, member_id)
);

-- Money moving between roommates outside an expense ("I paid Mo back $20").
CREATE TABLE settlements (
    id            BIGSERIAL PRIMARY KEY,
    from_member   INTEGER NOT NULL REFERENCES members(id),
    to_member     INTEGER NOT NULL REFERENCES members(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    created_by    INTEGER REFERENCES members(id),
    settled_on    DATE NOT NULL,
    deleted       BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_member <> to_member)
);

-- ---------- Schedules (Phase 7b.2; live DBs get these via migrations/phase7b2_schedule.sql) ----------

-- Weekly classes, one row per meeting day (imported from a pasted Quest schedule, confirmed
-- by the member before saving). weekday: Monday = 0 (Python's date.weekday()).
CREATE TABLE classes (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    course      TEXT NOT NULL,                  -- "MATH 136"
    section     TEXT,                           -- "001"
    kind        TEXT,                           -- LEC / TUT / LAB / ...
    weekday     SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    starts      TIME NOT NULL,                  -- local time
    ends        TIME NOT NULL,
    room        TEXT,
    term_start  DATE NOT NULL,
    term_end    DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends > starts),
    CHECK (term_end >= term_start)
);
CREATE INDEX idx_classes_member ON classes (member_id);

-- Quizzes, midterms, finals. Always visible to the suite: the point is letting the others know.
CREATE TABLE exams (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    course      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('quiz', 'midterm', 'final')),
    starts_at   TIMESTAMPTZ NOT NULL,
    ends_at     TIMESTAMPTZ,
    room        TEXT,
    created_by  INTEGER REFERENCES members(id),
    deleted     BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_exams_upcoming ON exams (starts_at) WHERE NOT deleted;
