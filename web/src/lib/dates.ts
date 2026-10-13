// Display helpers. Day *resolution* (what "Friday" means) always happens on the
// server; these only format dates the server already resolved.

function localISO(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** "Today" / "Tomorrow" / "Yesterday" / "Friday 16 October" for a local ISO date. */
export function relativeDay(iso: string, now = new Date()): string {
  const today = localISO(now);
  const tomorrow = localISO(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
  const yesterday = localISO(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  if (iso === today) return "Today";
  if (iso === tomorrow) return "Tomorrow";
  if (iso === yesterday) return "Yesterday";
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-CA", { weekday: "long", day: "numeric", month: "long" });
}

export function dayOf(timestamp: string): string {
  return localISO(new Date(timestamp));
}

export function clock(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit" });
}

/** "4:31 pm", "8 am" on the hour: the app's one short clock format. */
export function shortClock(timestamp: string | number): string {
  const [t, ap] = clock(new Date(timestamp).toISOString()).split(" ");
  return `${t.replace(/:00$/, "")} ${(ap ?? "").replace(/\./g, "")}`.trim();
}

export function todayISO(): string {
  return localISO(new Date());
}

/** A Waterloo term code from its start date: "F26" (Sep-Dec), "W27" (Jan-Apr), "S27" (May-Aug). */
export function termLabel(startISO: string): string {
  const [y, m] = startISO.split("-").map(Number);
  if (!y || !m) return "";
  return `${m >= 9 ? "F" : m >= 5 ? "S" : "W"}${String(y % 100).padStart(2, "0")}`;
}
