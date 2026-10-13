import type { QueryClient } from "@tanstack/react-query";
import { todayISO } from "./dates";

// Opening the app used to wait on the network for everything: who you are, then the
// briefing (calendar + weather). The last good answers are kept on the phone and shown
// immediately; every one of them is refetched at once, because it's seeded as old.
// Only Today's own data, only for this device, and wiped on logout.

const STORE = "abdo:snapshot:v1";
const KEYS = ["me", "today", "briefing", "exams", "calendar"];

type Saved = Record<string, { at: number; data: unknown }>;

function read(): Saved {
  try {
    return JSON.parse(localStorage.getItem(STORE) ?? "{}") as Saved;
  } catch {
    return {};
  }
}

export function clearSnapshot(): void {
  try {
    localStorage.removeItem(STORE);
  } catch {
    /* storage unavailable: nothing to clear */
  }
}

export function restoreSnapshot(qc: QueryClient): void {
  const saved = read();
  for (const [k, v] of Object.entries(saved)) {
    const key = JSON.parse(k) as unknown[];
    // Yesterday's briefing would greet you with yesterday's weather: skip it.
    if (key[0] === "briefing" && (v.data as { date?: string })?.date !== todayISO()) continue;
    qc.setQueryData(key, v.data, { updatedAt: 1 }); // "old", so it refetches on first use
  }
}

export function keepSnapshot(qc: QueryClient): void {
  qc.getQueryCache().subscribe((event) => {
    if (event.type !== "updated" || event.action.type !== "success") return;
    const key = event.query.queryKey;
    if (!KEYS.includes(String(key[0]))) return;
    try {
      const saved = read();
      saved[JSON.stringify(key)] = { at: Date.now(), data: event.query.state.data };
      localStorage.setItem(STORE, JSON.stringify(saved));
    } catch {
      /* storage full or blocked: the app just loads from the network */
    }
  });
}
