-- Phase 7a (proactive Abdo) migration for a database at the Phase 6 schema.
-- Apply once:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/phase7a_proactive.sql
BEGIN;

ALTER TABLE reminders ADD COLUMN created_by INTEGER REFERENCES members(id);
ALTER TABLE reminders ADD COLUMN sent_at TIMESTAMPTZ;
CREATE INDEX idx_reminders_due ON reminders (due_at) WHERE NOT done;

CREATE TABLE member_status (
    member_id   INTEGER PRIMARY KEY REFERENCES members(id),
    text        TEXT NOT NULL,
    until_at    TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sent_log (
    member_id   INTEGER NOT NULL REFERENCES members(id),
    kind        TEXT NOT NULL,
    local_date  DATE NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, kind, local_date)
);

COMMIT;
