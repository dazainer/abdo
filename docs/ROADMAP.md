# Abdo — Roadmap

Abdo moves from the Khalil family in New Cairo to a four-person student suite in
Waterloo, Ontario (Zain, Mo, Kateb, Nour), all on study terms (2A). The Egypt edition is
frozen at git tag `egypt-final` (see `docs/egypt/`). Real names, ids, and the residence are
kept out of this public repo (see `local/`, gitignored).

Goals, in priority order:
1. Useful **every day** for four students sharing a suite, not a novelty.
2. **Minimal running cost** without losing what makes Abdo *Abdo* (Egyptian personality, voice).
3. Reachable from **Telegram, his own web app, and an always-on room device**, all backed by one brain.

This file is the plan of record. Each phase gets a detailed spec in `docs/` before it's built
(per CLAUDE.md: build one phase at a time). Only Phase 5 is fully specced so far.

---

## Decisions log

| Topic | Decision |
|---|---|
| Language | Mix of Egyptian Arabic and English (code-switching is the norm, Franco/Arabizi in text). |
| Brain model | `claude-haiku-5-5` (≈10× cheaper than Haiku 4.5, better instruction following). Effort `low` for chat. Prompt caching on. |
| Voice provider | Stays **ElevenLabs** (Scribe STT + Flash TTS). Nothing cheaper does Egyptian Arabic well. Cost is controlled by *calling it less*: local wake word, pre-recorded clips, TTS cache. |
| Own STT/TTS models | Side project, **not** on the critical path. `stt.py`/`tts.py` stay swappable so a fine-tuned model can plug in later. |
| Telegram | Kept as a channel. **New bot** (the Egypt bot stays with the archived version). |
| Front end | A **PWA** (installable website) served by the same FastAPI app: no app stores, no fees. |
| Always-on | Room device with a **local** wake word "Ba2olak ya Abdo" → instant pre-recorded "Oly" → only then the paid STT → brain → TTS chain. Digital mute stops the mic on the device itself. |
| Speaker recognition | Local speaker embeddings on the room device (free). Low confidence → Abdo asks who's talking. Never used to authorize sensitive actions. |
| Prayer times | Computed locally (no API). Method **ISNA**, Asr **Standard (Shafi'i)**, both configurable. **Fajr never aloud**; Dhuhr/Asr/Maghrib/Isha play a pre-recorded clip on the room device (silent when muted). |
| Hardware | Budget **≤ $100 total (~$25 each)**, bought **only after** the room-device pipeline is proven on Zain's Mac (Phase 8a). Final pick comes from a separate research session using the requirements below. |
| Dropped features | Dog feeding (Egypt), chore rotation, study-group nudges, co-op tracker. |
| Repo visibility | **Public** (resume project). Code is household-agnostic: people in the DB, place/calendar in env vars, private notes in gitignored `local/`. Deploy `main`; secrets only ever live in Railway env vars. |

---

## Phases

### Phase 5: Move Abdo to the suite + brain upgrade → `docs/abdo_phase5_rehome_spec.md`
Brain separated from Telegram (`handle_turn`), speaker/channel tracking in the conversation log,
new household model, fresh database, Haiku 5.5 + prompt caching, persona for the suite,
`America/Toronto`, dogs removed, orders → packages, new Telegram bot, pinned dependencies,
public-repo hygiene + a real README.
**Exit:** all four roommates using Abdo on Telegram day to day.

### Phase 6: Abdo's web app (PWA), outline
- Served from FastAPI (static files + JSON API). Installable to the home screen on iOS/Android.
- **Login:** Telegram Login Widget (no passwords; it reuses the identity everyone already has).
  Fallback: per-roommate magic link.
- **Pages:** chat (text + push-to-talk via the browser mic → same STT path), shared lists
  (groceries, packages), a household dashboard view (later the room device's screen).
- **Web push** notifications (VAPID, free). On iOS they require the PWA to be installed to the home screen.
- **Mute control** for the room device (wired up in Phase 8).
- Open: frontend stack (plain JS/htmx vs a small framework). Decide when speccing.

### Phase 7: Proactive Abdo + student-life features, outline
Split in two so each half ships on its own.

**7a: Scheduler, briefing, prayers, timers**
- In-process scheduler (rebuilt from the DB at startup; Railway runs one instance).
  This finally wires up the `reminders` table.
- **Daily briefing** per roommate at their chosen time: today's classes, shared calendar,
  deadlines, packages, groceries, balances, weather (Open-Meteo: free, no key, not web search).
  Delivered by push/Telegram; spoken on the room device on "good morning" (Phase 8).
  Template-first; one Haiku call max per person per day.
- **Prayer times:** daily midnight job computes times locally and schedules notifications.
  Aloud playback arrives with the room device (Phase 8).
- **Timers & reminders:** "laundry's done in 35", "remind Kateb about the charger at 9".
- **Status:** "in a midterm, keep it quiet", "away till Sunday". Shown on the dashboard, used in answers.

**7b: Money, schedules, food, getting around**
- **Expenses / settle-up:** "I paid $42 for groceries, split 4 ways"; running balances;
  minimal "who pays whom" settle-up. Integer cents, CAD.
- **Timetables:** each roommate's class schedule (import method TBD: ICS import vs parsing a
  pasted Quest schedule). Powers "when are we all free tonight?" / "who's home at 3?".
- **Deadlines & midterms:** per-person dates with countdowns in the briefing.
- **Groceries+:** "we're out of X", who's going shopping, expiry tracking for perishables.
- **Polls:** "where are we eating?"
- **Buses:** GRT/ION next departures (GRT publishes GTFS + GTFS-realtime; verify access when speccing).

### Phase 8: The room device, outline
**8a: Prove it on Zain's Mac (no purchase)**
- Device client (Python): mic → **openWakeWord** custom "Ba2olak ya Abdo" → play "Oly" clip →
  VAD-bounded recording → **speaker ID** → `POST` audio + speaker guess to the server →
  play the reply. Short follow-up window (~5 s) without the wake word.
- Recording session with all four: ~50 wake-phrase takes each + 30–60 s of speech each for
  speaker enrollment + negative audio (TV, chatter, music).
- **Audio library:** pre-generate fixed clips once in Abdo's ElevenLabs voice ("Oly", prayer calls,
  "timer's done", acknowledgements, errors). **TTS cache:** any synthesized line is cached
  by text hash, so repeats are free.
