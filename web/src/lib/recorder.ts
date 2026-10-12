// Microphone recording for hold-to-talk. Chrome/Android record WebM/Opus,
// Safari/iOS record MP4/AAC; the server passes either straight to speech-to-text.

const PREFERRED = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/aac"];

export const MAX_SECONDS = 60;

export interface Recording {
  blob: Blob;
  filename: string;
  seconds: number;
}

export function canRecord(): boolean {
  return typeof window !== "undefined" && "MediaRecorder" in window && !!navigator.mediaDevices?.getUserMedia;
}

function pickType(): string | undefined {
  return PREFERRED.find((t) => MediaRecorder.isTypeSupported(t));
}

function extensionFor(type: string): string {
  if (type.includes("webm")) return "webm";
  if (type.includes("mp4")) return "m4a";
  if (type.includes("aac")) return "aac";
  return "audio";
}

export class Recorder {
  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: Blob[] = [];
  private startedAt = 0;
  private timer: number | undefined;

  /** Ask for the mic and start recording. Rejects if permission is denied. */
  async start(onAutoStop: () => void): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = pickType();
    this.recorder = new MediaRecorder(this.stream, type ? { mimeType: type } : undefined);
    this.chunks = [];
    this.recorder.ondataavailable = (e) => {
      if (e.data.size > 0) this.chunks.push(e.data);
    };
    this.recorder.start();
    this.startedAt = performance.now();
    this.timer = window.setTimeout(onAutoStop, MAX_SECONDS * 1000);
  }

  /** Stop and return the recording (always releases the mic). */
  stop(): Promise<Recording | null> {
    window.clearTimeout(this.timer);
    const recorder = this.recorder;
    if (!recorder || recorder.state === "inactive") {
      this.release();
      return Promise.resolve(null);
    }
    return new Promise((resolve) => {
      recorder.onstop = () => {
        const type = recorder.mimeType || "audio/webm";
        const blob = new Blob(this.chunks, { type });
        const seconds = (performance.now() - this.startedAt) / 1000;
        this.release();
        resolve({ blob, filename: `voice.${extensionFor(type)}`, seconds });
      };
      recorder.stop();
    });
  }

  cancel(): void {
    window.clearTimeout(this.timer);
    if (this.recorder && this.recorder.state !== "inactive") {
      this.recorder.onstop = null;
      this.recorder.stop();
    }
    this.release();
  }

  private release(): void {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.recorder = null;
  }
}

// ---- playback --------------------------------------------------------------------
// iOS only starts audio from a tap, and after the mic has been used it often leaves the
// AudioContext suspended (or "interrupted"). Two rules keep that from biting:
//   1. Every tap that might lead to audio calls unlockAudio(), which resumes the context,
//      or replaces it with a fresh one (a context created inside a tap starts running).
//   2. playMp3() never waits on resume() for long. Outside a tap, iOS can leave that promise
//      pending until the NEXT tap; waiting on it used to queue replies silently and then play
//      them all at once on the next mic press. Now it gives up after a moment and the bubble
//      keeps its play button instead.

type AudioSessionNavigator = Navigator & { audioSession?: { type: string } };

let ctx: AudioContext | null = null;
let current: AudioBufferSourceNode | null = null;

function setSession(type: "auto" | "playback"): void {
  // Safari 16.4+: "playback" lets Abdo's voice play even with the ringer switch on silent.
  const session = (navigator as AudioSessionNavigator).audioSession;
  if (session) {
    try {
      session.type = type;
    } catch {
      /* unsupported value on this browser */
    }
  }
}

/** Before using the mic: let the browser pick the right session for recording. */
export function prepareToRecord(): void {
  setSession("auto");
}

/** Call inside a tap or press, before anything that may play audio. */
export function unlockAudio(): void {
  const state = ctx?.state as string | undefined;
  if (!ctx || state === "closed" || state === "interrupted") {
    void ctx?.close().catch(() => undefined);
    ctx = new AudioContext();
  } else if (state === "suspended") {
    void ctx.resume().catch(() => undefined);
  }
}

export function stopAudio(): void {
  try {
    current?.stop();
  } catch {
    /* already stopped */
  }
  current = null;
}

function within<T>(promise: Promise<T>, ms: number): Promise<void> {
  return Promise.race([promise.then(() => undefined, () => undefined),
                       new Promise<void>((r) => setTimeout(r, ms))]);
}

export const RESUME_WAIT_MS = 400;

/** Play base64 MP3. Returns false (never hangs) if the browser won't play right now. */
export async function playMp3(base64: string, opts: { fromTap?: boolean } = {}): Promise<boolean> {
  try {
    if (opts.fromTap) unlockAudio();
    const context = ctx;
    if (!context) return false; // nothing was ever unlocked by a tap
    if (context.state !== "running") {
      await within(context.resume(), RESUME_WAIT_MS);
      // (state changes during the await; TypeScript's narrowing can't see that)
      if ((context.state as AudioContextState) !== "running") return false;
    }
    setSession("playback");
    const raw = atob(base64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const buffer = await context.decodeAudioData(bytes.buffer);
    stopAudio(); // one voice at a time: a new reply cuts off the previous one
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => {
      if (current === source) current = null;
    };
    source.start();
    current = source;
    return true;
  } catch {
    return false;
  }
}

// Spoken replies from this session, by reply text, so every voice reply keeps its play
// button (also after switching tabs), not just the latest one.
const spoken = new Map<string, string>();
const SPOKEN_LIMIT = 20;

export function rememberSpoken(reply: string, base64: string): void {
  spoken.delete(reply);
  spoken.set(reply, base64);
  while (spoken.size > SPOKEN_LIMIT) spoken.delete(spoken.keys().next().value as string);
}

export function spokenAudio(reply: string): string | undefined {
  return spoken.get(reply);
}
