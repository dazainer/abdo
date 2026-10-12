-- Phase 7b.1 (expenses + travel mode) migration for a database at the Phase 7a schema.
-- Apply once:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/phase7b1_money_travel.sql
BEGIN;

CREATE TABLE expenses (
    id            BIGSERIAL PRIMARY KEY,
    description   TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_by       INTEGER NOT NULL REFERENCES members(id),
    created_by    INTEGER REFERENCES members(id),
    spent_on      DATE NOT NULL,
    deleted       BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE expense_shares (
    expense_id    BIGINT NOT NULL REFERENCES expenses(id),
    member_id     INTEGER NOT NULL REFERENCES members(id),
    share_cents   INTEGER NOT NULL CHECK (share_cents >= 0),
    PRIMARY KEY (expense_id, member_id)
);

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

ALTER TABLE member_status ADD COLUMN place TEXT;
ALTER TABLE member_status ADD COLUMN place_lat DOUBLE PRECISION;
ALTER TABLE member_status ADD COLUMN place_lng DOUBLE PRECISION;
ALTER TABLE member_status ADD COLUMN place_tz TEXT;

COMMIT;