- Server → device push channel for scheduled audio (prayers, timers, briefing) + mute state.
- **Exit:** a week of reliable use from the Mac in the common room.

**8b: Buy and move to the dedicated device** (only after 8a passes). Same client, new box.

### Side project: Egyptian Arabic STT/TTS models
Fine-tune open Whisper on Egyptian Arabic + Arabic/English code-switching (public data such as
MGB-3 plus the suite's own consented recordings); later, an open TTS voice fine-tuned on a
Cairene speaker. Swapped in behind `stt.py`/`tts.py` only if it beats ElevenLabs on your audio.

---

## Room device: requirements (input for the hardware research session)

Must:
- Run 24/7, quietly, on residence power; outbound HTTPS only (works behind the residence network,
  no inbound ports or port-forwarding).
- Python 3.11+ on Linux (or macOS); runs openWakeWord + VAD + speaker-ID inference on CPU
  in real time. Light load: a Raspberry Pi 5 or any x86 mini PC from the last ~8 years.
- **Far-field mic** that picks up speech across a common room, with **echo cancellation**
  (so Abdo doesn't hear himself). A USB conference speakerphone covers mic + speaker in one.
- Audible speaker for prayer clips, timers, replies.
- Auto-start on boot; survives power blips (no SD-card corruption, or easy recovery).
Nice to have:
- Enough CPU/RAM to run a small local Whisper model later (side project). Favors x86 mini PC over Pi.
- A screen for the dashboard (can be a cheap used tablet added later, or skipped).
Budget: ≤ $100 CAD total, used market (Facebook Marketplace, Kijiji) is fine.

---

## Running-cost model (estimates, to be measured after Phase 5)

| Item | Estimate | Notes |
|---|---|---|
| Railway (Hobby) | ~$5/mo | App + Postgres. Verify current plan pricing. |
| Claude Haiku 5.5 | ~$1/mo or less | ~50 messages/day across the suite; most of the prompt is cached. |
| Web search | $10 per 1,000 searches | Kept rare by prompt; weather goes to Open-Meteo instead. |
| Cohere embeddings | cents | Only on remember/recall. |
| ElevenLabs | current plan | The real bill. Wake word, clips, and TTS cache keep it from growing with always-on. |
| Hardware | ≤ $100 one-time | Phase 8b only. |

---

## Open questions (resolve in the relevant phase spec)
- Geofence radius for "home" around the building (Phase 5; coordinates are in `local/`).
- Keep live-location sharing (`where_is`) for the suite, or drop it? (Phase 5 keeps it; cheap.)
- Should household facts support **personal** facts (only visible to their owner)? (Phase 7.)
- Timetable import method (Phase 7b).
