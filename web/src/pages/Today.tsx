import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type CalendarEvent, type Member } from "../api";
import { Sign } from "../components/Sign";
import { clock, relativeDay, todayISO, dayOf } from "../lib/dates";

export function Today({ me, toast }: { me: Member; toast: (msg: string) => void }) {
  return (
    <div className="page">
      <header className="screen-head"><Sign ar="النهاردة" en="Today" /></header>
      <main className="screen">
        <Around me={me} toast={toast} />
        <Prayers />
        <MyReminders toast={toast} />
        <Week />
      </main>
    </div>
  );
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

function useToday() {
  return useQuery({ queryKey: ["today"], queryFn: api.today, refetchInterval: 60_000 });
}

function untilLabel(iso: string | null): string {
  if (!iso) return "";
  return ` until ${relativeDay(dayOf(iso)) === "Today" ? "" : relativeDay(dayOf(iso)) + " "}${clock(iso)}`;
}

function Around({ me, toast }: { me: Member; toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const today = useToday();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [until, setUntil] = useState("");

  const statuses = today.data?.statuses ?? [];
  const mine = statuses.find((s) => s.member_id === me.id);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    try {
      await api.setStatus(text.trim(), until || undefined);
      setEditing(false);
      setText("");
      setUntil("");
      await qc.invalidateQueries({ queryKey: ["today"] });
    } catch (err) {
      toast(errorText(err, "Couldn't set your status. Try again."));
    }
  }

  async function clear() {
    try {
      await api.clearStatus();
      await qc.invalidateQueries({ queryKey: ["today"] });
    } catch (err) {
      toast(errorText(err, "Couldn't clear your status."));
    }
  }

  return (
    <section className="section" aria-labelledby="around">
      <h2 className="section-title" id="around">Around the suite</h2>
      {today.isError && <p className="notice notice-error">Couldn't load today.</p>}
      {today.data && statuses.length === 0 && !editing && (
        <p className="empty">No one's set a status. Let the others know if you're busy or away.</p>
      )}
      <ul className="rows">
        {statuses.map((s) => (
          <li className="row" key={s.member_id}>
            <span className="row-main">
              <span className="row-title"><strong>{s.name}</strong> {s.text}</span>
              {s.until && <span className="row-meta">{untilLabel(s.until).trim()}</span>}
            </span>
            {s.member_id === me.id && (
              <button className="btn btn-quiet btn-small" onClick={() => void clear()}>Clear</button>
            )}
          </li>
        ))}
      </ul>
      {editing ? (
        <form onSubmit={save} style={{ display: "grid", gap: 8, marginTop: 10 }}>
          <input className="field" dir="auto" value={text} onChange={(e) => setText(e.target.value)}
                 placeholder="In a midterm, away, studying…" aria-label="Your status" autoFocus maxLength={80} />
          <label className="row-meta" htmlFor="until">Until (optional, today)</label>
          <input id="until" className="field" type="time" value={until} onChange={(e) => setUntil(e.target.value)} />
          <div className="inline-form" style={{ margin: 0 }}>
            <button className="btn" disabled={!text.trim()}>Set status</button>
            <button type="button" className="btn btn-quiet" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <button className="btn btn-quiet" style={{ marginTop: 10 }} onClick={() => setEditing(true)}>
          {mine ? "Change my status" : "Set my status"}
        </button>
      )}
    </section>
  );
}

function Prayers() {
  const today = useToday();
  const prayers = today.data?.prayers;
  if (!today.data) return null;
  return (
    <section className="section" aria-labelledby="prayers">
      <h2 className="section-title" id="prayers">Prayer times</h2>
      {!prayers ? (
        <p className="empty">Prayer times need the suite's location set on the server.</p>
      ) : (
        <ul className="prayers">
          {prayers.times.map((p) => (
            <li key={p.name} className={`prayer${p.name === prayers.next ? " next" : ""}${p.name === "sunrise" ? " sunrise" : ""}`}>
              <span className="prayer-ar" lang="ar">{p.ar}</span>
              <span className="prayer-en">{p.en}</span>
              <span className="prayer-time">{p.label}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function MyReminders({ toast }: { toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const today = useToday();
  const reminders = today.data?.reminders ?? [];

  async function cancel(id: number) {
    try {
      await api.cancelReminder(id);
      await qc.invalidateQueries({ queryKey: ["today"] });
    } catch (err) {
      toast(errorText(err, "Couldn't cancel the reminder."));
    }
  }

  if (!today.data) return null;
  return (
    <section className="section" aria-labelledby="reminders">
      <h2 className="section-title" id="reminders">Your reminders</h2>
      {reminders.length === 0 ? (
        <p className="empty">None set. Ask Abdo: "remind me in 30 minutes to switch the laundry."</p>
      ) : (
        <ul className="rows">
          {reminders.map((r) => (
            <li className="row" key={r.id}>
              <span className="row-main">
                <span className="row-title">{r.text}</span>
                <span className="row-meta">
                  {relativeDay(dayOf(r.due_at))} at {clock(r.due_at)}{r.set_by ? `, from ${r.set_by}` : ""}
                </span>
              </span>
              <button className="btn btn-quiet btn-small" onClick={() => void cancel(r.id)}>Cancel</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Week() {
  const cal = useQuery({ queryKey: ["calendar"], queryFn: () => api.calendar(7), retry: 1 });
  const days = new Map<string, CalendarEvent[]>();
  for (const e of cal.data?.events ?? []) days.set(e.date, [...(days.get(e.date) ?? []), e]);
  const today = todayISO();

  return (
    <section className="section" aria-labelledby="week">
      <h2 className="section-title" id="week">This week</h2>
      <p className="section-note">To add or change something, ask Abdo in the chat.</p>
      {cal.isError && (
        <div className="notice notice-error" role="alert">
          {cal.error instanceof ApiError && cal.error.status === 503
            ? "Couldn't reach the calendar. That doesn't mean it's empty."
            : "Couldn't load the calendar."}{" "}
          <button className="btn-link" onClick={() => void cal.refetch()}>Try again</button>
        </div>
      )}
      {cal.data && !cal.data.configured && <p className="empty">The suite calendar isn't connected yet.</p>}
      {cal.data?.configured && days.size === 0 && (
        <p className="empty">Nothing on the calendar this week.</p>
      )}
      {[...days.entries()].map(([date, events]) => {
        const [weekday, num] = events[0].day.split(" ");
        return (
          <div className={`cal-day${date === today ? " cal-today" : ""}`} key={date}>
            <div className="cal-date">
              <span className="cal-num">{num}</span>
              <span className="cal-wd">{date === today ? "Today" : weekday.slice(0, 3)}</span>
            </div>
            <ul className="cal-events">
              {events.map((e) => (
                <li key={e.id}>
                  <span className="cal-time">{e.time ?? "All day"}</span>
                  <span className="row-title" style={{ display: "inline" }}>{e.summary}</span>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}
