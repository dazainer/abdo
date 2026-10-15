"""Guided recorder for the Phase 8a recording session (docs/abdo_phase8a_spec.md §3).

Shows one prompt at a time, records from the Mac's mic, cuts each take automatically, and files
it as 16 kHz mono WAV under room/data/<person>/<kind>/. Re-running resumes: numbering continues.

    pip install -r room/requirements.txt          # once (sounddevice, numpy)
    .venv/bin/python scripts/record_session.py --list-devices
    .venv/bin/python scripts/record_session.py --person zain           # everyone, one by one
    .venv/bin/python scripts/record_session.py --person room           # background + near misses
    .venv/bin/python scripts/record_session.py --person zain --only secret   # one block
    .venv/bin/python scripts/record_session.py --tidy --person zain     # trim clicks, drop empty takes
    .venv/bin/python scripts/record_session.py --selftest               # no mic needed

Hands-free: press Enter once to start a block. After a countdown (long enough to walk across
the room for the "far" block) a beep means it's listening. Say the phrase, pause, and when it
beeps the take is saved: say it again. Nothing to press between takes.
While a block runs, type a letter and Enter:  r = delete the last take,  p (or just Enter) =
pause,  s = skip the rest of the block,  q = quit. Key clicks, coughs and bumps shorter than a
word are ignored. Recordings stay on this Mac (room/data/ is gitignored).
"""
import argparse
import json
import queue
import shutil
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "room" / "data"

RATE = 16000
BLOCK = 480                 # 30 ms
PRE_ROLL = 10               # blocks (300 ms) kept before speech starts
START_BLOCKS = 5            # 150 ms of sound before a take starts: a key click is shorter
MIN_SPEECH_BLOCKS = 10      # a take needs 300 ms of actual sound, or it's a click/noise
NOISE_FACTOR = 6            # speech = 6x the room's noise level…
MIN_THRESHOLD = 80.0        # …but never below this (quiet speech peaks around 300-500)
END_SILENCE = 20            # 600 ms of quiet ends a take
MAX_TAKE_S = 8
LISTEN_SLICE_S = 2          # how often a waiting take checks for typed commands
LEAD_S = 3                  # countdown before a block starts
FAR_LEAD_S = 8              # …long enough to walk across the room

WAKE = "بقولك يا عبده"

# (kind, label, instruction, takes) — short utterances, one per Enter.
PERSON_TAKES = [
    ("wake", "normal", f"Say: {WAKE}. Normal voice, sitting at the table.", 10),
    ("wake", "quiet", f"Say: {WAKE}. Quietly, like someone's sleeping.", 10),
    ("wake", "loud", f"Say: {WAKE}. Loud, like calling from the kitchen.", 10),
    ("wake", "pace", f"Say: {WAKE}. Alternate fast and slow.", 10),
    ("wake", "far", f"Say: {WAKE}. From across the room (~4 m), some facing away.", 10),
    ("ya_abdo", "mixed", "Say just: يا عبده. Vary loudness and distance.", 20),
    ("secret", "mixed", "Say: افتح يا سمسم. Vary loudness and distance.", 30),
]

ENROLL_LINES = [
    "صباح الفل، النهاردة عندي lecture الساعة عشرة.",
    "حد شاف الشاحن بتاعي؟ I left it on the table.",
    "أنا رايح Walmart بعد العصر، حد عايز حاجة؟",
    "The midterm is next Tuesday, so I'll be studying all weekend.",
    "عبده، ضيف عيش ولبن وبيض على القائمة.",
    "مين دفع الإيجار الشهر ده؟",
    "I'm heading to campus, back around six.",
    "الجو برد قوي النهاردة، خد جاكيت.",
    "Can you remind me to call home tonight?",
    "خلاص يا جماعة، نتعشى شاورما؟",
]

COMMANDS = [
    f"{WAKE}، ضيف لبن للقائمة",
    f"{WAKE}، فكرني بعد عشر دقايق أقلب الغسيل",
    f"{WAKE}، العصر الساعة كام؟",
    f"{WAKE}، مين في البيت؟",
    f"{WAKE}، what's on the calendar this week?",
    f"{WAKE}، الباص الجاي امتى؟",
    f"{WAKE}، مين عليه فلوس؟",
    f"{WAKE}، I paid twenty dollars for pizza, split it with everyone",
    f"{WAKE}، الجو عامل ايه بكرة؟",
    f"{WAKE}، احنا فاضيين امتى النهاردة؟",
    f"{WAKE}، good morning",
    f"{WAKE}، في حاجة في التلاجة هتبوظ؟",
    f"{WAKE}، اسكت",
    "افتح يا سمسم",
    f"{WAKE}، شكراً",
]

