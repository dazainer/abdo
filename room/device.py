"""Abdo's room device, v0 (Phase 8a): runs on the Mac in the common room.

    idle → wake → acknowledgement clip ("أيوه يا حبيبي") → record until you stop talking →
    send to the server (a "ثواني" filler if it's slow) → play the reply → 5 s for a follow-up
    without the wake word → idle.

The wake word model isn't trained yet (it needs everyone's recordings), so for now the wake
word is a key: press Enter. Speaker ID isn't in yet either: every turn reaches Abdo as
"Someone". Both are swappable pieces (Wake, identify_speaker) for when the models exist.

    .venv/bin/python room/device.py                 # run it (Enter = "بقولك يا عبده")
    .venv/bin/python room/device.py --say "فين مو؟"  # one text turn, no mic (to test the server)
    .venv/bin/python room/device.py --device 2      # a specific mic (see record_session --list-devices)

Config: room/device.env (gitignored): ABDO_URL, ROOM_DEVICE_TOKEN, optional MIC_DEVICE.
While running, type and Enter:  (nothing) = wake,  any text = send it as a turn,
unmute = come back after "اسكت يا عبده",  q = quit.
"""
import argparse
import base64
import io
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import record_session as rec  # noqa: E402  (the mic + speech-cutting code from the recorder)

CLIPS = ROOT / "room" / "clips"
CONFIG = ROOT / "room" / "device.env"

LISTEN_AFTER_WAKE_S = 6     # time to start talking after the acknowledgement
FOLLOW_UP_S = 5             # a follow-up without the wake word
THINKING_AFTER_S = 2.5      # play a "ثواني" filler if the server hasn't answered by then
REQUEST_TIMEOUT_S = 60


# ---- config + audio out -----------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit(f"Missing {CONFIG}. It needs ABDO_URL and ROOM_DEVICE_TOKEN (see the setup steps).")
    cfg = {}
    for line in CONFIG.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for key in ("ABDO_URL", "ROOM_DEVICE_TOKEN"):
        if not cfg.get(key):
            raise SystemExit(f"{CONFIG} is missing {key}")
    return cfg


def play(path: Path) -> None:
    """Play an audio file and wait for it to finish (afplay on macOS, ffplay elsewhere)."""
    if shutil.which("afplay"):
        cmd = ["afplay", str(path)]
    elif shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
    else:
        print(f"   (no audio player for {path.name})")
        return
    subprocess.run(cmd, check=False)


class Clips:
    """Abdo's pre-recorded lines by category; never the same one twice in a row."""
    def __init__(self):
        self.last: dict[str, Path] = {}

    def pick(self, category: str) -> Path | None:
        files = sorted((CLIPS / category).glob("*.mp3")) + sorted((CLIPS / category).glob("*.wav"))
        if not files:
            return None
        options = [f for f in files if f != self.last.get(category)] or files
        choice = random.choice(options)
        self.last[category] = choice
        return choice

    def play(self, category: str, name: str | None = None) -> None:
        path = (CLIPS / category / name) if name else self.pick(category)
        if path and path.exists():
            play(path)


def wav_bytes(audio) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rec.RATE)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


# ---- talking to the server ---------------------------------------------------------------------

