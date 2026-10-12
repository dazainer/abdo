// Typed client for Abdo's /api. Every request carries the session cookie; every
// mutating request sends X-Abdo, which the server requires (CSRF protection).

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface Member {
  id: number;
  name: string;
  arabic_name: string | null;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  channel: "telegram" | "web" | "room";
  modality: "text" | "voice";
  created_at: string;
  speaker: string | null;
}

export interface ShoppingItem {
  id: number;
  item: string;
  qty: string | null;
}

export interface Package {
  id: number;
  description: string;
  carrier: string | null;
  owner: string | null;
  status: "expected" | "arrived";
  expected_on: string | null;
}

export interface CalendarEvent {
  id: string;
  summary: string;
  date: string;       // local ISO date, resolved by the server
  day: string;        // "Friday 16 October 2026"
  time: string | null; // "19:00", or null for all-day
}

export interface Status {
  member_id: number;
  name: string;
  text: string;
  until: string | null;
}

export interface PrayerTime {
  name: string;
  ar: string;
  en: string;
  at: string;
  label: string; // "3:52 p.m."
}

export interface Reminder {
  id: number;
  text: string;
  due_at: string;
  set_by: string | null;
}

export interface Today {
  statuses: Status[];
  prayers: { next: string | null; times: PrayerTime[] } | null;
  reminders: Reminder[];
}

export interface Briefing {
  greeting: string;
  name: string;
  date: string;
  day: string; // "Monday 12 October"
  weather: {
    temp: number;
    feels: number;
    sky: string;
    kind: "clear" | "cloudy" | "fog" | "rain" | "snow" | "storm";
    high: number;
    low: number;
    rain: number | null;
  } | null;
  events: { summary: string; time: string | null }[];
  calendar_ok: boolean;
  waiting: string[];
  arriving: string[];
  groceries: number;
}

export interface Prefs {
  briefing: boolean;
  briefing_time: string;
  prayers: boolean;
}

export interface VoiceReply {
  transcript: string;
  reply: string;
  audio: string | null; // base64 MP3
}

function messageFrom(status: number, body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  if (status === 401) return "You're logged out.";
  if (status >= 500) return "Abdo's server had a problem. Try again in a moment.";
  return "That didn't work. Try again.";
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (method !== "GET") headers["X-Abdo"] = "1";
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "No connection. Check your internet and try again.");
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) throw new ApiError(res.status, messageFrom(res.status, data));
  return data as T;
}

export const api = {
  me: () => request<{ member: Member }>("GET", "/api/me"),
  requestCode: (name: string) => request<{ ok: true }>("POST", "/api/auth/request-code", { name }),
  verify: (name: string, code: string) =>
    request<{ member: Member }>("POST", "/api/auth/verify", { name, code }),
  logout: () => request<{ ok: true }>("POST", "/api/auth/logout"),

  history: (before?: number, limit = 30) =>
    request<{ messages: ChatMessage[]; has_more: boolean }>(
      "GET",
      `/api/chat/history?limit=${limit}${before ? `&before=${before}` : ""}`,
    ),
  chat: (text: string) => request<{ reply: string }>("POST", "/api/chat", { text }),
  voice: (audio: Blob, filename: string) => {
    const form = new FormData();
    form.append("audio", audio, filename);
    return request<VoiceReply>("POST", "/api/chat/voice", form);
  },

  shopping: () => request<{ items: ShoppingItem[] }>("GET", "/api/shopping"),
  addItem: (item: string, qty?: string) => request<{ ok: true }>("POST", "/api/shopping", { item, qty }),
  boughtItem: (item: string) => request<{ ok: true }>("POST", "/api/shopping/bought", { item }),

  packages: () => request<{ packages: Package[] }>("GET", "/api/packages"),
  addPackage: (p: { description: string; owner?: string; carrier?: string; day?: string }) =>
    request<{ id: number }>("POST", "/api/packages", p),
  setPackageStatus: (id: number, status: "arrived" | "picked_up" | "cancelled") =>
    request<{ ok: true }>("PATCH", `/api/packages/${id}`, { status }),

  calendar: (days = 7) =>
    request<{ configured: boolean; events: CalendarEvent[] }>("GET", `/api/calendar?days=${days}`),

  today: () => request<Today>("GET", "/api/today"),
  briefing: () => request<Briefing>("GET", "/api/briefing"),
  setStatus: (text: string, until_time?: string) =>
    request<{ ok: true }>("PUT", "/api/status", { text, until_time }),
  clearStatus: () => request<{ ok: true }>("DELETE", "/api/status"),
  cancelReminder: (id: number) => request<{ ok: true }>("DELETE", `/api/reminders/${id}`),
  prefs: () => request<{ prefs: Prefs }>("GET", "/api/me/prefs"),
  updatePrefs: (p: Partial<Prefs>) => request<{ prefs: Prefs }>("PATCH", "/api/me/prefs", p),

  pushKey: () => request<{ key: string | null }>("GET", "/api/push/public-key"),
  pushSubscribe: (sub: PushSubscriptionJSON) => request<{ ok: true }>("POST", "/api/push/subscribe", sub),
  pushUnsubscribe: (endpoint: string) => request<{ ok: true }>("POST", "/api/push/unsubscribe", { endpoint }),
  pushTest: () => request<{ sent: number }>("POST", "/api/push/test"),
};
