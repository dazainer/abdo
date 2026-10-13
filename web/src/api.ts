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
  place: string | null;
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

export interface ProposedClass {
  course: string;
  section: string;
  kind: string;
  days: string[];
  start: string;
  end: string;
  room: string;
  start_date: string;
  end_date: string;
}

export interface ProposedExam {
  course: string;
  date: string;
  start: string;
  end: string;
  room: string;
  kind: "quiz" | "midterm" | "final";
}

export interface SavedClass {
  course: string;
  section: string;
  kind: string;
  day: string;
  start: string;
  end: string;
  room: string;
  term_start: string;
  term_end: string;
}

export interface Exam {
  id: number;
  member_id: number;
  name: string;
  course: string;
  kind: "quiz" | "midterm" | "final";
  starts_at: string;
  ends_at: string | null;
  room: string | null;
  can_delete: boolean;
}

export interface Today {
  free: { from: string; to: string; together: { from: string; to: string }[]; away: string[]; no_timetable: string[] };
  statuses: Status[];
  prayers: { next: string | null; place: string | null; times: PrayerTime[] } | null;
  reminders: Reminder[];
}

export type SkyKind = "clear" | "cloudy" | "fog" | "rain" | "snow" | "storm";

export interface WeatherHour {
  hour: string; // "15:00"
  temp: number;
  kind: SkyKind;
  night: boolean;
  rain: number; // % chance
}

export interface Briefing {
  greeting: string; // the whole phrase, as notifications say it
  greeting_main: string;
  greeting_tail: string; // part of the phrase, set smaller ("" if none)
  greeting_named: boolean; // add "يا <name>" after the main phrase
  name: string;
  date: string;
  day: string; // "Monday 12 October"
  weather: {
    temp: number;
    feels: number;
    sky: string;
    kind: SkyKind;
    high: number;
    low: number;
    rain: number | null;
    night: boolean;
    hours: WeatherHour[];
  } | null;
  place: string | null;
  events: { summary: string; time: string | null }[];
  calendar_ok: boolean;
  money: { net: number; transfers: { direction: "pay" | "receive"; name: string; cents: number }[] };
  classes: { what: string; from: string; to: string }[];
  fridge: { item: string; label: string; state: "soon" | "expired" }[];
  polls: { id: number; question: string }[];
  my_exams: { course: string; kind: string; when: string; time: string; day: string }[];
  others_exams: { name: string; course: string; kind: string; when: string; time: string }[];
  waiting: string[];
  arriving: string[];
  groceries: number;
}

export interface MoneyOverview {
  me: number;
  members: { id: number; name: string }[];
  balances: { member_id: number; name: string; net_cents: number }[];
  transfers: { from_member: number; from_name: string; to_member: number; to_name: string; cents: number }[];
  expenses: {
    id: number;
    description: string;
    amount_cents: number;
    paid_by: number;
    paid_by_name: string;
    spent_on: string;
    shares: { member_id: number; name: string; share_cents: number }[];
    can_delete: boolean;
  }[];
  payments: { id: number; from_name: string; to_name: string; cents: number; settled_on: string; can_delete: boolean }[];
}

export interface FridgeItem {
  id: number;
  item: string;
  owner: string | null;
  mine: boolean;
  expires_on: string | null;
  label: string | null;
  state: "expired" | "soon" | "ok" | null;
}

export interface BusDeparture {
  route: string;
  headsign: string;
  stop: string;
  at: string;
  in_min: number;
  live: boolean;
  delay_min: number;
  arrive: string | null;
  arrive_stop: string | null;
  then_route: string | null;
}

export interface BusTrip {
  name: string;
  ok: boolean;
  departures: BusDeparture[];
}

export interface Poll {
  id: number;
  question: string;
  closed: boolean;
  options: { text: string; count: number; voters: string[] }[];
  waiting_on: string[];
  created_by_name: string | null;
  mine: boolean;
  my_vote: number | null;
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

