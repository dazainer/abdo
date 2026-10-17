# Abdo — Phase 8a Spec: the room device, proven on a Mac

Goal: Abdo in the common room, hands-free. Someone says the wake phrase, Abdo answers with a
short pre-recorded acknowledgement, listens, works out who's talking, and speaks the reply.
Prayer calls, timers and the morning briefing play aloud on their own. Everything runs on a
Mac first; hardware is bought only after a week of reliable use (8b, ≤ $100).

Status: **device v0 running end to end with a key as the wake word** (`room/device.py`,
`app/channels/room.py`); Zain's recordings done (2026-10-17); clip library and recorder built (`room/clips.json`, `scripts/make_clips.py`,
`scripts/record_session.py`). Decisions confirmed 2026-10-14 are folded in below.

## 1. How a room turn works

```
mic (16 kHz mono, always on)
 → wake word model ("Ba2olak ya Abdo")            local, CPU
 → play an acknowledgement clip ("أيوه يا حبيبي")  local file, instant
 → record until silence (VAD), max 15 s            local
 → speaker ID on the recording                      local: who is it, how sure
 → POST /api/room/turn {audio, speaker guess, confidence}   device token auth
     server: STT → handle_turn(thread="room", member=guess if sure, channel="room", voice=True)
             → TTS (cached by text) → reply audio
 → play the reply; listen 5 s for a follow-up without the wake word
```

- Speaker ID never authorizes anything sensitive. Below the confidence threshold, the turn is
  "someone in the room" and Abdo asks who it is if the answer depends on it ("whose package?").
- Mute: a toggle in the app and a spoken "اسكت يا عبده". While muted he ignores the normal wake
  phrase entirely and listens only for a **secret phrase, "افتح يا سمسم"**, which unmutes him
  with a witty line. It's a second small wake-word model, trained from the same session.
- **Wake phrase**: "بقولك يا عبده". "يا عبده" on its own should wake him too, **only if** the
  tests show it doesn't misfire much (a shorter phrase is easier to trigger by accident, from
  conversation or TV). The session records both, and the false-wake rate decides.
- Server → device channel (long-poll or SSE) for scheduled audio: prayer calls (Dhuhr to Isha
  aloud, never Fajr), timers, the briefing on "good morning", mute state.

## 2. Pre-recorded clips (the audio library)

Generated once in Abdo's ElevenLabs voice by a script (`scripts/make_clips.py`), stored as
files on the device, not in the repo. Any other synthesized line is cached by text hash, so a
repeat never costs credits. The whole library is about 550 characters of TTS (roughly 550
credits once, on multilingual v2).

**Wake acknowledgements** (played the instant the wake word fires; rotate at random, never the
same one twice in a row):

| The suite's list | Suggested additions |
|---|---|
| قول لي | نعم يا باشا |
| قول لي يا حبيبي | تحت أمرك |
| أيوه يا حبيبي | قول يا معلم |
| حبيب عبده | سامعك |
| اؤمرني | عيوني |
| أفندم | أيوه يا كبير |
| أيوان! | معاك |

All fourteen are in. **Personal acknowledgements** ("أيوه يا <name>", from the `members` table)
come later, and only if speaker ID turns out reliable on the wake phrase alone (about 1 s of
audio). Until then he greets generically and learns who's talking from the full request.

**While thinking** (only if the reply takes over ~2.5 s): "ثانية واحدة", "ثواني", "لحظة كده",
"لحظات معايا", "خليني أشوف", "بص يا سيدي".