def send_turn(cfg: dict, *, audio=None, text: str | None = None, speaker=None, confidence=None) -> dict:
    import httpx
    files = {"audio": ("room.wav", wav_bytes(audio), "audio/wav")} if audio is not None else None
    data = {k: v for k, v in {"text": text, "speaker": speaker,
                              "confidence": None if confidence is None else str(confidence)}.items() if v}
    r = httpx.post(f"{cfg['ABDO_URL'].rstrip('/')}/api/room/turn", files=files, data=data,
                   headers={"Authorization": f"Bearer {cfg['ROOM_DEVICE_TOKEN']}"}, timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def identify_speaker(audio) -> tuple[str | None, float | None]:
    """Who's talking. v0: nobody yet (the voiceprints come after everyone records)."""
    return None, None


# ---- the device ----------------------------------------------------------------------------------

class Device:
    def __init__(self, cfg: dict, mic_device=None):
        self.cfg = cfg
        self.clips = Clips()
        self.mic = rec.Mic(mic_device)
        self.keys: queue.Queue = queue.Queue()
        self.muted = False
        threading.Thread(target=self._read_keys, daemon=True).start()
        self.threshold = rec.calibrate(self.mic)

    def _read_keys(self):
        for line in sys.stdin:
            self.keys.put(line.rstrip("\n"))
        self.keys.put("q")

    def listen(self, wait_s: float):
        """One utterance (or None), cut by the recorder's speech detector."""
        self.mic.drain()
        audio = rec.cut_take(self.mic.blocks(), self.threshold, wait_s=wait_s)
        if audio is None or rec.speech_blocks(audio, self.threshold) < rec.MIN_SPEECH_BLOCKS:
            return None
        return audio

    def turn(self, *, audio=None, text=None) -> bool:
        """Send one turn and speak the answer. Returns True if a follow-up makes sense."""
        speaker, confidence = identify_speaker(audio) if audio is not None else (None, None)
        result: dict = {}
        error: list = []

        def call():
            try:
                result.update(send_turn(self.cfg, audio=audio, text=text, speaker=speaker, confidence=confidence))
            except Exception as e:
                error.append(e)

        worker = threading.Thread(target=call)
        worker.start()
        worker.join(THINKING_AFTER_S)
        if worker.is_alive():
            self.clips.play("thinking")
            worker.join()
        if error:
            print(f"   server error: {error[0]}")
            self.clips.play("error", "error-offline.mp3")
            return False
        if not result.get("heard"):
            print("   (nothing understood)")
            self.clips.play("error", "error-didnt_hear.mp3")
            return False
        print(f"   heard: {result['transcript']}")
        if result.get("action") == "mute":
            self.clips.play("mute")
            self.muted = True
            print("   muted. Type 'unmute' to bring him back (the secret phrase comes with the wake model).")
            return False
        print(f"   Abdo: {result['reply']}")
        if result.get("audio"):
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(base64.b64decode(result["audio"]))
            play(Path(f.name))
            Path(f.name).unlink(missing_ok=True)
        return True

    def wake(self):
        print("\n[wake]")
        self.clips.play("ack")
        audio = self.listen(LISTEN_AFTER_WAKE_S)
        if audio is None:
            print("   (didn't hear a request)")
            return
        follow = self.turn(audio=audio)
        while follow:
            print(f"   (listening {FOLLOW_UP_S} s for a follow-up…)")
            audio = self.listen(FOLLOW_UP_S)
            if audio is None:
                break
            follow = self.turn(audio=audio)

    def run(self):
        print("\nAbdo's room device v0. Enter = wake word (\"بقولك يا عبده\"), or type a request and Enter. q = quit.")
        while True:
            line = self.keys.get()
            cmd = line.strip()
            if cmd.lower() == "q":
                return
            if cmd.lower() == "unmute":
                if self.muted:
                    self.muted = False
                    self.clips.play("unmute")
                    print("   unmuted")
                continue
            if self.muted:
                print("   (muted: type 'unmute')")
                continue
            if cmd:
                print(f"\n[text] {cmd}")
                self.turn(text=cmd)
            else:
                self.wake()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--say", help="send one text turn and play the answer (no mic)")
    ap.add_argument("--device", type=int, help="mic device number")
    args = ap.parse_args()
    cfg = load_config()
    if args.say:
        result = send_turn(cfg, text=args.say)
        print(result.get("reply") or result)
        if result.get("audio"):
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(base64.b64decode(result["audio"]))
            play(Path(f.name))
        return
    mic = args.device if args.device is not None else (int(cfg["MIC_DEVICE"]) if cfg.get("MIC_DEVICE") else None)
    try:
        Device(cfg, mic).run()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
