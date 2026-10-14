-- Class breaks: days a member's classes don't meet (reading week, a holiday, a skipped
-- week). Classes are skipped on every day from starts_on to ends_on, inclusive.
BEGIN;
CREATE TABLE IF NOT EXISTS class_breaks (
    id          SERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    starts_on   DATE NOT NULL,
    ends_on     DATE NOT NULL,
    label       TEXT NOT NULL DEFAULT 'Break',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends_on >= starts_on)
);
CREATE INDEX IF NOT EXISTS idx_class_breaks_member ON class_breaks (member_id, ends_on);
COMMIT;
