-- Seed the first family member so Abdo recognizes you.
-- Without a matching row, Abdo replies "I don't know you yet" to everyone.
-- Get your numeric Telegram user id by messaging @userinfobot, then replace
-- the placeholder below and run this against the Railway Postgres database.

INSERT INTO family_members (name, arabic_name, telegram_id, role)
VALUES ('Zain', 'زين', <your_telegram_id>, 'member')
ON CONFLICT (telegram_id) DO NOTHING;

-- Verify:
-- SELECT id, name, telegram_id, role FROM family_members ORDER BY id;
