-- Phase 7b.2 (timetables + exams) migration for a database at the Phase 7b.1 schema.
-- Apply once:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/phase7b2_schedule.sql
BEGIN;

CREATE TABLE classes (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    course      TEXT NOT NULL,
    section     TEXT,
    kind        TEXT,
    weekday     SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    starts      TIME NOT NULL,
    ends        TIME NOT NULL,
    room        TEXT,
    term_start  DATE NOT NULL,
    term_end    DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends > starts),
    CHECK (term_end >= term_start)
);
CREATE INDEX idx_classes_member ON classes (member_id);

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

COMMIT;
