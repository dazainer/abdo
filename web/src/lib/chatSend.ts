import { useSyncExternalStore } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import { playMp3, rememberSpoken, type Recording } from "./recorder";

// A message on its way to Abdo lives here, not in the Chat screen. Switching tabs used to
// unmount Chat mid-send: the message vanished from the screen while the server was still
// answering it, so people said "yes" again and a confirmed calendar event got added three
// times. Now the send carries on, the pending bubble is still there when you come back, and
// the mic stays disabled until the reply lands.

export type Pending = { kind: "text"; text: string } | { kind: "voice" };

let pending: Pending | null = null;
let spokenVersion = 0;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function usePending(): Pending | null {
  return useSyncExternalStore(subscribe, () => pending);
}

/** Changes whenever a reply gains audio, so its Play button appears. */
export function useSpokenVersion(): number {
  return useSyncExternalStore(subscribe, () => spokenVersion);
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/** Send a text message. Returns false if it didn't go through (the caller gives it back). */
export async function sendText(qc: QueryClient, value: string, toast: (m: string) => void): Promise<boolean> {
  if (pending) return false;
  pending = { kind: "text", text: value };
  emit();
  try {
    await api.chat(value);
    await qc.invalidateQueries({ queryKey: ["history"] });
    return true;
  } catch (err) {
    toast(errorText(err, "Couldn't reach Abdo. Try again."));
    return false;
  } finally {
    pending = null;
    emit();
  }
}

export async function sendVoice(qc: QueryClient, rec: Recording, toast: (m: string) => void): Promise<void> {
  if (pending) return;
  pending = { kind: "voice" };
  emit();
  try {
    const res = await api.voice(rec.blob, rec.filename);
    await qc.invalidateQueries({ queryKey: ["history"] });
    if (!res.transcript) {
      toast(res.reply);
      return;
    }
    if (res.audio) {
      rememberSpoken(res.reply, res.audio);
      spokenVersion += 1;
      emit();
      if (!(await playMp3(res.audio))) toast("Tap Play to hear Abdo's reply.");
    }
  } catch (err) {
    toast(errorText(err, "Couldn't send the voice note. Try again."));
  } finally {
    pending = null;
    emit();
  }
}
