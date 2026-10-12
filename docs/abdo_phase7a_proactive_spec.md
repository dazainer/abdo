# Abdo — Phase 7a Build Spec: Proactive Abdo (scheduler, reminders, briefing, prayers, status)

Until now Abdo only answers. Phase 7a lets him speak first: reminders and timers you set by
talking to him, a morning briefing, prayer-time notifications, and roommate status
("in a midterm till 3"). Phase 7b (expenses, timetables, deadlines, groceries+, polls, buses)
builds on the scheduler added here.

**Exit criteria:** "remind me in 35 minutes about the laundry" (text or voice, Telegram or app)
arrives on time; each roommate gets an 8:00 briefing; prayer notifications arrive at the right
times (Fajr silent); statuses show in the app and Abdo uses them; a redeploy or restart never
loses or duplicates a scheduled message.

---

## 0. Decisions (2026-10-12)

| Topic | Decision |
|---|---|
| Delivery | **Push if the member has any push subscription, else Telegram**. Never both. One `notify.deliver()` used by everything proactive. |
| Briefing | **On by default at 08:00** local; each member can turn it off or change the time (Settings). Built from a template by code: no Claude call. |
| Prayers | **All five notify; Fajr is silent** (push `silent: true`, Telegram `disable_notification`). Per-member on/off (default on). ISNA method, standard (Shafi'i) Asr, both configurable via env. |
| Scheduling | **A background loop inside the app**, ticking every 30 s, with all state in Postgres, so restarts lose nothing. Idempotency in the database means two overlapping instances (Railway's zero-downtime deploys briefly run old + new) can never double-send. No scheduling library. |
| Recurrence | One-shot reminders only in 7a. "Every Monday" comes with 7b's timetables. |

---

## 1. Data (migration `migrations/phase7a_proactive.sql`, mirrored in `schema.sql`)

```sql
-- reminders already exists (member_id, text, due_at, recurring, done, created_at).
ALTER TABLE reminders ADD COLUMN created_by INTEGER REFERENCES members(id);
ALTER TABLE reminders ADD COLUMN sent_at TIMESTAMPTZ;
CREATE INDEX idx_reminders_due ON reminders (due_at) WHERE NOT done;

-- "What's everyone up to": one current status per member, optionally expiring.
CREATE TABLE member_status (
    member_id   INTEGER PRIMARY KEY REFERENCES members(id),
    text        TEXT NOT NULL,              -- "in a midterm", "away in Toronto"
    until_at    TIMESTAMPTZ,                -- NULL = until cleared
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotency for daily sends: a (member, kind, local day) row is claimed before sending,
-- so the briefing and each prayer go out exactly once per day per member.
CREATE TABLE sent_log (
    member_id   INTEGER NOT NULL REFERENCES members(id),
    kind        TEXT NOT NULL,              -- 'briefing' | 'prayer:fajr' | ... | 'prayer:isha'
    local_date  DATE NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, kind, local_date)
);
```

Member preferences live in the existing `members.prefs` JSONB (no migration):
`{"briefing": true, "briefing_time": "08:00", "prayers": true}`. A missing key means the default.

---

## 2. Delivery (`app/notify.py`)

`async def deliver(member, title, body, url="/", silent=False) -> str` returns `"push"`,
`"telegram"`, or `"none"`.
- If `push.send_to_member(...)` delivered to ≥ 1 device, done.
- Else, if the member has a Telegram id: `telegram.send_message(..., disable_notification=silent)`
  (add the optional parameter). Text = `title` + newline + `body`.
- Else log and return `"none"`.
- Push payload gains `silent`; the service worker passes it to `showNotification({ silent })`.

---

## 3. Scheduler (`app/scheduler.py`)

`run()` loops forever (every 30 s; `SCHEDULER_ENABLED=false` disables it for tests/demo):
1. **Reminders:** fetch due rows (`NOT done AND due_at <= now()`), and for each **claim** it with
   `UPDATE reminders SET done = true, sent_at = now() WHERE id = $1 AND NOT done RETURNING ...`.
   Only the claimer delivers. A reminder more than 6 h overdue (the app was down) still goes out,
   prefixed "(late)".
2. **Briefings:** for each member with the briefing on, when local time ≥ their `briefing_time`
   and before 12:00, claim `sent_log(member, 'briefing', today)` with
   `INSERT ... ON CONFLICT DO NOTHING RETURNING`, and deliver if claimed. (Missed by noon = skipped, not sent stale.)
3. **Prayers:** today's times (cached per local date). For each prayer whose time has passed by
   less than 20 minutes, claim `sent_log(member, 'prayer:<name>', today)` for each member with
   prayers on, then deliver (Fajr silent). Outside the 20-minute window it's skipped: a restart
   never sends an hour-old adhan.
- Each step is wrapped in try/except, so one failure never stops the loop. It starts in the app
  lifespan (`asyncio.create_task`) and is cancelled on shutdown.

Delivery for a claimed item happens after the claim commits. If delivery then fails, it's logged
and not retried (no duplicate risk). A Telegram fallback failure is logged loudly.

---

## 4. Prayer times (`app/prayer.py`)

- `adhanpy` (pure-Python port of the Adhan library; pinned). `CalculationMethod` from env
  `PRAYER_METHOD` (default `NORTH_AMERICA` = ISNA, Fajr/Isha 15°), madhab from `PRAYER_ASR`
  (`SHAFI` default, `HANAFI` option), at `HOME_LAT/HOME_LNG`.
- **Always pass the local calendar date** (adhanpy derives the day from a UTC timestamp; in
  Toronto evenings that would be tomorrow's date). Convert results to local time.
- `times_for(date) -> {fajr, sunrise, dhuhr, asr, maghrib, isha}` (aware local datetimes).
- Tool `get_prayer_times(day?)`: "امتى العصر؟" → code-computed times, day resolved by `resolve_date`.
- Copy: notification title is the prayer's name (`العصر` / Asr), body "Asr time in Waterloo: 3:52 p.m."
  (the spoken clips arrive in Phase 8).

---

## 5. Briefing (`app/briefing.py`)

Template, plain text, short, bilingual like Abdo. Lines appear only when there's something to say:
```
صباح الخير يا Zain ☀️
Weather: 3°, feels like -1, light snow. High 5°, 60% chance of precipitation.
Today: Suite dinner at 19:00
Waiting for you at the building: desk lamp
Groceries: 3 items on the list
Prayers: Dhuhr 1:12 p.m., Asr 3:52, Maghrib 6:30, Isha 7:51
```
- **Weather:** Open-Meteo forecast API (free, no key): current temperature, apparent temperature,
  weather code; daily max/min and precipitation probability, `timezone=<TIMEZONE>`. WMO code →
  short English phrase in code. Cached 30 min; on any failure the line is omitted (never a fake forecast).
- **Calendar:** today's events via the same read path as the tools. A degraded read →
  "Couldn't check the calendar." (never "nothing today").
- Statuses of the *other* roommates are included if set ("Mo: away till Sunday").
- Tool `get_briefing()` so "what's my day look like?" returns the same text on demand.

---

## 6. Reminders and status as tools (`tools.py`, `db.py`)

- `set_reminder(text, for?, in_minutes?, day?, time?)`: `in_minutes` for timers ("in 35
  minutes"), or `day` + `time` (HH:MM) resolved **in code** (`resolve_date` + `_parse_time`,
  local tz). Exactly one of the two forms; past times rejected with an honest error. `for` =
  a roommate's name (default: the speaker). Returns the resolved local time so Abdo confirms it.
- `list_reminders(for?)`: upcoming, with ids. `cancel_reminder(id)`: real ids only.
- Delivery text: "⏰ {text}" (own reminder) or "⏰ From Zain: {text}" (someone else's).
- `set_status(text, until_day?, until_time?)`, `clear_status()`, statuses for everyone also
  appear in the **dynamic** context block (who's away/busy right now), so Abdo can say "Mo's in
  a midterm till 3" without a tool call. Expired statuses are ignored and cleaned lazily.
- Persona: short additions for reminders (confirm the resolved time once, like the calendar),
  status, prayer times. The static persona changes, so the cache re-warms once.

---

## 7. Web app

- **Today tab** (replaces Calendar; route `/today`, old `/calendar` redirects): roommates' statuses
  (with "Set my status" / "Clear"), today's prayer times (next one highlighted), my upcoming
  reminders (cancel), then the 7-day calendar as now.
- **Settings:** Morning briefing toggle + time picker, Prayer notifications toggle.
- API: `GET /api/today` (statuses, prayer times, my reminders), `PUT /api/status`,
  `DELETE /api/status`, `DELETE /api/reminders/{id}`, `GET/PATCH /api/me/prefs`. Mutations
  validate like the tools (same functions).

---

## 8. Tests

- Scheduler with an injectable clock + fake DB: a due reminder sends once; two concurrent ticks
  send once; a 7-h-late reminder says "(late)"; the briefing sends once after 08:00, not before,
  not after noon, and not when off; prayers send once inside the window, never outside; Fajr is silent.
- `deliver`: push wins; zero push deliveries → Telegram fallback with `disable_notification`
  for silent; no channels → "none".
- Prayer times: ordering Fajr < sunrise < Dhuhr < Asr < Maghrib < Isha; Dhuhr within a few
  minutes of solar noon for the home coordinates; an evening call (after 20:00 local) returns
  *today's* times; the DST day (2026-11-01) works; Hanafi Asr is later than Shafi'i.
- Reminder tool: in_minutes vs day+time; past time rejected; unknown roommate rejected; both
  forms at once rejected; cancel with a fake id is an honest error.
- Briefing: lines omitted when empty; weather failure omits the line; degraded calendar says so.
- Status: expiry respected; appears in the dynamic context, never in `STATIC_PERSONA`.
- Web: /api/today shape, prefs validation (HH:MM), status set/clear, reminder cancel auth (only your own).

---

## 9. Checklist

- [x] Migration + `schema.sql`
- [x] `notify.py` (+ `telegram.send_message(disable_notification=)`, SW `silent`)
- [x] `prayer.py` (adhanpy pinned) + `get_prayer_times` tool
- [x] `scheduler.py` + lifespan wiring + `SCHEDULER_ENABLED`
- [x] `briefing.py` (Open-Meteo) + `get_briefing` tool
- [x] Reminder + status tools, dynamic-context statuses, persona lines
- [x] Web: Today tab, Settings prefs, API routes
- [x] Tests green (backend 296 checks; frontend typecheck + Vitest + build; UI checked in the demo server)
- [ ] Deploy: migration applied; live checks: a 2-minute timer arrives, tomorrow's 08:00
      briefing arrives, one prayer notification arrives and its time matches the Athan app
- [x] CLAUDE.md + ROADMAP updated
