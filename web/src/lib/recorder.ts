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

// iOS only plays audio started by a user gesture, and Abdo's spoken reply arrives
// seconds after the press. An AudioContext resumed during the press stays unlocked,
// so the reply is decoded and played through it.
let ctx: AudioContext | null = null;

export function unlockAudio(): void {
  ctx ??= new AudioContext();
  void ctx.resume().catch(() => undefined);
}

export async function playMp3(base64: string): Promise<boolean> {
  try {
    ctx ??= new AudioContext();
    if (ctx.state === "suspended") await ctx.resume();
    if (ctx.state !== "running") return false;
    const raw = atob(base64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const buffer = await ctx.decodeAudioData(bytes.buffer);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.start();
    return true;
  } catch {
    return false; // blocked or undecodable: the bubble keeps a play button
  }
}
