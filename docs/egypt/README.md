# Abdo — Egypt edition (archived)

These are the build specs and summary for the original Abdo: the Khalil family's
household assistant in New Cairo (Phases 1–4, Fix 1, shopping list, orders, web search).

They describe the code at git tag **`egypt-final`** (also kept on the **`egypt`** branch),
not the current `main`, which has moved to the Waterloo student suite. Read them as history.

## Redeploying the Egypt version

1. `git checkout egypt-final` (or the `egypt` branch) in a fresh clone/worktree.
2. Restore the database from the private backup dump (kept outside git, never committed):
   `pg_restore --no-owner --no-acl -d "$NEW_DATABASE_URL" abdo_egypt_<date>.dump`
   (the target Postgres needs the `pgvector` extension available). The dump was taken from
   a Postgres **18** server, so restore with a v18+ client
   (`/opt/homebrew/opt/postgresql@18/bin/pg_restore` on Zain's Mac).
3. Restore the Egypt env vars from the private `egypt.env` backup (Telegram bot token,
   webhook secret, Google service account, calendar id, home coords, ElevenLabs voice).
4. Deploy to Railway and run `scripts/set_webhook.sh https://<app>.up.railway.app`.

Note: `scribe_v2`, `claude-haiku-4-5-20251001`, and the ElevenLabs voice id were current
as of mid-2026. Check that they are still served before redeploying.