NEAR_MISSES = ["بقولك يا عمرو", "بقولك إيه", "بقولك يا عبد الله", "يا عم بقولك", "افتح الشباك"]

# (kind, label, instruction, seconds) — long continuous recordings for the "room".
ROOM_TIMED = [
    ("neg", "conversation", "Talk normally in Arabic and English. Nobody says عبده or سمسم.", 300),
    ("neg", "media", "Play TV or YouTube at normal volume. Talk over it now and then.", 300),
    ("neg", "music", "Play music, Arabic and English.", 180),
    ("neg", "kitchen", "Kitchen and room noise: dishes, doors, footsteps, the microwave.", 120),
]


# ---- the segmenter (pure: shared by the live recorder, --tidy and the self-test) ----------

def rms(block) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) if len(block) else 0.0


def speech_blocks(audio, threshold: float) -> int:
    """How many 30 ms blocks of the clip are above the speech threshold."""
    return sum(1 for i in range(0, len(audio) - BLOCK + 1, BLOCK) if rms(audio[i:i + BLOCK]) > threshold)


def cut_take(blocks, threshold: float, wait_s: float = 10):
    """Take blocks of int16 audio until one utterance has been heard and has ended.
    A take starts only after START_BLOCKS of continuous sound (a key click never gets there);
    it keeps 300 ms before that and trims the trailing silence to 300 ms. Returns None if
    nothing started within wait_s."""
    import numpy as np
    pre, take = [], []
    loud_run = quiet_run = 0
    started = False
    max_blocks = int(MAX_TAKE_S * RATE / BLOCK)
    wait_blocks = int(wait_s * RATE / BLOCK)
    for i, b in enumerate(blocks):
        loud = rms(b) > threshold
        if not started:
            pre.append(b)
            pre = pre[-(PRE_ROLL + START_BLOCKS):]
            loud_run = loud_run + 1 if loud else 0
            if loud_run >= START_BLOCKS:
                started, take = True, list(pre)
            elif i >= wait_blocks:
                return None
            continue
        take.append(b)
        quiet_run = 0 if loud else quiet_run + 1
        if quiet_run >= END_SILENCE or len(take) >= max_blocks:
            break
    if not started:
        return None
    keep = len(take) - max(0, quiet_run - PRE_ROLL)
    return np.concatenate(take[:keep])


def tidy_clip(audio, threshold: float | None = None):
    """An already-recorded take with any click before the speech cut off and the edges
    trimmed to 300 ms; None if there's no real speech in it."""
    import numpy as np
    n = len(audio) // BLOCK
    if n == 0:
        return None
    levels = [rms(audio[k * BLOCK:(k + 1) * BLOCK]) for k in range(n)]
    if threshold is None:
        threshold = max(float(np.percentile(levels, 5)) * NOISE_FACTOR, MIN_THRESHOLD)
    loud = [lv > threshold for lv in levels]
    start = next((k for k in range(n - START_BLOCKS + 1) if all(loud[k:k + START_BLOCKS])), None)
    if start is None:
        return None
    end = max(k for k in range(n) if loud[k])
    if sum(loud[start:end + 1]) < MIN_SPEECH_BLOCKS:
        return None
    # A short burst just before the speech is the key click: start after it.
    click_end = max((k for k in range(start) if loud[k]), default=-10)
    cut_from = max(start - PRE_ROLL, click_end + 2, 0)
    cut_to = min(n, end + 1 + PRE_ROLL)
    return audio[cut_from * BLOCK:cut_to * BLOCK]


# ---- live recording ----------------------------------------------------------------------------

class Keys:
    """Lines typed while recording (a letter, then Enter), read in the background so a
    hands-free block never blocks on the keyboard."""
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in sys.stdin:
            self.q.put(line.strip().lower())
        self.q.put("q")

    def wait(self) -> str:
        return self.q.get()

    def poll(self) -> str | None:
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None


class Mic:
    def __init__(self, device=None):
        import sounddevice as sd
        self.sd = sd
        self.device = device
        self.q: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                     device=device, callback=lambda d, *_: self.q.put(d[:, 0].copy()))
        self.stream.start()

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def skip(self, seconds: float):
        """Throw away what the mic hears for a moment (after a key press or a beep)."""
        time.sleep(seconds)
        self.drain()

    def blocks(self):
        while True:
            yield self.q.get()

    def seconds(self, s: float):
        import numpy as np
        self.drain()
        return np.concatenate([self.q.get() for _ in range(int(s * RATE / BLOCK))])

    def beep(self, freq: float = 880.0, dur: float = 0.12):
        import numpy as np
        t = np.arange(int(RATE * dur)) / RATE
        tone = (0.25 * np.sin(2 * np.pi * freq * t) * np.minimum(1, (dur - t) * 40)).astype(np.float32)
        self.sd.play(tone, RATE)
        self.sd.wait()
        self.skip(0.25)  # the mic heard the beep; don't let it start a take