**Errors**: "معلش مسمعتش، تاني؟" (didn't catch it), "النت فاصل شوية" (server unreachable),
"مش عارف مين بيتكلم" (when it matters who's asking).

**Prayer calls** (Dhuhr, Asr, Maghrib, Isha; Fajr is never aloud): the prayer's name, then who
he's calling, picked at random: "العصر يا شباب", "العصر يا رجالة", "العصر يا اخواتي",
"العصر يا نجوم" (MSA prayer names, like the app).

**Timers**: no voice line, just a short chime (generated locally, no credits). Reminders read
their own text through the TTS cache.

**Mute**: "حاضر، هسكت" when muted; the secret phrase gets a witty comeback ("الكنز اتفتح! رجعتلكم",
"مين نده؟ أنا هنا").

## 3. The recording session

One sitting, about 20 minutes per person plus 15 minutes of room noise. Everything is recorded
on the Mac that will run 8a, in the common room, because the model has to learn that mic in
that room. A person who isn't there (one roommate is away) records later; the models work
without them and get retrained when they do.

A guided recorder (`scripts/record_session.py`) does the bookkeeping: it shows one prompt at a
time, cuts each take automatically by voice activity, saves `data/<person>/<kind>/<n>.wav`
(16 kHz mono), and lets you redo (`r`) or skip (`s`). No file names to type, no editing after.

Per person:

1. **Wake phrase, 50 takes**, prompted in blocks of 10 so the takes vary:
   normal voice at the table; quiet; loud or calling from the kitchen; fast and slow; from
   across the room (~4 m); facing away; while someone else is talking.
   Then **"يا عبده" alone, 20 takes**, and the secret unmute phrase **"افتح يا سمسم", 30 takes**,
   varied the same way.
2. **Enrollment speech, ~90 s**: read a short mixed Arabic/English prompt list (the way the
   suite talks), then 30 s of free talk from across the room.
3. **Commands, 15–20**: real requests ("بقولك يا عبده، ضيف لبن للقائمة"). These test the
   whole chain and measure STT on the room mic.

Room negatives (no wake phrase anywhere):

- 5 min of normal conversation in Arabic and English.
- 5 min of TV or YouTube, 3 min of music, kitchen noise.
- **Near misses**, 10 takes each: "بقولك يا عمرو", "بقولك إيه", "بقولك يا عبد الله", "يا عبده"
  on its own (**decide**: should "يا عبده" alone wake him?).

Consent: everyone agrees before recording. Recordings stay on the Mac (gitignored), are used
only to train and test these models, and any person's files are deleted on request.

## 4. Processing

Everything runs offline on the Mac.

**Wake word.** openWakeWord (the plan of record) trains a small model on top of a frozen
speech-embedding model. Its standard training pipeline leans on synthetic English TTS
samples, so check the current docs first. The plan is to train mostly on the real takes:

- Positives: the 50 × 3–4 people takes, augmented (room reverb, the recorded noise mixed in at
  several loudness levels, small speed and pitch changes) into a few thousand examples; plus
  synthetic takes from an Arabic TTS voice for variety, if it pronounces the phrase well.
- Negatives: the room recordings, the near misses, and openWakeWord's general negative set.
- Hold out 10 takes per person and 3 minutes of each negative type for testing.
- Target: hears ≥ 90% of held-out takes up to 4 m; fewer than one false wake per 10 hours
  of the held-out room audio (scaled from what we have).
- Fallback if training the phrase that way is a dead end: a small classifier head on the
  same embeddings, trained directly on our clips.

**Speaker ID.** A pretrained speaker-embedding model (ECAPA-TDNN via SpeechBrain, or the
lighter Resemblyzer). Each person's voiceprint is the average embedding of their enrollment
segments. A turn is matched by cosine similarity; below a tuned threshold it's "unknown".
Test on held-out commands: report how often it picks the right person, and how often it
names someone confidently but wrongly (that number has to be near zero).

**STT on the room mic.** Run the held-out commands through ElevenLabs Scribe (what the server
uses now) and read the transcripts; if the room mic hurts accuracy, that decides the 8b mic.

## 5. Build order

1. **Clip library**: `scripts/make_clips.py` + the phrase list above (cheap, can be done now).
2. **Recorder**: `scripts/record_session.py`; dry run with one person.
3. **Recording session** with everyone available.
4. **Wake word**: train, test against the targets, tune the threshold.
5. **Speaker ID**: enroll, test, set the threshold.
6. **Device client** (Python on the Mac) + server: `/api/room/turn`, the device channel, device
   token, mute, the `room` thread.
7. **A week in the common room.** Exit: reliable enough that nobody reaches for their phone.
   Then 8b: the hardware research session, using what this phase measured (mic, CPU load).

## 6. Costs

- Clips: a few hundred TTS characters once; later lines are cached.
- Room turns: the same STT + Haiku + TTS per turn as a voice note today. ElevenLabs is the
  main running cost, so acknowledgements and fixed lines are always clips, never synthesis.
- Hardware: none in 8a.
