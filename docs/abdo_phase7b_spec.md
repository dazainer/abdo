# Abdo — Phase 7b Build Spec: Money, Schedules, Food, Getting Around

Shipped in four independently deployable parts. Each part gets a migration in `migrations/`,
smoke tests, and a live check before the next starts.

| Part | Contents | Status |
|---|---|---|
| **7b.1** | Expenses + settle-up, travel mode | deployed 2026-10-12 |
| **7b.2** | Class timetables (paste from Quest), exams | deployed 2026-10-12; import moved to a deterministic parser (`app/quest.py`) on 2026-10-13 |
| **7b.3** | Groceries+ (fridge + expiry, "going shopping"), polls | built 2026-10-13 (434 backend checks); awaiting deploy |
| **7b.4** | Buses (ION + GRT), plus a web-app declutter pass | built 2026-10-13; awaiting deploy |

## Decisions (2026-10-12)

| Topic | Decision |
|---|---|
| Expense split | **Everyone by default**, people can be excluded or a split narrowed ("split with Mo"). Equal shares, exact to the cent. Fewest-transfers settle-up. Abdo confirms an expense once before recording it (money, like calendar writes). |
| Timetables | **Paste from Quest's list view** → parsed deterministically in code (`app/quest.py`; revised 2026-10-13 from a Claude extraction) → TST slots proposed as exams → the person confirms → saved. |
| Exams | **Quizzes, midterms, finals only**, always visible to the suite (the point is informing the others). No assignments, no private flag. |
| Buses | ION + GRT. Foundation now, stops/routes later. Known trips: Walmart on Bridgeport (groceries); ION from Laurier-Waterloo Park station. |
| Travel mode | Kept simple: a status can carry a city; that person's prayers, Today, and briefing weather follow it until the status ends. No GPS. |

---

## 7b.1 — Expenses + travel mode

### Data (`migrations/phase7b1_money_travel.sql`, mirrored in `schema.sql`)

```sql
CREATE TABLE expenses (
    id            BIGSERIAL PRIMARY KEY,
    description   TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_by       INTEGER NOT NULL REFERENCES members(id),
    created_by    INTEGER REFERENCES members(id),
    spent_on      DATE NOT NULL,
    deleted       BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE expense_shares (
    expense_id    BIGINT NOT NULL REFERENCES expenses(id),
    member_id     INTEGER NOT NULL REFERENCES members(id),
    share_cents   INTEGER NOT NULL CHECK (share_cents >= 0),
    PRIMARY KEY (expense_id, member_id)
);
-- "I paid Mo back $20": money moving between roommates outside an expense.
CREATE TABLE settlements (
    id            BIGSERIAL PRIMARY KEY,
    from_member   INTEGER NOT NULL REFERENCES members(id),
    to_member     INTEGER NOT NULL REFERENCES members(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    created_by    INTEGER REFERENCES members(id),
    settled_on    DATE NOT NULL,
    deleted       BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_member <> to_member)
);

-- Travel mode: a status can carry a place.
ALTER TABLE member_status ADD COLUMN place TEXT;               -- "Montreal, Canada"
ALTER TABLE member_status ADD COLUMN place_lat DOUBLE PRECISION;
ALTER TABLE member_status ADD COLUMN place_lng DOUBLE PRECISION;
ALTER TABLE member_status ADD COLUMN place_tz TEXT;            -- IANA, from the geocoder
```

### Money rules (`app/money.py`, pure functions, heavily tested)
- **Amounts are integer cents** end to end. `parse_amount("42.5" | "$42.50" | "42")` uses
  `Decimal`, rejects ≤ 0, more than 2 decimals, or > $10,000 (a typo guard).
- **Equal split, exact:** `split(amount_cents, member_ids)` gives each person
  `amount // n`, and the first `amount % n` people (by id) get 1 extra cent. The shares always sum
  to the amount.
- **Balances:** `net[m] = paid(m) − owed_shares(m) + settlements_sent(m) − settlements_received(m)`.
  Positive means the suite owes them. Deleted rows are ignored. The nets sum to 0.
- **Settle-up:** greedy fewest transfers (largest debtor pays largest creditor, repeat). At
  most n−1 transfers.
- **Delete** is soft and only by whoever paid or recorded it (an honest error otherwise).

### Tools
- `add_expense(description, amount, paid_by?, split_with?, exclude?, day?)`:
  - `paid_by` defaults to the speaker.
  - `split_with` (names) narrows the split from everyone; `exclude` removes people.
  - `day` is resolved in code (default today). Unknown names give an honest error.
  - Returns the per-person shares, so Abdo can read them back.
- `get_balances()`: each person's net plus the suggested transfers ("Mo → Zain $12.50").
- `record_payment(to, amount, from?)`: a settlement, `from` defaults to the speaker.
- `list_expenses(limit?)` (recent, with ids) and `delete_expense(id)`.
- **Persona:** before `add_expense` / `record_payment`, read back the resolved details ONCE
  ("$42 groceries, paid by you, split 4 ways, $10.50 each: أسجلها؟"), then act on "yes".
  Never compute shares or balances yourself; read them from the tools.

