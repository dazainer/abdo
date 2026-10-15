"""Guided recorder for the Phase 8a recording session (docs/abdo_phase8a_spec.md §3).

Shows one prompt at a time, records from the Mac's mic, cuts each take automatically when the
speaker stops, and files it as 16 kHz mono WAV under room/data/<person>/<kind>/. Nobody types
file names or edits audio afterwards. Re-running resumes: numbering continues.

    pip install -r room/requirements.txt          # once (sounddevice, numpy)
    .venv/bin/python scripts/record_session.py --list-devices
    .venv/bin/python scripts/record_session.py --person zain           # everyone, one by one
    .venv/bin/python scripts/record_session.py --person room           # background + near misses
    .venv/bin/python scripts/record_session.py --person zain --only secret   # one block
    .venv/bin/python scripts/record_session.py --selftest               # no mic needed

Per take: press Enter, then speak; it stops by itself after a short silence.
Between takes: r = redo the last take, s = skip the rest of this block, q = save and quit.
Recordings stay on this Mac (room/data/ is gitignored).
"""
import argparse
import json
import queue
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "room" / "data"

RATE = 16000
BLOCK = 480                 # 30 ms
PRE_ROLL = 10               # blocks (300 ms) kept before speech starts
START_BLOCKS = 2            # 60 ms above threshold = speech
END_SILENCE = 20            # 600 ms below threshold = done
MAX_TAKE_S = 8
WAIT_FOR_SPEECH_S = 10

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


# ---- the segmenter (pure: shared by the live recorder and the self-test) ------------

def rms(block) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) if len(block) else 0.0


def cut_take(blocks, threshold: float):
    """Take blocks of int16 audio until one utterance has been heard and has ended.
    Returns the utterance (with 300 ms before it and the trailing silence trimmed to
    300 ms), or None if nobody spoke in time."""
    import numpy as np
    pre, take = [], []
    loud_run = quiet_run = 0
    started = False
    max_blocks = int(MAX_TAKE_S * RATE / BLOCK)
    wait_blocks = int(WAIT_FOR_SPEECH_S * RATE / BLOCK)
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


# ---- live recording -------------------------------------------------------------------

class Mic:
    def __init__(self, device=None):
        import sounddevice as sd
        self.q: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                     device=device, callback=lambda d, *_: self.q.put(d[:, 0].copy()))
        self.stream.start()

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def blocks(self):
        while True:
            yield self.q.get()

    def seconds(self, s: float):
        import numpy as np
        self.drain()
        return np.concatenate([self.q.get() for _ in range(int(s * RATE / BLOCK))])


def save(path: Path, audio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())


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
    threshold = max(noise * 3.5, 250.0)
    print(f"  room level {noise:.0f}, speech threshold {threshold:.0f}")
    return threshold


def record_takes(mic, threshold, person, kind, label, instruction, count, prompts=None) -> bool:
    """One block of short takes. Returns False if the user quit."""
    folder = DATA / person / kind
    print(f"\n== {kind}/{label}: {instruction}")
    n, last = 0, None
    while n < count:
        prompt = prompts[n] if prompts else None
        if prompt:
            print(f"   Say: {prompt}")
        key = input(f"[{n + 1}/{count}] Enter = record, r = redo last, s = skip block, q = quit: ").strip().lower()
        if key == "q":
            return False
        if key == "s":
            return True
        if key == "r":
            if last and last.exists():
                last.unlink()
                n -= 1
                print("   removed the last take; record it again")
            continue
        mic.drain()
        print("   listening…")
        audio = cut_take(mic.blocks(), threshold)
        if audio is None:
            print("   didn't hear anything; try again (closer, or louder)")
            continue
        last = folder / f"{label}-{next_index(folder, label):03d}.wav"
        save(last, audio)
        log(person, {"file": str(last.relative_to(DATA)), "kind": kind, "label": label,
                     "instruction": instruction, "prompt": prompt, "seconds": round(len(audio) / RATE, 2)})
        print(f"   saved {last.name} ({len(audio) / RATE:.1f} s)")
        n += 1
    return True


def record_timed(mic, person, kind, label, instruction, seconds, lines=None) -> bool:
    print(f"\n== {kind}/{label}: {instruction}")
    if lines:
        for line in lines:
            print(f"   {line}")
    key = input(f"Enter = start {seconds} s of recording, s = skip, q = quit: ").strip().lower()
    if key in ("s", "q"):
        return key == "s"
    folder = DATA / person / kind
    path = folder / f"{label}-{next_index(folder, label):03d}.wav"
    print(f"   recording {seconds} s… (Ctrl+C stops early and keeps what's recorded)")
    import numpy as np
    mic.drain()
    chunks, t0 = [], time.monotonic()
    try:
        while time.monotonic() - t0 < seconds:
            chunks.append(mic.q.get())
    except KeyboardInterrupt:
        print("   stopped early")
    save(path, np.concatenate(chunks))
    log(person, {"file": str(path.relative_to(DATA)), "kind": kind, "label": label,
                 "instruction": instruction, "seconds": round(time.monotonic() - t0, 1)})
    print(f"   saved {path.name}")
    return True


def run(person: str, only: str | None, device) -> None:
    mic = Mic(device)
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
        ok = (record_takes(mic, threshold, person, kind, label, instruction, amount, extra) if mode == "takes"
              else record_timed(mic, person, kind, label, instruction, amount, extra))
        if not ok:
            break
    print(f"\nDone. Files are in {DATA / person}")


def selftest() -> None:
    """The segmenter on synthetic audio: silence, a 0.8 s 'word', silence."""
    import numpy as np
    rng = np.random.default_rng(0)
    noise = lambda s: (rng.normal(0, 60, int(s * RATE))).astype(np.int16)
    t = np.arange(int(0.8 * RATE)) / RATE
    word = (np.sin(2 * np.pi * 220 * t) * 6000).astype(np.int16) + noise(0.8)
    audio = np.concatenate([noise(1.0), word, noise(1.5)])
    blocks = (audio[i:i + BLOCK] for i in range(0, len(audio) - BLOCK + 1, BLOCK))
    take = cut_take(blocks, threshold=250)
    assert take is not None, "missed the word"
    secs = len(take) / RATE
    assert 0.9 <= secs <= 1.6, f"take is {secs:.2f} s (expected the word plus ~0.6 s of edges)"
    silent = (noise(0.03) for _ in range(int(WAIT_FOR_SPEECH_S * RATE / BLOCK) + 5))
    assert cut_take(silent, threshold=250) is None, "invented a take from silence"
    print(f"selftest ok: cut a {secs:.2f} s take around a 0.80 s word; silence gives no take")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", help="a short name for the speaker (lowercase), or 'room' for background audio")
    ap.add_argument("--only", help="one kind: wake, ya_abdo, secret, enroll, commands (or neg, near for room)")
    ap.add_argument("--device", type=int, help="input device number from --list-devices")
    ap.add_argument("--list-devices", action="store_true")
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
    try:
        run(args.person.strip().lower(), args.only, args.device)
    except KeyboardInterrupt:
        print("\nStopped. Everything recorded so far is saved.")


if __name__ == "__main__":
    sys.exit(main())
