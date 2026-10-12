# Abdo — Phase 6 Build Spec: Abdo's Web App (PWA)

Abdo gets his own app: an installable web app (PWA) served by the **same FastAPI service** on
Railway. It has no app store, no fees, and no second deployment. It's a second **channel** next to Telegram
(same `handle_turn`, same brain), plus a few screens that show the suite's shared state
(lists, packages, calendar) directly.

**Exit criteria:** all four roommates can install the app on their phones, log in, chat with
Abdo by text and voice (sharing history with their Telegram DM), and see and edit the shopping
list and packages. Push notifications can be enabled and a test push arrives. Smoke tests and the frontend build are green.

---

## 0. Decisions (2026-10-11)

| Topic | Decision | Why |
|---|---|---|
| Frontend | **React + TypeScript + Vite**, built into static files that FastAPI serves | Grows cleanly into the Phase 7 dashboard and Phase 8 room screen; strong resume signal. |
| Login | **6-digit code delivered by Telegram DM** → long-lived session cookie | Works inside installed iPhone apps (OAuth/magic-link redirects land in Safari, not the app); no passwords, no new provider. Telegram is only the code's *delivery*: the web channel stays independent. Passkeys can be added later on top. |
| Chat history | **One personal thread per member** shared by Telegram DMs and the web app | Abdo remembers across devices. Group chats and the room stay separate threads. |
| Push | **Infrastructure only**: subscribe/unsubscribe + a test push. No real triggers until Phase 7. | |
| Deploy | **Dockerfile** (multi-stage: Node builds the frontend, Python runs the app) replaces Nixpacks | One explicit, reproducible build for two languages; ffmpeg installed in the image. |

---

## 1. Hardening first: stop logging the webhook secret

Uvicorn's access log prints every request path, and the Telegram webhook path contains the secret
(`/tg/<secret>`). It's currently visible in Railway logs.
- Add a `logging.Filter` on the `uvicorn.access` logger that rewrites `/tg/<anything>` →
  `/tg/<redacted>` (keep access logs, lose the secret).
- **Rotate** `TELEGRAM_WEBHOOK_SECRET` (new value in Railway + local `.env`), redeploy, re-run
  `scripts/set_webhook.sh`. Zain does the Railway/terminal part.

---

## 2. Architecture

```
Browser / installed PWA (React)
   │  fetch /api/*  (session cookie)
   ▼
FastAPI ── app/channels/web.py   (APIRouter /api: auth, chat, voice, lists, packages, calendar, push)
       ├── app/channels/telegram.py
       ├── turn.handle_turn  ← both channels
       └── StaticFiles: web/dist (the built React app) + SPA fallback to index.html
```

- **Abdo is still only reachable through `handle_turn`.** The list/package/calendar screens are
  *views* over the same tables, so the API handlers call the **same `db.py` functions** the tools
  use (no duplicated SQL, no second source of truth). Checking an item off in the app is the same
  row Abdo sees. No Claude call is made for plain UI actions (that would cost money for nothing).
- API routes are all under `/api`; everything else serves the SPA, so client-side routes
  (`/chat`, `/lists`) work on refresh and when opened from the home screen.

---

## 3. Unified personal thread

- New thread key for 1:1 conversations: **`dm:<member_id>`**.
- Telegram: a **private** chat → `dm:<member_id>`; a **group/supergroup** → `tg:<chat_id>` (unchanged).
  `parse_update` adds `chat_type` from `message.chat.type`.
- Web chat → `dm:<member_id>`.
- Replies are delivered on the channel the message came from. A message sent in the app is
  answered in the app, but appears in history on both (the app shows a small "via Telegram" tag).
- **One-time data fix** in the live DB: `UPDATE messages SET thread = 'dm:1' WHERE thread = 'tg:<Zain's chat id>'`
  (only test messages exist). Documented as a step for Zain, not run by the app.

---

## 4. Auth: Telegram-delivered code + session

**Tables:**
```sql
CREATE TABLE login_codes (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    code_hash   TEXT NOT NULL,                   -- sha256(code + server pepper), never the code
    expires_at  TIMESTAMPTZ NOT NULL,            -- 10 minutes
    attempts    INTEGER NOT NULL DEFAULT 0,      -- max 5, then dead
    used        BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE web_sessions (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the cookie token
    member_id    INTEGER NOT NULL REFERENCES members(id),
    user_agent   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL            -- 90 days, sliding
);
```

