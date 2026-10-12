import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, ApiError, type CalendarEvent, type Member } from "../api";
import { Sign } from "../components/Sign";
import { Icon } from "../components/Icon";
import { fmt } from "./Money";
import { clock, relativeDay, todayISO, dayOf } from "../lib/dates";

export function Today({ me, toast }: { me: Member; toast: (msg: string) => void }) {
  return (
    <div className="page">
      <header className="screen-head"><Sign ar="النهاردة" en="Today" /></header>
      <main className="screen">
        <BriefingCard />
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

// The morning briefing, live: the same data the 8:00 notification is built from, laid out
// as a card instead of a block of text. Statuses and prayer times have their own sections
// right below, so the card leaves them out.
function BriefingCard() {
  const b = useQuery({ queryKey: ["briefing"], queryFn: api.briefing, refetchInterval: 10 * 60_000 });
  if (b.isPending) return null;
  if (b.isError || !b.data) {
    return <p className="notice notice-error">Couldn't load your briefing.</p>;
  }
  const d = b.data;
  const w = d.weather;
  const forYou = [
    ...d.waiting.map((x) => `${x}: waiting at the building`),
    ...d.arriving.map((x) => `${x}: arriving today`),
  ];
  const quiet = !d.events.length && d.calendar_ok && !forYou.length && !d.groceries
    && !d.money.transfers.some((t) => t.cents >= 100);

  return (
    <section className="plate briefing" aria-labelledby="briefing-title">
      <div className="briefing-head">
        <p className="briefing-day">{d.day}{d.place ? `, in ${d.place}` : ""}</p>
        <h2 className="briefing-title" id="briefing-title" dir="rtl" lang="ar">
          {d.greeting} يا {d.name}
        </h2>
      </div>

      {w && (
        <div className="briefing-weather">
          <span className="briefing-icon"><Icon name={w.kind} label={w.sky || w.kind} /></span>
          <span className="briefing-temp">{w.temp}<span className="briefing-deg">°</span></span>
          <span className="briefing-sky">
            <span className="briefing-sky-main">{w.sky}</span>
            <span>
              {w.feels !== w.temp ? `Feels like ${w.feels}°. ` : ""}High {w.high}°, low {w.low}°
              {w.rain !== null ? `. ${w.rain}% chance of rain or snow` : ""}
            </span>
          </span>
        </div>
      )}

      <dl className="briefing-rows">
        {!d.calendar_ok ? (
          <div className="briefing-row">
            <dt>Today</dt>
            <dd>Couldn't check the calendar.</dd>
          </div>
        ) : d.events.length > 0 && (
          <div className="briefing-row">
            <dt>Today</dt>
            <dd>
              {d.events.map((e, i) => (
                <span key={i} className="briefing-line">
                  {e.time && <span className="briefing-time">{e.time}</span>}
                  <span className="row-title" style={{ display: "inline" }}>{e.summary}</span>
                </span>
              ))}
            </dd>
          </div>
        )}
        {forYou.length > 0 && (
          <div className="briefing-row">
            <dt>For you</dt>
            <dd>{forYou.map((x) => <span key={x} className="briefing-line">{x}</span>)}</dd>
          </div>
        )}
        {d.money.transfers.filter((t) => t.cents >= 100).length > 0 && (
          <div className="briefing-row">
            <dt>Money</dt>
            <dd>
              {d.money.transfers.filter((t) => t.cents >= 100).map((t) => (
                <span key={t.name} className="briefing-line">
                  {t.direction === "pay" ? `You owe ${t.name} ${fmt(t.cents)}` : `${t.name} owes you ${fmt(t.cents)}`}
                </span>
              ))}
              <Link to="/money" className="briefing-link">Settle up</Link>
            </dd>
          </div>
        )}
        {d.groceries > 0 && (
          <div className="briefing-row">
            <dt>Groceries</dt>
            <dd>
              {d.groceries} item{d.groceries === 1 ? "" : "s"} on the list.{" "}
              <Link to="/lists" className="briefing-link">See the list</Link>
            </dd>
          </div>
        )}
      </dl>
      {quiet && <p className="briefing-quiet">Nothing else on today.</p>}
    </section>
  );
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
  const [place, setPlace] = useState("");

  const statuses = today.data?.statuses ?? [];
  const mine = statuses.find((s) => s.member_id === me.id);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    try {
      const res = await api.setStatus(text.trim(), until || undefined, place.trim() || undefined);
      if (res.place) toast(`Travel mode: ${res.place}. Your prayer times and weather follow you.`);
      setEditing(false);
      setText("");
      setUntil("");
      setPlace("");
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
              <span className="row-title"><strong>{s.name}</strong> {s.text}{s.place ? ` (in ${s.place})` : ""}</span>
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
          <input className="field" dir="auto" value={place} onChange={(e) => setPlace(e.target.value)}
                 placeholder="Away in another city? (optional)" aria-label="City, if you're away" maxLength={80} />
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
      {prayers?.place && <p className="section-note">In {prayers.place}, while you're away</p>}
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
