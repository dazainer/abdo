-- Phase 7b.4: saved bus/ION trips. The real stop ids (they point at the suite's area) are
-- inserted from the gitignored local/bus_trips.sql, never committed.
BEGIN;
CREATE TABLE IF NOT EXISTS bus_trips (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,                -- "Groceries", "Mall -> home"
    from_stops  TEXT[] NOT NULL,                     -- GRT stop ids you board at (any of them)
    to_stops    TEXT[] NOT NULL DEFAULT '{}',        -- where you get off; empty = just departures
    routes      TEXT[] NOT NULL DEFAULT '{}',        -- limit to these routes; empty = any that gets there
    position    INTEGER NOT NULL DEFAULT 0           -- display order
);
COMMIT;