def save(path: Path, audio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())


def load(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def next_index(folder: Path, label: str) -> int:
    nums = [int(p.stem.rsplit("-", 1)[1]) for p in folder.glob(f"{label}-*.wav") if p.stem.rsplit("-", 1)[1].isdigit()]
    return max(nums, default=0) + 1


def log(person: str, record: dict) -> None:
    path = DATA / person / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**record, "at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")


def calibrate(mic: Mic) -> float:
    print("Quiet for a second, measuring the room…")
    noise = rms(mic.seconds(1.5))
    threshold = max(noise * NOISE_FACTOR, MIN_THRESHOLD)
    print(f"  room level {noise:.0f}, speech threshold {threshold:.0f}")
    return threshold


def countdown(mic: Mic, seconds: int):
    for k in range(seconds, 0, -1):
        print(f"   {k}…", end=" ", flush=True)
        time.sleep(1)
    print()
    mic.beep(660)


def record_takes(mic, keys, threshold, person, kind, label, instruction, count, prompts=None) -> bool:
    """One hands-free block of short takes. Returns False if the user quit."""
    folder = DATA / person / kind
    lead = FAR_LEAD_S if label == "far" else LEAD_S
    print(f"\n== {kind}/{label} ({count} takes): {instruction}")
    print(f"   Enter = start (you get {lead} s to get in place), s = skip block, q = quit")
    cmd = keys.wait()
    if cmd in ("q", "s"):
        return cmd == "s"
    countdown(mic, lead)
    n, saved = 0, []
    if prompts:
        print(f"   Say: {prompts[0]}")
    else:
        print("   Listening. Say it, pause for the beep, say it again…")
    while n < count:
        cmd = keys.poll()
        if cmd is not None:
            if cmd == "q":
                return False
            if cmd == "s":
                return True
            if cmd == "r":
                if saved:
                    saved.pop().unlink(missing_ok=True)
                    n -= 1
                    print(f"   deleted the last take; {count - n} to go")
                else:
                    print("   nothing to delete in this block")
            else:  # Enter or p: pause
                input_wait = "   Paused. Enter to carry on…"
                print(input_wait)
                if keys.wait() == "q":
                    return False
                countdown(mic, lead)
            mic.skip(0.4)  # the key click
            continue
        audio = cut_take(mic.blocks(), threshold, wait_s=LISTEN_SLICE_S)
        if audio is None:
            continue
        if speech_blocks(audio, threshold) < MIN_SPEECH_BLOCKS:
            print("   (ignored a short noise)")
            continue
        path = folder / f"{label}-{next_index(folder, label):03d}.wav"
        save(path, audio)
        saved.append(path)
        n += 1
        log(person, {"file": str(path.relative_to(DATA)), "kind": kind, "label": label,
                     "instruction": instruction, "prompt": prompts[n - 1] if prompts else None,
                     "seconds": round(len(audio) / RATE, 2)})
        print(f"   [{n}/{count}] saved {path.name} ({len(audio) / RATE:.1f} s)")
        mic.beep()
        if prompts and n < count:
            print(f"   Say: {prompts[n]}")
    return True


def record_timed(mic, keys, person, kind, label, instruction, seconds, lines=None) -> bool:
    print(f"\n== {kind}/{label} ({seconds} s): {instruction}")
    for line in lines or []:
        print(f"   {line}")
    print("   Enter = start, s = skip, q = quit. While recording, q + Enter stops early.")
    cmd = keys.wait()
    if cmd in ("q", "s"):
        return cmd == "s"
    countdown(mic, LEAD_S)
    import numpy as np
    folder = DATA / person / kind
    path = folder / f"{label}-{next_index(folder, label):03d}.wav"
    chunks, t0 = [], time.monotonic()
    print(f"   recording {seconds} s…")
    while time.monotonic() - t0 < seconds:
        chunks.append(mic.q.get())
        if keys.poll() == "q":
            print("   stopped early")
            break
    save(path, np.concatenate(chunks))
    log(person, {"file": str(path.relative_to(DATA)), "kind": kind, "label": label,
                 "instruction": instruction, "seconds": round(time.monotonic() - t0, 1)})
    print(f"   saved {path.name}")
    mic.beep()
    return True