**Flow:**
1. `POST /api/auth/request-code {name}`: the member types their name (matched with
   `get_member_id_by_name`). If the member exists **and has a Telegram id**, generate a random
   6-digit code, store its hash, and DM it: "كود الدخول بتاعك: 123456 — valid 10 minutes. If
   this wasn't you, ignore it." **Always** respond `{"ok": true}` regardless, so the endpoint can't
   be used to discover who lives in the suite. Rate limit: max 3 codes per member per 15 minutes.
2. `POST /api/auth/verify {name, code}`: constant-time compare against the latest unused,
   unexpired code; 5 wrong attempts kill it. On success: create a session, set cookie
   `abdo_session` (random 32-byte token; `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=90d`).
3. Every `/api/*` route except `auth/*` requires a valid session (FastAPI dependency
   `current_member`). Sliding expiry: `last_seen_at`/`expires_at` refreshed at most once per day.
4. `POST /api/auth/logout` deletes the session. `GET /api/me` returns `{id, name, arabic_name}`.
- **CSRF:** `SameSite=Lax` cookie + every mutating request must send `Content-Type: application/json`
  or `X-Abdo: 1` (the voice upload). Browsers won't send those cross-site without CORS, and no CORS is enabled.
- New env var `SESSION_PEPPER` (random string) for hashing codes/tokens.

---

## 5. API (`app/channels/web.py`)

| Method | Path | Does |
|---|---|---|
| POST | `/api/auth/request-code` | §4 |
| POST | `/api/auth/verify` | §4 |
| POST | `/api/auth/logout` | §4 |
| GET | `/api/me` | current member |
| GET | `/api/chat/history?before=<id>&limit=30` | the member's `dm:` thread, newest page first; each row: id, role, content, channel, modality, created_at |
| POST | `/api/chat` `{text}` | `handle_turn(channel="web", thread="dm:<id>")` → `{reply}` |
| POST | `/api/chat/voice` (multipart audio) | STT → `handle_turn(voice=True)` → `{transcript, reply, audio}` where `audio` is base64 MP3 (or `null` if TTS failed: degrade to text) |
| GET | `/api/shopping` · POST `{item, qty?}` · POST `/api/shopping/bought {item}` | wraps the existing db functions |
| GET | `/api/packages` · POST `{description, owner?, carrier?, day?}` · PATCH `/api/packages/{id} {status}` | same validation rules as the tools (day via `resolve_date`, owner via name lookup) |
| GET | `/api/calendar?days=7` | read-only upcoming events, weekday rendered in code; a degraded read returns an error state, never an empty list |
| GET | `/api/push/public-key` · POST `/api/push/subscribe` · POST `/api/push/unsubscribe` · POST `/api/push/test` | §7 |

- Package/shopping validation logic lives in **one place**: extract the bits of `tools.run_tool`
  the API needs (day resolution, owner lookup) into small shared helpers rather than re-implementing them.
- Voice: browsers record `audio/webm;codecs=opus` (Chrome/Android) or `audio/mp4` (Safari/iOS);
  Scribe accepts both, so pass the upload through with its real content type. Cap uploads at
  `MAX_VOICE_SECONDS` / ~2 MB. `tts.py` gains `synthesize_mp3()` (browsers play MP3 natively; the
  OGG/Opus transcode stays Telegram-only).

---

## 6. Frontend (`web/`)

**Stack:** Vite + React 19 + TypeScript (strict), React Router, TanStack Query (fetching, caching,
polling), `vite-plugin-pwa` (manifest + service worker via `injectManifest`, so push handling is ours).
No UI kit: a small hand-written CSS design system (CSS variables, light/dark).

**Screens (mobile-first, bottom tab bar):**
1. **Login:** name → code (6 boxes, numeric keypad, auto-submit). Bilingual microcopy.
2. **Chat (home tab):** message list with `dir="auto"` per bubble (Arabic right-to-left, English
   left-to-right, mixed handled by the browser), "via Telegram" tag, infinite scroll up, typing
   indicator while waiting. **Hold-to-talk** mic button: record → upload → show transcript bubble,
   then Abdo's text, then auto-play the reply audio. Polls history every ~5 s while visible, so a
   Telegram message shows up.
3. **Lists:** shopping list (add, tap to mark bought) and packages (grouped Arrived / Expected,
   add form, mark arrived / picked up).