  goingShopping: (store?: string) => request<{ notified: number }>("POST", "/api/shopping/going", { store }),
  fridge: () => request<{ items: FridgeItem[] }>("GET", "/api/fridge"),
  addFridge: (f: { item: string; expires_on?: string; mine?: boolean }) => request<{ id: number }>("POST", "/api/fridge", f),
  fridgeDone: (id: number, rebuy: boolean) =>
    request<{ ok: true; added_to_list: boolean }>("POST", `/api/fridge/${id}/done`, { rebuy }),
  polls: () => request<{ polls: Poll[] }>("GET", "/api/polls"),
  createPoll: (question: string, options: string[]) => request<{ id: number }>("POST", "/api/polls", { question, options }),
  vote: (id: number, option_index: number) => request<{ ok: true }>("POST", `/api/polls/${id}/vote`, { option_index }),
  closePoll: (id: number) => request<{ ok: true }>("POST", `/api/polls/${id}/close`),
  unvote: (id: number) => request<{ ok: true }>("DELETE", `/api/polls/${id}/vote`),
  deletePoll: (id: number) => request<{ ok: true }>("DELETE", `/api/polls/${id}`),

  packages: () => request<{ packages: Package[] }>("GET", "/api/packages"),
  addPackage: (p: { description: string; owner?: string; carrier?: string; day?: string }) =>
    request<{ id: number }>("POST", "/api/packages", p),
  setPackageStatus: (id: number, status: "arrived" | "picked_up" | "cancelled") =>
    request<{ ok: true }>("PATCH", `/api/packages/${id}`, { status }),

  calendar: (days = 7) =>
    request<{ configured: boolean; events: CalendarEvent[] }>("GET", `/api/calendar?days=${days}`),

  today: () => request<Today>("GET", "/api/today"),
  briefing: () => request<Briefing>("GET", "/api/briefing"),
  buses: (homeOnly = false) => request<{ trips: BusTrip[] }>("GET", homeOnly ? "/api/buses?home=1" : "/api/buses"),
  setStatus: (text: string, until_time?: string, place?: string) =>
    request<{ ok: true; place: string | null }>("PUT", "/api/status", { text, until_time, place }),
  clearStatus: () => request<{ ok: true }>("DELETE", "/api/status"),
  cancelReminder: (id: number) => request<{ ok: true }>("DELETE", `/api/reminders/${id}`),
  prefs: () => request<{ prefs: Prefs }>("GET", "/api/me/prefs"),
  updatePrefs: (p: Partial<Prefs>) => request<{ prefs: Prefs }>("PATCH", "/api/me/prefs", p),

  schedule: () => request<{ classes: SavedClass[] }>("GET", "/api/schedule"),
  parseSchedule: (text: string) =>
    request<{ classes: ProposedClass[]; exams: ProposedExam[]; term_start: string; term_end: string; skipped: string[] }>(
      "POST", "/api/schedule/parse", { text }),
  saveSchedule: (classes: ProposedClass[], term_start: string, term_end: string, exams: ProposedExam[]) =>
    request<{ saved: number; exams_added: number }>("PUT", "/api/schedule",
      { classes, term_start: term_start || null, term_end: term_end || null, exams }),
  exams: () => request<{ exams: Exam[] }>("GET", "/api/exams"),
  addExam: (e: { course: string; kind: string; date: string; time: string; room?: string }) =>
    request<{ id: number }>("POST", "/api/exams", e),
  deleteExam: (id: number) => request<{ ok: true }>("DELETE", `/api/exams/${id}`),

  money: () => request<MoneyOverview>("GET", "/api/money"),
  addExpense: (e: { description: string; amount: string; paid_by?: number; split_with?: number[] }) =>
    request<{ id: number }>("POST", "/api/expenses", e),
  deleteExpense: (id: number) => request<{ ok: true }>("DELETE", `/api/expenses/${id}`),
  addSettlement: (p: { to_member: number; amount: string; from_member?: number }) =>
    request<{ id: number }>("POST", "/api/settlements", p),
  deleteSettlement: (id: number) => request<{ ok: true }>("DELETE", `/api/settlements/${id}`),

  pushKey: () => request<{ key: string | null }>("GET", "/api/push/public-key"),
  pushSubscribe: (sub: PushSubscriptionJSON) => request<{ ok: true }>("POST", "/api/push/subscribe", sub),
  pushUnsubscribe: (endpoint: string) => request<{ ok: true }>("POST", "/api/push/unsubscribe", { endpoint }),
  pushTest: () => request<{ sent: number }>("POST", "/api/push/test"),
};