def run(person: str, only: str | None, device) -> None:
    mic = Mic(device)
    keys = Keys()
    threshold = calibrate(mic)
    if person == "room":
        steps = [("timed", k, l, i, s, None) for k, l, i, s in ROOM_TIMED]
        steps += [("takes", "near", f"near-{n + 1}", f"Say: {p}", 10, None) for n, p in enumerate(NEAR_MISSES)]
    else:
        steps = [("takes", k, l, i, c, None) for k, l, i, c in PERSON_TAKES]
        steps += [("timed", "enroll", "read", "Read these lines aloud at a normal pace, at the table.", 75, ENROLL_LINES),
                  ("timed", "enroll", "free", "Talk freely from across the room for 30 s.", 30, None),
                  ("takes", "commands", "cmd", "Real requests, one at a time.", len(COMMANDS), COMMANDS)]
    for mode, kind, label, instruction, amount, extra in steps:
        if only and kind != only:
            continue
        ok = (record_takes(mic, keys, threshold, person, kind, label, instruction, amount, extra) if mode == "takes"
              else record_timed(mic, keys, person, kind, label, instruction, amount, extra))
        if not ok:
            break
    print(f"\nDone. Files are in {DATA / person}")


def tidy(person: str) -> None:
    """Trim the key click off takes already recorded and delete takes with no real speech.
    Originals are kept in room/data/<person>/_originals/ the first time a file changes."""
    base = DATA / person
    if not base.exists():
        raise SystemExit(f"nothing recorded for '{person}' yet ({base})")
    trimmed = removed = kept = 0
    for path in sorted(base.glob("*/*.wav")):
        kind = path.parent.name
        if kind in ("enroll", "neg", "_originals"):
            continue                        # long recordings stay as they are
        audio = load(path)
        clean = tidy_clip(audio)
        backup = base / "_originals" / kind / path.name
        if clean is None or len(clean) < len(audio):
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
        if clean is None:
            path.unlink()
            removed += 1
            print(f"  removed {kind}/{path.name} (no speech)")
        elif len(clean) < len(audio):
            save(path, clean)
            trimmed += 1
            print(f"  trimmed {kind}/{path.name}: {len(audio) / RATE:.2f} s -> {len(clean) / RATE:.2f} s")
        else:
            kept += 1
    print(f"\n{trimmed} trimmed, {removed} removed, {kept} already clean. Originals: {base / '_originals'}")


def selftest() -> None:
    """The segmenter and the tidier on synthetic audio."""
    import numpy as np
    rng = np.random.default_rng(0)
    noise = lambda s: (rng.normal(0, 60, int(s * RATE))).astype(np.int16)
    t = np.arange(int(0.8 * RATE)) / RATE
    word = (np.sin(2 * np.pi * 220 * t) * 6000).astype(np.int16) + noise(0.8)
    click = (rng.normal(0, 9000, int(0.05 * RATE))).astype(np.int16)

    def blocks_of(a):
        return (a[i:i + BLOCK] for i in range(0, len(a) - BLOCK + 1, BLOCK))

    take = cut_take(blocks_of(np.concatenate([noise(1.0), word, noise(1.5)])), threshold=250)
    assert take is not None, "missed the word"
    secs = len(take) / RATE
    assert 0.9 <= secs <= 1.6, f"take is {secs:.2f} s (expected the word plus ~0.6 s of edges)"
    assert cut_take(blocks_of(noise(3)), threshold=250, wait_s=2) is None, "invented a take from silence"
    assert cut_take(blocks_of(np.concatenate([noise(0.5), click, noise(2.5)])), threshold=250, wait_s=2) is None, \
        "a key click started a take"

    recorded = np.concatenate([noise(0.2), click, noise(0.6), word, noise(0.8)])
    clean = tidy_clip(recorded)
    assert clean is not None and abs(rms(clean[:BLOCK * 3]) - 60) < 200, "the click wasn't trimmed off the start"
    assert tidy_clip(np.concatenate([noise(0.3), click, noise(0.5)])) is None, "a click-only take wasn't dropped"
    print(f"selftest ok: {secs:.2f} s take around a 0.80 s word; silence and key clicks give no take; "
          f"--tidy trims a leading click and drops click-only takes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", help="a short name for the speaker (lowercase), or 'room' for background audio")
    ap.add_argument("--only", help="one kind: wake, ya_abdo, secret, enroll, commands (or neg, near for room)")
    ap.add_argument("--device", type=int, help="input device number from --list-devices")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--tidy", action="store_true", help="clean up takes already recorded for --person")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if not args.person:
        ap.error("--person is required (e.g. --person zain, or --person room)")
    if args.tidy:
        tidy(args.person.strip().lower())
        return
    try:
        run(args.person.strip().lower(), args.only, args.device)
    except KeyboardInterrupt:
        print("\nStopped. Everything recorded so far is saved.")


if __name__ == "__main__":
    sys.exit(main())