4. **Calendar:** next 7 days, read-only, grouped by day. Changes go through Abdo, who confirms.
5. **Settings:** name, push notifications toggle + "send test", install instructions (iOS
   needs Share → Add to Home Screen), log out.

**PWA:** `manifest.webmanifest` (name "Abdo", Arabic short name "عبده", icons, `display: standalone`,
theme colors), service worker: precache the app shell, network-only for `/api`, `push` +
`notificationclick` handlers. Fonts: IBM Plex Sans + IBM Plex Sans Arabic (self-hosted, subset).

**Dev:** `npm run dev` in `web/` with Vite proxying `/api` → `localhost:8000`.

---

## 7. Push notifications (infrastructure only)

- `pywebpush` with VAPID. New env vars: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`
  (`mailto:`). Generate once with a small `scripts/gen_vapid.py`.
- Table:
```sql
CREATE TABLE push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
- `app/push.py`: `send_to_member(member_id, title, body, url)`. Runs `pywebpush` off the event loop
  and deletes subscriptions that return 404/410 (expired). Phase 7 calls this; Phase 6 only uses it for the test button.
- iOS: push works only for the installed (home-screen) app, iOS 16.4+. The Settings screen says so.

---

## 8. Build and deploy

- **`Dockerfile`** (multi-stage):
  1. `node:24-slim`: `npm ci && npm run build` in `web/` → `web/dist`.
  2. `python:3.13-slim` (matches prod): `apt-get install ffmpeg`, `pip install -r requirements.txt`,
     copy `app/`, `scripts/`, and the built `web/dist`. `CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}`.
- Remove `nixpacks.toml` and `Procfile` once the Docker build is verified (Railway uses the
  Dockerfile automatically when present). Check Railway → Settings for a custom start command that would override `CMD`.
- `.dockerignore`: `.venv`, `node_modules`, `local/`, `.env*`, `seed.sql`, `.git`, docs.
- New pinned deps: `pywebpush`, `python-multipart` (uploads). Frontend deps pinned via `package-lock.json`.

---

## 9. Tests

Backend (`tests/smoke.py`, offline):
- Auth: request-code always `ok` (existing, unknown, and no-Telegram members alike); a code is DM'd only
  to a real member; wrong code ×5 kills it; expired code rejected; success sets an HttpOnly cookie;
  `/api/*` without a session → 401; logout invalidates.
- Thread unification: Telegram private chat → `dm:<id>`, group → `tg:<chat>`; web chat → `dm:<id>`;
  history endpoint returns both channels' rows.
- Chat + voice endpoints route through `handle_turn` with `channel="web"`; TTS failure → `audio: null`.
- Lists/packages endpoints mirror tool validation (bad day / unknown owner → 400 with a message).
- Calendar endpoint: degraded read → error state, not `[]`.
- Access-log filter redacts `/tg/<secret>`.
- SPA fallback: unknown non-`/api` path serves `index.html`; unknown `/api` path is a JSON 404.

Frontend: `tsc --noEmit` + `vite build` must pass; Vitest for the API client (error → typed
result) and the code-input component.

---

## 10. Checklist

- [x] §1 log redaction (secret rotation: new value generated, applied at deploy)
- [x] Schema additions (`login_codes`, `web_sessions`, `push_subscriptions`) as a migration file
      `migrations/phase6_webapp.sql` (the live DB already has the Phase 5 schema). `schema.sql` updated too.
- [x] Thread unification (+ one-time UPDATE for Zain's test DM)
- [x] `channels/web.py`: auth, chat, voice, lists, packages, calendar, push
- [x] `tts.synthesize_mp3`, `push.py`, `scripts/gen_vapid.py`
- [x] `web/`: React app, PWA manifest + service worker, design system, 5 screens
- [ ] Dockerfile + `.dockerignore` written; Railway build verified; Nixpacks files removed (after first green Docker deploy)
- [x] Tests green (backend 227 checks; frontend typecheck + 10 Vitest + build; SW verified in real Chrome) (backend smoke + frontend typecheck/build/Vitest)
- [ ] Railway env: `SESSION_PEPPER`, `VAPID_*`; migration applied; deploy
- [ ] Live checks: install on iPhone + Android, log in, text + voice chat, history shared with
      Telegram, lists/packages edits visible to Abdo, test push arrives on an installed iPhone
- [x] CLAUDE.md + ROADMAP + README updated
