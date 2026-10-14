import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A stand-in for the browser's AudioContext that can misbehave the way iOS does:
// stay suspended, or hang on resume() until the next tap.
class FakeContext {
  static instances: FakeContext[] = [];
  static startState: string = "running";
  static hangOnResume = false;
  state: string = FakeContext.startState;
  destination = {};
  sampleRate = 48000;
  // Every source made; `voices` are the real replies (the silent keep-alive loops).
  sources: { start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; loop?: boolean }[] = [];
  get voices() {
    return this.sources.filter((s) => !s.loop);
  }
  get silence() {
    return this.sources.filter((s) => s.loop);
  }
  constructor() {
    FakeContext.instances.push(this);
  }
  resume() {
    if (FakeContext.hangOnResume) return new Promise<void>(() => {});
    this.state = "running";
    return Promise.resolve();
  }
  close() {
    this.state = "closed";
    return Promise.resolve();
  }
  createBuffer() {
    return {};
  }
  decodeAudioData() {
    return Promise.resolve({});
  }
  createBufferSource() {
    const src = { buffer: null, connect: vi.fn(), start: vi.fn(), stop: vi.fn(), onended: null };
    this.sources.push(src);
    return src;
  }
}

const MP3 = btoa("fake-mp3");

async function load() {
  vi.resetModules(); // fresh module state (the shared context) per test
  return import("./recorder");
}

beforeEach(() => {
  FakeContext.instances = [];
  FakeContext.startState = "running";
  FakeContext.hangOnResume = false;
  vi.stubGlobal("AudioContext", FakeContext);
});
afterEach(() => vi.unstubAllGlobals());

describe("playMp3", () => {
  it("won't play before any tap has unlocked audio", async () => {
    const { playMp3 } = await load();
    expect(await playMp3(MP3)).toBe(false);
  });

  it("plays after a tap unlocked it", async () => {
    const { playMp3, unlockAudio } = await load();
    unlockAudio();
    expect(await playMp3(MP3)).toBe(true);
    expect(FakeContext.instances[0].voices[0].start).toHaveBeenCalled();
  });

  it("gives up quickly when iOS hangs resume(), and never plays that reply later", async () => {
    const { playMp3, unlockAudio, RESUME_WAIT_MS } = await load();
    unlockAudio();
    const ctx = FakeContext.instances[0];
    ctx.state = "suspended"; // iOS suspended it after the mic was used
    FakeContext.hangOnResume = true;
    const started = Date.now();
    expect(await playMp3(MP3)).toBe(false);
    expect(Date.now() - started).toBeLessThan(RESUME_WAIT_MS + 300);
    ctx.state = "running"; // the next tap would resume it…
    await new Promise((r) => setTimeout(r, 20));
    expect(ctx.voices).toHaveLength(0); // …but the old reply doesn't burst out
  });

  it("a tap replaces an interrupted context and plays", async () => {
    const { playMp3, unlockAudio } = await load();
    unlockAudio();
    FakeContext.instances[0].state = "interrupted";
    expect(await playMp3(MP3, { fromTap: true })).toBe(true);
    expect(FakeContext.instances).toHaveLength(2);
    expect(FakeContext.instances[1].voices[0].start).toHaveBeenCalled();
  });

  it("unlock with fresh: true always starts a new running context (after the mic closes)", async () => {
    const { playMp3, unlockAudio } = await load();
    unlockAudio();
    FakeContext.instances[0].state = "running";
    unlockAudio({ fresh: true });
    expect(FakeContext.instances).toHaveLength(2);
    expect(FakeContext.instances[0].state).toBe("closed");
    expect(await playMp3(MP3)).toBe(true);
    expect(FakeContext.instances[1].voices[0].start).toHaveBeenCalled();
  });

  it("plays one voice at a time", async () => {
    const { playMp3, unlockAudio } = await load();
    unlockAudio();
    await playMp3(MP3);
    await playMp3(MP3);
    const [first, second] = FakeContext.instances[0].voices;
    expect(first.stop).toHaveBeenCalled();
    expect(second.start).toHaveBeenCalled();
  });
});

describe("spoken replies", () => {
  it("keeps audio for every recent reply, capped", async () => {
    const { rememberSpoken, spokenAudio } = await load();
    rememberSpoken("first", "a");
    rememberSpoken("second", "b");
    expect(spokenAudio("first")).toBe("a");
    for (let i = 0; i < 25; i++) rememberSpoken(`r${i}`, "x");
    expect(spokenAudio("first")).toBeUndefined();
    expect(spokenAudio("r24")).toBe("x");
  });

  it("keeps the context alive with silence from the tap until the reply plays", async () => {
    const { playMp3, unlockAudio } = await load();
    unlockAudio({ fresh: true });
    const ctx = FakeContext.instances[0];
    expect(ctx.silence).toHaveLength(1);
    expect(ctx.silence[0].start).toHaveBeenCalled();
    expect(await playMp3(MP3)).toBe(true);
    expect(ctx.silence[0].stop).toHaveBeenCalled();
  });
});
