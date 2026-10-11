-- Template for seeding the suite's members. Copy to seed.sql (gitignored), fill in
-- real names and Telegram ids, then run it against the database:
--   psql "$DATABASE_URL" -f seed.sql
-- Get a numeric Telegram user id by messaging @userinfobot. telegram_id may be NULL
-- until that person has one on file; Abdo still knows them by name in the roster.

INSERT INTO members (name, full_name, arabic_name, telegram_id) VALUES
    ('Alex', 'Alex Example', 'أليكس', 111111111),
    ('Sam',  'Sam Example',  'سام',   NULL)
ON CONFLICT (name) DO NOTHING;

-- Later, when someone's Telegram id comes in:
--   UPDATE members SET telegram_id = 222222222 WHERE name = 'Sam';

-- Verify:
--   SELECT id, name, full_name, arabic_name, telegram_id FROM members ORDER BY id;
