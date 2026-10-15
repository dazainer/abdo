-- Urgent reminders: sent up to 3 times, 2 minutes apart, until the person taps the
-- notification or messages Abdo. `sends` counts deliveries; `acked_at` is when they saw it.
BEGIN;
ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS urgent   BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sends    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ;
COMMIT;
