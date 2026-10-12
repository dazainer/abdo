-- Poll tweaks (after 7b.3): the asker can delete a poll (it disappears for everyone).
-- Apply once:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/phase7b3_poll_tweaks.sql
BEGIN;
ALTER TABLE polls ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT false;
COMMIT;