### Web: a **Money** tab (5th tab)
- **Top:** "You're owed $X" / "You owe $X" / "All square", in a large figure on an enamel plate.
- **Settle up:** the suggested transfers. On each one involving me, **Mark paid**
  records the settlement.
- **Add expense:** amount, description, paid by (default me), split among (four checkboxes,
  all ticked), live per-person preview.
- **Recent expenses:** description, amount, who paid, the split, Delete (if allowed).
- API: `GET /api/money` (balances, transfers, recent), `POST /api/expenses`,
  `DELETE /api/expenses/{id}`, `POST /api/settlements`, `DELETE /api/settlements/{id}`.
- **Briefing:** one line / card row if your |net| ≥ $1 ("You owe Mo $12.50").

### Travel mode
- `set_status(text, place?, until_day?, until_time?)`: `place` is geocoded with Open-Meteo
  (`geocoding-api.open-meteo.com/v1/search`, first result). The resolved "City, Country" is echoed
  so Abdo can confirm it; an unknown place gives an honest error. The web status form gets an
  optional "Where (city)" field.
- `app/location.py`: `location_for(member_id) -> (lat, lng, tz, label)`: the place from the
  member's **unexpired** status, else the suite's home. Used by:
  - **prayer notifications:** the scheduler groups members by location, computes times
    per location in that location's timezone, and claims `sent_log` by that location's local date.
  - **Today prayer times** and the **briefing** (weather, prayers, "in Montreal" note),
    for that member.
- `prayer.times_for(day, lat=None, lng=None, tz=None)`; weather cache keyed by coordinates.
- Abdo's context shows "Zain: away (in Montreal, Canada) until Sun 23:59".
- Briefing time is read in the place's timezone.

### Tests
- Money: parse edge cases; split sums exactly (incl. 1¢ ÷ 3, $100 ÷ 3); balances sum to 0;
  settle-up transfers zero every balance with ≤ n−1 moves; payer outside the split; deleted
  rows ignored; delete permissions.
- Tools: default everyone, `split_with`, `exclude`, unknown name, bad amount, day resolution.
- Web: money endpoint shape, add/delete/settle, validation, only-your-own delete.
- Travel: geocode found/not found (fake), status with place feeds `location_for`;
  scheduler sends a traveller's prayers at the place's times (and the suite's at home times);
  the expired status falls back home; the briefing uses the place's weather.

---

## 7b.2 — Timetables + exams (outline)
- `classes` (member, course, kind LEC/TUT/LAB, weekday, start, end, room, term_start, term_end)
  and `exams` (member, course, kind quiz/midterm/final, starts_at, ends_at?, room).
- **Import:** a paste box in the app → `POST /api/timetable/extract` → one Claude call with a
  strict JSON schema (structured output) → preview table → **Save** replaces that person's
  classes. Nothing is stored before the person confirms.
- Tools: `who_is_free(day?, from?, to?)` ("when are we all free tonight?" computed in code from
  classes + exams + statuses), `get_schedule(name?, day?)`, `add_exam(...)`, `list_exams()`.
- Briefing: today's classes; your exams with countdowns ("Midterm MATH 136 in 3 days"); others'
  exams today/tomorrow ("Kateb has a midterm tomorrow morning").

## 7b.3 — Groceries+ and polls (outline)
- **Fridge:** `pantry_items` (item, owner?, shared, expires_on). "We bought milk, expires Friday".
  The morning briefing lists what expires today or tomorrow, and expired items are offered to
  the shopping list.
- **"Going shopping":** "I'm going to Walmart" → push to the others: "Zain is going to Walmart.
  Add anything?" (one tap opens Lists).
- **Polls:** `polls` / `poll_votes`. Created by Abdo or the app, pushed to everyone, voted in
  the app (Telegram: reply with an option name). Results come in Abdo's answers and on Lists.

## 7b.4 — Buses (built 2026-10-13)
- **Source: the GraphQL API behind GRT's own departures site** (`stoptimes.grt.ca`,
  `GRT_API_URL`). Checked 2026-10-12: GRT's GTFS host (`webapps.regionofwaterloo.ca`) refuses
  connections, the only reachable GTFS copy (Mobility Database mirror) is last term's, and GRT's
  live tracking is down for everyone, so Google Maps and the Transit app show scheduled times
  too. The departures API serves the current schedule (holiday service included) and marks a
  departure live by itself once GRT's tracking returns. No GTFS download or storage.
- It isn't a documented open-data API: one small query per trip, a 60 s cache, only fetched
  when someone asks (the Today section is closed until tapped), and a failure is reported as
  "couldn't reach GRT", never guessed. If it changes, swap `transit._post`; the rest stays.
- **Trips** (`bus_trips`): board at any of `from_stops`, get off at any of `to_stops`,
  optionally only some `routes`. A departure counts only if its own trip reaches a `to_stop`
  after the boarding stop, which settles direction and gives the arrival time. No `to_stops` =
  plain departures (the stop by home). Real stop ids live in the gitignored
  `local/bus_trips.sql`, never in the repo.
- Tool `next_bus(trip?, route?)`; `GET /api/buses`; a closed "Buses" section on Today.
- Not built (Zain: low priority): "leave by" alerts. `set_reminder` already covers "remind me
  ten minutes before the 4:41".
