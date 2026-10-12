-- Phase 7b.3 (fridge + polls) migration for a database at the Phase 7b.2 schema.
-- Apply once:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/phase7b3_food_polls.sql
BEGIN;

CREATE TABLE pantry_items (
    id          BIGSERIAL PRIMARY KEY,
    item        TEXT NOT NULL,
    owner_id    INTEGER REFERENCES members(id),
    expires_on  DATE,
    added_by    INTEGER REFERENCES members(id),
    used        BOOLEAN NOT NULL DEFAULT false,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_pantry_open ON pantry_items (expires_on) WHERE NOT used;

CREATE TABLE polls (
    id          BIGSERIAL PRIMARY KEY,
    question    TEXT NOT NULL,
    options     JSONB NOT NULL,
    created_by  INTEGER REFERENCES members(id),
    closed      BOOLEAN NOT NULL DEFAULT false,
    closed_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE poll_votes (
    poll_id       BIGINT NOT NULL REFERENCES polls(id),
    member_id     INTEGER NOT NULL REFERENCES members(id),
    option_index  INTEGER NOT NULL,
    voted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (poll_id, member_id)
);

COMMIT;
