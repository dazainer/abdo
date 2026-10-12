-- Phase 6 (web app) migration for a database created from the Phase 5 schema.
-- Apply once:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/phase6_webapp.sql
-- (A fresh database gets all of this from schema.sql instead.)
BEGIN;

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

CREATE TABLE web_sessions (
    token_hash    TEXT PRIMARY KEY,
    member_id     INTEGER NOT NULL REFERENCES members(id),
    user_agent    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Private Telegram chats now share one personal thread with the web app:
-- 'tg:<chat_id>' for a private chat becomes 'dm:<member_id>' (a private chat's id
-- equals the member's Telegram user id). Group chats keep 'tg:<chat_id>'.
UPDATE messages m
   SET thread = 'dm:' || mem.id
  FROM members mem
 WHERE mem.telegram_id IS NOT NULL
   AND m.thread = 'tg:' || mem.telegram_id;

COMMIT;
