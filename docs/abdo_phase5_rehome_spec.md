# Abdo — Phase 5 Build Spec: Move to the Suite + Brain Upgrade

Abdo leaves the Khalil family and moves in with a four-person student suite in Waterloo:
Zain, Mo, Kateb, Nour. (Real full names, ids, residence, and coordinates are in the gitignored
`local/household.md`; this public spec doesn't repeat them.)
This phase re-homes him (people, place, timezone, persona, data), separates the brain from
Telegram so later channels (web app, room device) plug into the same turn logic, and moves
the brain to Claude Haiku 5.5 with prompt caching. **No new user-facing features yet**,
apart from orders → packages. Those come in Phases 6–8 (see `docs/ROADMAP.md`).

**Exit criteria:** all four roommates are members, Abdo answers correctly on Telegram in
Egyptian/English, voice notes still work, the smoke suite is green, and the conversation log records
who said what on which channel.

---

## 0. Prerequisites (Zain does these, outside the code)

1. **Egypt backup verified:** git tag `egypt-final` + branch `egypt` pushed to GitHub;
   database dump + `egypt.env` saved privately and the dump checked (`pg_restore --list`).
   Nothing in Railway is deleted before this.
2. **New Telegram bot** via @BotFather (walkthrough when we get there): name, username,
   profile picture, and its token. Leave **privacy mode ON** (in a group, Abdo only sees
   messages that mention or reply to him).
3. **Telegram user ids** of all four roommates (each messages @userinfobot).
4. **New Google Calendar** (generic name, e.g. "Suite"), shared with the existing service account's
   email with **"Make changes to events"**. Note its calendar id.
5. **Home coordinates:** drop a pin on the building in Google Maps.

Status (2026-10-11): 1 ✅ · 2 ✅ (token in Zain's password manager) · 3: Zain only so far ·
4 ✅ · 5 ✅. Values are in `local/household.md` and the draft `local/suite.env`.

---

## 1. Infrastructure

- **Reuse the Railway project and app service** (keeps the Anthropic/Cohere/ElevenLabs/Google
  env vars). Add a **fresh Postgres** service with pgvector; point `DATABASE_URL` at it as a
  reference (`${{<NewPostgres>.DATABASE_URL}}`). Delete the old Postgres only after the dump is verified.
- **Retire the old bot's webhook** so Egypt-bot traffic can't reach the new code:
  `curl "https://api.telegram.org/bot<OLD_TOKEN>/deleteWebhook"` (old token is in `egypt.env`).
- **Rotate `TELEGRAM_WEBHOOK_SECRET`** (new random string), set the new `TELEGRAM_BOT_TOKEN`,
  `GOOGLE_CALENDAR_ID`, `HOME_LAT`/`HOME_LNG`, `TIMEZONE=America/Toronto`.
- Apply the new `schema.sql`, run the new `seed.sql`, then `scripts/set_webhook.sh`.
- **Pin dependencies.** `requirements.txt` is currently unpinned, so any Railway rebuild can
  silently pull a breaking major (e.g. `anthropic` 1.x moves to `httpx2`). Pin every package
  to the versions tested in `.venv` (`==`).

---

## 2. Schema (fresh database, replaces `schema.sql` wholesale)

No migration path is needed: it's a new database. Changes from the Egypt schema:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- Suite members (was family_members). Roles/birthdates are gone: four peers.
-- telegram_id is now NULLABLE: web/room identities don't need Telegram (Phase 6/8).
CREATE TABLE members (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,          -- what everyone calls them: 'Zain', 'Mo', 'Kateb', 'Nour'
    full_name    TEXT,                          -- first + last name, for matching only
    arabic_name  TEXT,                          -- 'مو'
    telegram_id  BIGINT UNIQUE,
    prefs        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- briefing time, etc. (Phase 7)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conversation log, now channel- and speaker-aware.
CREATE TABLE messages (
    id                  BIGSERIAL PRIMARY KEY,
    thread              TEXT NOT NULL,          -- 'tg:<chat_id>' | 'web:<member_id>' | 'room'
    channel             TEXT NOT NULL,          -- 'telegram' | 'web' | 'room'
    role                TEXT NOT NULL,          -- 'user' | 'assistant'
    member_id           INTEGER REFERENCES members(id),
        -- user turn: the speaker (NULL = unknown speaker, room device)
        -- assistant turn: the member being answered
    speaker_confidence  REAL,                   -- room speaker-ID score; NULL when identity came from a login
    modality            TEXT NOT NULL DEFAULT 'text',   -- 'text' | 'voice'
    content             TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_thread_time ON messages (thread, created_at DESC);

-- household_facts, member_locations, shopping_items: unchanged except FKs → members(id).

-- Packages (replaces Egypt's orders: no cash-on-delivery / tip in a residence).
CREATE TABLE packages (
    id           BIGSERIAL PRIMARY KEY,
    description  TEXT NOT NULL,                 -- 'Amazon: desk lamp'
    carrier      TEXT,                          -- 'Amazon', 'Canada Post', ...
    owner_id     INTEGER REFERENCES members(id),
    expected_on  DATE,                          -- NULL = unknown
    status       TEXT NOT NULL DEFAULT 'expected',  -- expected | arrived | picked_up | cancelled
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_packages_open ON packages (status) WHERE status IN ('expected', 'arrived');

-- reminders: kept as-is (FK → members) for Phase 7.
-- dog_feedings, orders: removed.
```

**Seeding:** the committed file becomes `seed.example.sql` (placeholder names and ids). The
real `seed.sql` is **gitignored** and generated from `local/household.md`, which holds the
confirmed names, full names, and Arabic spellings. Abdo addresses everyone by `name`;
`full_name` is only for matching (a first or last name → the right member).
Start with **Zain only** (the only Telegram id so far); the others are added with one
`INSERT` each as their ids come in. No redeploy is needed.

---

## 3. Separate the brain from Telegram

Today `main.py` mixes Telegram parsing, identity, logging, and the brain call. Split it so a
turn is channel-agnostic:

```
app/main.py              FastAPI app, GET /, include routers. Nothing else.
app/turn.py              handle_turn(): the one entry point every channel calls.
app/channels/telegram.py APIRouter: POST /tg/{secret} (secret checks, parse, identity,
                         location ping, text → handle_turn, voice → background job).
app/telegram.py          Telegram Bot API client (unchanged).
app/brain.py             think(): the Claude tool loop (model upgrade below).
```

```python
# app/turn.py
async def handle_turn(*, member, text: str, channel: str, thread: str,
                      voice: bool = False, speaker_confidence: float | None = None) -> str:
    """Log the user turn, run the brain, log the reply, return it. Never raises:
    a failure becomes an honest fallback line (channels must never go silent or 500)."""
```

- `thread` keys conversation history: `tg:<chat_id>` (a DM or the suite's group chat),
  `web:<member_id>` (Phase 6), `room` (Phase 8, shared by everyone in the room).
- `brain.think(member, thread, text, voice)` loads history by `thread`. In history, **user
  turns are prefixed with the speaker's name** (`Mo: ...`; `Someone: ...` when unknown) so
  Abdo can follow shared threads (group chat, room). Assistant turns are unprefixed.
- The voice background job becomes: download → `stt.transcribe` → `handle_turn(voice=True)`
  → text-first → `tts.synthesize`. STT/TTS stay channel-agnostic for reuse by web and room.
- Keep every existing guarantee: return `200` fast, voice in `BackgroundTasks`, location
  pings recorded silently, no reply per location update, honest errors instead of silence.
- Unknown sender reply (mixed Egyptian/English): "أنا عبده 👋 مش عارفك لسه — ask Zain to add you."

---

## 4. Brain upgrade: Claude Haiku 5.5

`brain.py`:
- Model: `"claude-haiku-5-5"` (exact string, no date suffix).
- `output_config={"effort": "low"}`. Haiku 5.5 thinks by default (adaptive) and the default
  effort is `medium`; `low` keeps chat snappy for voice. If the live checks in §9 show grounding
  regressions (skipped `recall_facts`, double calendar confirms), move to `"medium"`. The cost difference is negligible.
- **`max_tokens` 600 → 2048.** Thinking tokens count toward `max_tokens`; 600 can be consumed
  by thinking and leave no text.
- Don't send `temperature`/`top_p`/`top_k` (non-default values 400) or assistant prefills
  (400). Neither is used today; keep it that way.
- Read content **by block type** (already done for text). Responses can start with `thinking` blocks.
- Keep appending `resp.content` **unmodified** inside the tool loop (thinking blocks must go
  back exactly as received). Across turns only text is stored in `messages`, so no thinking
  block is ever replayed into an edited history. The system prompt and tools are built once per turn and
  must not change between rounds of the same turn.
- **Handle `stop_reason == "refusal"`** before reading content: log `stop_details`, return an
  honest in-character line. Haiku 5.5 has no server-side fallback; don't send `fallbacks`.
- `stop_reason == "max_tokens"` with no text → log it distinctly (the existing empty-reply
  fallback covers the user side).
- **Web search** stays `web_search_20250305` (the `_20260209` dynamic-filtering variant isn't
  listed for Haiku 5.5). Verify against current docs at build time. Today's date stays in the prompt (needed with search).
- **SDK:** confirm the pinned `anthropic` version supports `output_config` and `claude-haiku-5-5`;
  upgrade within 0.x if needed. A move to 1.x (httpx2) is out of scope for this phase.

---

## 5. Prompt caching

Requests render as `tools → system → messages`; a cache hit needs a byte-identical prefix.
Today the system prompt starts with per-request values (speaker name, date, voice hint), so
nothing after them can be cached. Restructure `prompts.py` into two blocks:

```python
system = [
    {"type": "text", "text": STATIC_PERSONA,            # identical on every request
     "cache_control": {"type": "ephemeral"}},           # caches tools + persona (5-min TTL)
    {"type": "text", "text": dynamic_context(...)},     # speaker, channel, roster, date/time,
]                                                       # date reference, voice hint
```

- `STATIC_PERSONA` must contain **no** names, dates, times, roster, or voice/text variant.
  `ALL_TOOLS` stays a module constant in a fixed order (already true).
- Even a single message benefits: each tool-loop round re-sends the same prefix within seconds.
- Log `usage.cache_read_input_tokens` / `cache_creation_input_tokens` per call at INFO.
  If reads stay 0 across back-to-back messages, something in the prefix is varying.
- Haiku 5.5's minimum cacheable prefix is small (512 tokens per the SDK notes; confirm in the
  docs). Tools + persona are well above it.

---

## 6. Persona (`prompts.py`)

Rewrite the identity sections; keep the hard-won rules.

**Change:**
- Abdo is the assistant for **a four-person student suite in Waterloo, Ontario**. Egyptian
  students and friends. He's their roommate-ish friend: warm, witty, banter welcome. Remove
  the "younger kids" constraint. **No names in the static persona**: who's who comes from
  the roster in the dynamic block (from the `members` table).
- **Language:** mirror the speaker. Default to Egyptian Arabic, switch to English or mix
  freely the way they do; Franco/Arabizi in text when they use it.
- Local context: Waterloo time (`America/Toronto`), CAD (`$`, "تلاتين دولار" in voice),
  Celsius, metric.
- Remove dogs; orders → packages wording.

**Keep (verbatim where possible):** Egyptian dialect rules and fused pronouns; short
speakable replies; no markdown; grounding (`recall_facts` before "I don't know"); honesty
(only "done" after a tool succeeds); calendar rules (spoken day + HH:MM separately, confirm
once, use real ids, never assume empty on a failed read); live-location tense rules; web
search discipline; voice-mode hint incl. ellipsis pauses and the dictation carve-out.

---

## 7. Tools

- **Remove:** `get_dog_status`, `mark_dogs_fed` (+ `db` queries, tests).
- **Replace** `add_order` / `get_orders` / `mark_order_arrived` with:
  - `add_package(description, owner?, carrier?, day?)`: `day` resolved with `calendar_svc.resolve_date`, never computed by the model.
  - `get_packages(status?)`: default: open (expected + arrived), with owner and expected day.
  - `update_package(package_id, status)`: arrived / picked_up / cancelled; real ids only, same error discipline as calendar.
- `where_is`: unchanged logic; `geo.describe` uses the new `HOME_LAT`/`HOME_LNG`. Revisit `HOME_RADIUS_KM`
  for the building's footprint.
- `get_member_id_by_name`: match `name` **or** `full_name`, case-insensitive.
- Update every "Cairo local" wording in tool descriptions/docstrings to the configured
  timezone (descriptions are part of the cached prefix: keep them static, so say "local
  time", not a computed zone).

---

## 8. Config and code cleanup

- `config.py`: `timezone` default `America/Toronto`; drop the Cairo comments.
- `db.py`: remove the hard-coded `'Africa/Cairo'` (it only lived in the dog query, which goes).
- Comments mentioning Cairo/New Cairo in `geo.py`, `tools.py`, `calendar_svc.py` → neutral wording.
- `GET /` version marker → `2026-10-xx-suite-rehome`.

## 8b. Public-repo hygiene

The repo is public and linked from Zain's resume. In this phase:
- **Replace the real Egypt Wi-Fi password** used as an example in `prompts.py` and
  `tests/smoke.py` with an obviously fake one (e.g. `"The Wi-Fi password is sunflower42."`).
  It stays in git history, so the router password itself must be changed. That's Zain's family's call.
- No full names, residence name, coordinates, Telegram ids, or calendar ids in code, prompts,
  tests, or docs. `seed.sql` → gitignored; `seed.example.sql` committed.
- `.env.example`: update comments for the suite (no real values).
- **`README.md` for recruiters** (none exists today): what Abdo is (one paragraph + a
  screenshot/GIF placeholder), architecture diagram (channels → `handle_turn` → Claude tool loop
  → Postgres), notable engineering decisions (code-resolved dates, honest-failure tool results,
  voice as an I/O channel, prompt caching / cost design), stack, how to run locally. Phases 6–8
  as "roadmap". Written in English, no household details.

---

## 9. Tests

`tests/smoke.py` (offline, faked network + DB), updated:
- Drop dog scenarios; add packages scenarios (add → list → update; bad id → honest error).
- `handle_turn` logs user + assistant rows with the right `thread`, `channel`, `member_id`, `modality`.
- History renders speaker prefixes for user turns.
- **Cache invariant:** `STATIC_PERSONA` is byte-identical across different speakers, times,
  and voice/text. Dynamic values appear only in the second block.
- Refusal stop reason → in-character fallback, no crash; `max_tokens` with no text → fallback.
- `resolve_date` / event formatting correct in `America/Toronto` (incl. a DST boundary date).
- Fake brain responses can include a leading `thinking` block; text extraction still works.

Live checks after deploy (Telegram, all four roommates):
- [ ] Each roommate is recognized by name; an unknown account gets the polite refusal.
- [ ] Mixed Egyptian/English in → natural mixed reply; English in → English.
- [ ] "What's the wifi password?" after storing it → `recall_facts` path, correct answer.
- [ ] Calendar create/move/delete with a single confirmation on the new suite calendar; weekdays correct.
- [ ] Voice note in → text first, then voice reply (Cairene voice), numbers as words.
- [ ] Group chat (optional): Abdo answers only when mentioned/replied to; history shows speakers.
- [ ] Railway logs show non-zero `cache_read_input_tokens` on back-to-back messages.

---

## 10. Build checklist

- [ ] Prereqs §0 done (backup verified first)
- [x] `requirements.txt` pinned; SDK supports Haiku 5.5 + `output_config` (0.109.1, no upgrade needed)
- [x] New `schema.sql` + `seed.example.sql` (real `seed.sql` gitignored)
- [x] `turn.py`, `channels/telegram.py`, thin `main.py`
- [x] `brain.py`: Haiku 5.5, effort, max_tokens, refusal, cache-usage logging
- [x] `prompts.py`: static/dynamic split + suite persona (no names in the static block)
- [x] Public-repo hygiene §8b: fake examples, gitignored seed, README
- [x] Tools: dogs out, packages in, timezone wording
- [x] Smoke tests green (169 checks; `count_tokens` confirms the model id, cached prefix ≈ 10.2K tokens)
- [ ] Railway: new Postgres, env vars, schema + seed, webhook set, old webhook deleted
- [ ] Live checks §9
- [ ] CLAUDE.md: mark Phase 5 done, Phase 6 current
