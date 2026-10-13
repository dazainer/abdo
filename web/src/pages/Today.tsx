import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, ApiError, type Briefing, type CalendarEvent, type Exam, type Member, type Reminder, type Today as TodayData } from "../api";
import { Sign } from "../components/Sign";
import { Icon, type IconName } from "../components/Icon";
import { fmt } from "./Money";
import { clock, dayOf, relativeDay, todayISO } from "../lib/dates";

// Today is the home screen: the briefing plate (greeting, weather, the next twelve hours),
// the few things that need you, then the rest of the day as stops on one line, like a bus
// route. Every fact appears once: what's on today is on the line, what's later is under
// "Coming up".

export function Today({ me, toast }: { me: Member; toast: (msg: string) => void }) {
  const today = useQuery({ queryKey: ["today"], queryFn: api.today, refetchInterval: 60_000 });
  const brief = useQuery({ queryKey: ["briefing"], queryFn: api.briefing, refetchInterval: 10 * 60_000 });
  const exams = useQuery({ queryKey: ["exams"], queryFn: api.exams, refetchInterval: 5 * 60_000 });

  return (
    <div className="page">
      <header className="screen-head">
        <Sign ar="النهاردة" en="Today" />
        <Link to="/buses" className="head-action" aria-label="Buses"><Icon name="bus" /></Link>
      </header>
      <main className="screen">
        <BriefingPlate q={brief} />
        {brief.data && <NeedsYou b={brief.data} />}
        <DayLine me={me} today={today.data} brief={brief.data} exams={exams.data?.exams ?? []} toast={toast} />
        {today.isError && <p className="notice notice-error">Couldn't load today.</p>}
        <Around me={me} today={today.data} toast={toast} />
        <ComingUp me={me} exams={exams.data?.exams ?? []} reminders={today.data?.reminders ?? []} toast={toast} />
      </main>
    </div>
  );
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// ---- the briefing plate ------------------------------------------------------------

function skyIcon(kind: string, night: boolean): IconName {
  if (kind === "clear" && night) return "moon";
  return kind as IconName;
}

/** "3 PM" on the first hour and at noon/midnight, bare "4" after: eight hours fit a phone. */
function hourLabel(hhmm: string, first: boolean): string {
  const h = Number(hhmm.slice(0, 2));
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return first || h % 12 === 0 ? `${h12} ${h < 12 ? "AM" : "PM"}` : String(h12);
}

function BriefingPlate({ q }: { q: { data?: Briefing; isPending: boolean; isError: boolean } }) {
  if (q.isPending) return <section className="plate briefing briefing-loading" aria-busy="true" />;
  if (q.isError || !q.data) return <p className="notice notice-error">Couldn't load your briefing.</p>;
  const d = q.data;
  const w = d.weather;
  return (
    <section className="plate briefing" aria-labelledby="briefing-title">
      <p className="briefing-day">{d.day}{d.place ? `, in ${d.place}` : ""}</p>
      <h2 className="briefing-title" id="briefing-title" dir="rtl" lang="ar">
        {d.greeting_main}{d.greeting_named ? ` يا ${d.name}` : ""}
        {d.greeting_tail && <span className="briefing-tail">{d.greeting_tail}</span>}
      </h2>
      {w && (
        <>
          <div className="briefing-weather">
            <span className="briefing-icon"><Icon name={skyIcon(w.kind, w.night)} label={w.sky || w.kind} /></span>
            <span className="briefing-temp">{w.temp}<span className="briefing-deg">°</span></span>
            <span className="briefing-sky">
              <span className="briefing-sky-main">{w.sky}</span>
              <span>
                {w.feels !== w.temp ? `Feels like ${w.feels}°. ` : ""}High {w.high}°, low {w.low}°
                {w.rain !== null ? `. ${w.rain}% chance of rain or snow` : ""}
              </span>
            </span>
          </div>
          {w.hours.length > 0 && (
            <ol className="hours" aria-label="Next 12 hours">
              {w.hours.map((h, i) => (
                <li key={h.hour} className="hour">
                  <span className="hour-at">{hourLabel(h.hour, i === 0)}</span>
                  <Icon name={skyIcon(h.kind, h.night)} label={h.kind} />
                  <span className="hour-temp">{h.temp}°</span>
                  <span className="hour-rain">{h.rain >= 30 ? `${h.rain}%` : " "}</span>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}

// ---- what needs you: one tap from each fact to where you act on it ------------------

function NeedsYou({ b }: { b: Briefing }) {
  const chips: { key: string; text: string; to: string }[] = [
    ...b.waiting.map((x) => ({ key: `w-${x}`, text: `${x} is waiting at the building`, to: "/lists" })),
    ...b.arriving.map((x) => ({ key: `a-${x}`, text: `${x} arrives today`, to: "/lists" })),
    ...b.money.transfers.filter((t) => t.cents >= 100).map((t) => ({
      key: `m-${t.name}`,
      text: t.direction === "pay" ? `You owe ${t.name} ${fmt(t.cents)}` : `${t.name} owes you ${fmt(t.cents)}`,
      to: "/money",
    })),
    ...b.fridge.map((f) => ({
      key: `f-${f.item}`, text: f.state === "expired" ? `${f.item}: ${f.label}` : `${f.item}: use ${f.label}`, to: "/lists",
    })),
    ...b.polls.map((p) => ({ key: `p-${p.id}`, text: `Vote: ${p.question}`, to: "/lists" })),
  ];
  if (!chips.length) return null;
  return (
    <nav className="needs" aria-label="Needs you">
      {chips.map((c) => (
        <Link key={c.key} to={c.to} className="need">{c.text.charAt(0).toUpperCase() + c.text.slice(1)}</Link>
      ))}
    </nav>
  );
}

// ---- the day as a line --------------------------------------------------------------

type Stop = {
  key: string;
  at: number | null;            // ms; null = all day
  kind: "prayer" | "sunrise" | "class" | "event" | "exam" | "reminder";
  title: ReactNode;
  sub?: string;
  next?: boolean;
  action?: ReactNode;
};

function at(dateISO: string, hhmm: string): number {
  const [y, m, d] = dateISO.split("-").map(Number);
  const [h, min] = hhmm.split(":").map(Number);
  return new Date(y, m - 1, d, h, min).getTime();
}

function timeParts(ms: number): { t: string; ap: string } {
  const [t, ap] = clock(new Date(ms).toISOString()).split(" ");
  return { t, ap: (ap ?? "").replace(/\./g, "") };
}

/** One clock format for the whole screen: "4:31 am", "8 am" on the hour. */
function short(ms: number): string {
  const { t, ap } = timeParts(ms);
  return `${t.replace(/:00$/, "")} ${ap}`;
}

/** "08:00"-"18:00" today -> "8 am-6 pm" (hyphen: no dashes as separators). */
function span(from: string, to: string): string {
  const day = todayISO();
  return `${short(at(day, from))}-${short(at(day, to))}`;
}

function DayLine({ me, today, brief, exams, toast }: {
  me: Member;
  today?: TodayData;
  brief?: Briefing;
  exams: Exam[];
  toast: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [showEarlier, setShowEarlier] = useState(false);
  const now = Date.now();
  const day = todayISO();

  const stops = useMemo(() => {
    async function cancelReminder(r: Reminder) {
      try {
        await api.cancelReminder(r.id);
        await qc.invalidateQueries({ queryKey: ["today"] });
      } catch (err) {
        toast(errorText(err, "Couldn't cancel the reminder."));
      }
    }
    const out: Stop[] = [];
    for (const p of today?.prayers?.times ?? []) {
      out.push({
        key: `p-${p.name}`, at: new Date(p.at).getTime(), kind: p.name === "sunrise" ? "sunrise" : "prayer",
        title: <span dir="ltr"><bdi lang="ar" className="stop-ar">{p.ar}</bdi> <span className="stop-en">{p.en}</span></span>,
        next: p.name === today?.prayers?.next,
      });
    }
    for (const c of brief?.classes ?? []) {
      out.push({ key: `c-${c.from}-${c.what}`, at: at(day, c.from), kind: "class", title: c.what, sub: `Until ${short(at(day, c.to))}` });
    }
    for (const e of brief?.events ?? []) {
      out.push({ key: `e-${e.summary}-${e.time}`, at: e.time ? at(day, e.time) : null, kind: "event", title: e.summary });
    }
    for (const x of exams.filter((x) => dayOf(x.starts_at) === day)) {
      out.push({
        key: `x-${x.id}`, at: new Date(x.starts_at).getTime(), kind: "exam",
        title: <><strong>{x.member_id === me.id ? "Your" : `${x.name}:`}</strong> {x.course} {x.kind}</>,
        sub: x.room ?? undefined,
      });
    }
    for (const r of (today?.reminders ?? []).filter((r) => dayOf(r.due_at) === day)) {
      out.push({
        key: `r-${r.id}`, at: new Date(r.due_at).getTime(), kind: "reminder", title: r.text,
        sub: r.set_by ? `Reminder from ${r.set_by}` : "Reminder",
        action: <button className="btn btn-quiet btn-small" onClick={() => void cancelReminder(r)}>Cancel</button>,
      });
    }
    return out.sort((a, b) => (a.at ?? 0) - (b.at ?? 0));
  }, [today, brief, exams, day, me.id, qc, toast]);

  if (!today && !brief) return null;
  const allDay = stops.filter((s) => s.at === null);
  const earlier = stops.filter((s) => s.at !== null && s.at < now);
  const later = stops.filter((s) => s.at !== null && s.at >= now);
  const nowT = timeParts(now);

  return (
    <section className="section" aria-labelledby="the-day">
      <h2 className="visually-hidden" id="the-day">The rest of today</h2>
      <ol className="line">
        {allDay.map((s) => <StopRow key={s.key} s={s} />)}
        {earlier.length > 0 && (
          <li className="stop stop-fold">
            <span className="stop-time" />
            <button className="btn-link" onClick={() => setShowEarlier(!showEarlier)} aria-expanded={showEarlier}>
              {showEarlier ? "Hide earlier" : `Earlier today (${earlier.length})`}
            </button>
          </li>
        )}
        {showEarlier && earlier.map((s) => <StopRow key={s.key} s={s} past />)}
        <li className="now-mark">
          <span className="now-label">Now {nowT.t} <small>{nowT.ap}</small></span>
        </li>
        {later.map((s) => <StopRow key={s.key} s={s} />)}
        {later.length === 0 && (
          <li className="stop stop-end"><span className="stop-time" /><span className="row-meta">Nothing else today</span></li>
        )}
      </ol>
    </section>
  );
}

/** "4:09 pm", the am/pm set small. */
function Time({ ms }: { ms: number | null }) {
  if (ms === null) return <>All day</>;
  const t = timeParts(ms);
  return <>{t.t}<small> {t.ap}</small></>;
}

function StopRow({ s, past = false }: { s: Stop; past?: boolean }) {
  return (
    <li className={`stop stop-${s.kind}${s.next ? " stop-next" : ""}${past ? " stop-past" : ""}`}>
      <span className="stop-time"><Time ms={s.at} /></span>
      <span className="stop-main">
        <span className="row-title">{s.title}</span>
        {s.sub && <span className="row-meta">{s.sub}</span>}
      </span>
      {s.action}
    </li>
  );
}

// ---- around: statuses and free time ----------------------------------------------

function untilLabel(iso: string | null): string {
  if (!iso) return "";
  const day = relativeDay(dayOf(iso));
  return `Until ${day === "Today" ? "" : `${day} `}${short(new Date(iso).getTime())}`;
}

function Around({ me, today, toast }: { me: Member; today?: TodayData; toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [until, setUntil] = useState("");
  const [place, setPlace] = useState("");
  if (!today) return null;

  const statuses = today.statuses;
  const mine = statuses.find((s) => s.member_id === me.id);
  const f = today.free;
  const windows = f.together.map((w) => span(w.from, w.to)).join(", ");
  const skip = [...f.away.map((n) => `${n} (away)`),
    ...f.no_timetable.filter((n) => !f.away.includes(n)).map((n) => `${n} (no classes added)`)];

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    try {
      const res = await api.setStatus(text.trim(), until || undefined, place.trim() || undefined);
      if (res.place) toast(`Travel mode: ${res.place}`);
      setEditing(false);
      setText(""); setUntil(""); setPlace("");
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
    <section className="section panel" aria-labelledby="around">
      <h2 className="section-title" id="around">Around</h2>
      <p className="free">
        <span className="free-label">Free together</span>
        <span className="free-line">{windows || "Not today"}</span>
      </p>
      {skip.length > 0 && <p className="section-note">Without {skip.join(", ")}</p>}
      <ul className="rows">
        {statuses.map((s) => (
          <li className="row" key={s.member_id}>
            <span className="row-main">
              <span className="row-title"><strong>{s.member_id === me.id ? "You" : s.name}</strong> {s.text}{s.place ? ` (in ${s.place})` : ""}</span>
              {s.until && <span className="row-meta">{untilLabel(s.until)}</span>}
            </span>
            {s.member_id === me.id && <button className="btn btn-quiet btn-small" onClick={() => void clear()}>Clear</button>}
          </li>
        ))}
      </ul>
      {editing ? (
        <form onSubmit={save} className="stack-form">
          <input className="field" dir="auto" value={text} onChange={(e) => setText(e.target.value)}
                 placeholder="In a midterm, away, studying…" aria-label="Your status" autoFocus maxLength={80} />
          <input className="field" dir="auto" value={place} onChange={(e) => setPlace(e.target.value)}
                 placeholder="City, if you're away" aria-label="City, if you're away" maxLength={80} />
          <label className="row-meta" htmlFor="until">Until</label>
          <input id="until" className="field" type="time" value={until} onChange={(e) => setUntil(e.target.value)} />
          <div className="inline-form">
            <button className="btn" disabled={!text.trim()}>Set status</button>
            <button type="button" className="btn btn-quiet" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <button className="btn btn-quiet section-action" onClick={() => setEditing(true)}>
          {mine ? "Change my status" : "Set my status"}
        </button>
      )}
    </section>
  );
}

// ---- coming up: exams, reminders and calendar after today, by day ------------------

type Upcoming = {
  key: string; day: string; sortTime: string; at: number | null; icon: IconName;
  title: ReactNode; sub?: string; action?: ReactNode;
};

function hhmm(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function ComingUp({ me, exams, reminders, toast }: {
  me: Member;
  exams: Exam[];
  reminders: Reminder[];
  toast: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const cal = useQuery({ queryKey: ["calendar"], queryFn: () => api.calendar(7), retry: 1 });
  const [adding, setAdding] = useState(false);
  const day = todayISO();

  async function removeExam(id: number) {
    try {
      await api.deleteExam(id);
      await qc.invalidateQueries({ queryKey: ["exams"] });
    } catch (err) {
      toast(errorText(err, "Couldn't remove it."));
    }
  }
  async function cancelReminder(id: number) {
    try {
      await api.cancelReminder(id);
      await qc.invalidateQueries({ queryKey: ["today"] });
    } catch (err) {
      toast(errorText(err, "Couldn't cancel the reminder."));
    }
  }

  const items: Upcoming[] = [
    ...exams.filter((x) => dayOf(x.starts_at) > day).map((x) => ({
      key: `x-${x.id}`, day: dayOf(x.starts_at), sortTime: hhmm(x.starts_at), at: new Date(x.starts_at).getTime(), icon: "pencil" as IconName,
      title: <><strong>{x.member_id === me.id ? "Your" : `${x.name}:`}</strong> {x.course} {x.kind}</>,
      sub: x.room ?? undefined,
      action: x.can_delete ? <button className="btn btn-quiet btn-small" onClick={() => void removeExam(x.id)}>Remove</button> : undefined,
    })),
    ...reminders.filter((r) => dayOf(r.due_at) > day).map((r) => ({
      key: `r-${r.id}`, day: dayOf(r.due_at), sortTime: hhmm(r.due_at), at: new Date(r.due_at).getTime(), icon: "bell" as IconName, title: r.text,
      sub: r.set_by ? `Reminder from ${r.set_by}` : "Reminder",
      action: <button className="btn btn-quiet btn-small" onClick={() => void cancelReminder(r.id)}>Cancel</button>,
    })),
    ...(cal.data?.events ?? []).filter((e: CalendarEvent) => e.date > day).map((e) => ({
      key: `e-${e.id}`, day: e.date, sortTime: e.time ?? "00:00", at: e.time ? at(e.date, e.time) : null, icon: "calendar" as IconName,
      title: e.summary,
    })),
  ].sort((a, b) => (a.day + a.sortTime).localeCompare(b.day + b.sortTime));

  const days = [...new Set(items.map((i) => i.day))];
  return (
    <section className="section panel" aria-labelledby="coming-up">
      <h2 className="section-title" id="coming-up">Coming up</h2>
      {cal.isError && (
        <p className="notice notice-error" role="alert">
          {cal.error instanceof ApiError && cal.error.status === 503
            ? "Couldn't reach the calendar. That doesn't mean it's empty."
            : "Couldn't load the calendar."}{" "}
          <button className="btn-link" onClick={() => void cal.refetch()}>Try again</button>
        </p>
      )}
      {cal.isFetched && !items.length && <p className="empty">Nothing in the next week.</p>}
      {days.map((d) => (
        <div className="up-day" key={d}>
          <h3 className="up-date">{relativeDay(d)}</h3>
          <ul className="rows">
            {items.filter((i) => i.day === d).map((i) => (
              <li className="row" key={i.key}>
                <span className={`up-icon up-${i.icon}`}><Icon name={i.icon} /></span>
                <span className="up-time"><Time ms={i.at} /></span>
                <span className="row-main">
                  <span className="row-title">{i.title}</span>
                  {i.sub && <span className="row-meta">{i.sub}</span>}
                </span>
                {i.action}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {adding ? <AddExam onDone={() => setAdding(false)} toast={toast} /> : (
        <button className="btn btn-quiet section-action" onClick={() => setAdding(true)}>Add an exam</button>
      )}
    </section>
  );
}

function AddExam({ onDone, toast }: { onDone: () => void; toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const [course, setCourse] = useState("");
  const [kind, setKind] = useState("midterm");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");

  async function add(e: FormEvent) {
    e.preventDefault();
    try {
      await api.addExam({ course: course.trim(), kind, date, time });
      await qc.invalidateQueries({ queryKey: ["exams"] });
      onDone();
    } catch (err) {
      toast(errorText(err, "Couldn't add the exam."));
    }
  }

  return (
    <form onSubmit={add} className="stack-form">
      <div className="inline-form">
        <input className="field" value={course} onChange={(e) => setCourse(e.target.value)} placeholder="Course"
               aria-label="Course" autoFocus maxLength={40} />
        <select className="field field-kind" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
          <option value="quiz">Quiz</option>
          <option value="midterm">Midterm</option>
          <option value="final">Final</option>
        </select>
      </div>
      <div className="inline-form">
        <input className="field" type="date" value={date} onChange={(e) => setDate(e.target.value)} aria-label="Date" />
        <input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} aria-label="Start time" />
      </div>
      <div className="inline-form">
        <button className="btn" disabled={!course.trim() || !date || !time}>Add exam</button>
        <button type="button" className="btn btn-quiet" onClick={onDone}>Cancel</button>
      </div>
    </form>
  );
}
