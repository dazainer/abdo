import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, ApiError, type CalendarEvent, type Exam } from "../api";
import { Sign } from "../components/Sign";
import { dayOf, relativeDay, shortClock, todayISO } from "../lib/dates";

// The suite calendar at a glance: five weeks from this Monday, a dot under each day with
// something on (gold for an exam), and the chosen day's list underneath. Read-only:
// changes go through Abdo, who confirms them.

const WEEKS = 5;
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function Calendar() {
  const cal = useQuery({ queryKey: ["calendar", 31], queryFn: () => api.calendar(31), retry: 1 });
  const exams = useQuery({ queryKey: ["exams"], queryFn: api.exams });
  const today = todayISO();
  const [picked, setPicked] = useState(today);

  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  const days = Array.from({ length: WEEKS * 7 }, (_, i) =>
    iso(new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + i)));

  const events = new Map<string, CalendarEvent[]>();
  for (const e of cal.data?.events ?? []) events.set(e.date, [...(events.get(e.date) ?? []), e]);
  const tests = new Map<string, Exam[]>();
  for (const x of exams.data?.exams ?? []) {
    const d = dayOf(x.starts_at);
    tests.set(d, [...(tests.get(d) ?? []), x]);
  }

  const dayEvents = events.get(picked) ?? [];
  const dayExams = tests.get(picked) ?? [];

  return (
    <div className="page">
      <header className="screen-head">
        <Link to="/" className="head-back" aria-label="Back to Today">Today</Link>
        <Sign ar="التقويم" en="Calendar" />
      </header>
      <main className="screen">
        {cal.isError && (
          <p className="notice notice-error" role="alert">
            {cal.error instanceof ApiError && cal.error.status === 503
              ? "Couldn't reach the calendar. That doesn't mean it's empty."
              : "Couldn't load the calendar."}{" "}
            <button className="btn-link" onClick={() => void cal.refetch()}>Try again</button>
          </p>
        )}
        {cal.data && !cal.data.configured && <p className="empty">The suite calendar isn't connected yet.</p>}

        <section className="panel month" aria-label="Next five weeks">
          <div className="month-grid" role="grid">
            {WD.map((w) => <span key={w} className="month-wd" role="columnheader">{w}</span>)}
            {days.map((d) => {
              const n = Number(d.slice(8));
              const hasEvent = events.has(d);
              const hasExam = tests.has(d);
              return (
                <button
                  key={d}
                  role="gridcell"
                  className={`month-day${d === today ? " is-today" : ""}${d === picked ? " is-picked" : ""}${d < today ? " is-past" : ""}`}
                  aria-label={`${relativeDay(d)}${hasEvent ? ", has events" : ""}${hasExam ? ", has an exam" : ""}`}
                  aria-pressed={d === picked}
                  onClick={() => setPicked(d)}
                >
                  <span className="month-num">{n === 1 ? new Date(`${d}T12:00`).toLocaleDateString("en-CA", { month: "short" }) : n}</span>
                  <span className="month-dots">
                    {hasEvent && <i className="dot" />}
                    {hasExam && <i className="dot dot-exam" />}
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        <section className="section panel" aria-labelledby="picked-day">
          <h2 className="section-title" id="picked-day">{relativeDay(picked)}</h2>
          {!dayEvents.length && !dayExams.length && <p className="empty">Nothing on.</p>}
          <ul className="rows">
            {dayExams.map((x) => (
              <li className="row" key={`x${x.id}`}>
                <span className="up-time">{shortClock(x.starts_at)}</span>
                <span className="row-main">
                  <span className="row-title"><strong className="exam-who">{x.name}:</strong> {x.course} {x.kind}</span>
                  {x.room && <span className="row-meta">{x.room}</span>}
                </span>
              </li>
            ))}
            {dayEvents.map((e) => (
              <li className="row" key={e.id}>
                <span className="up-time">{e.time ? shortClock(`${e.date}T${e.time}`) : "All day"}</span>
                <span className="row-main"><span className="row-title">{e.summary}</span></span>
              </li>
            ))}
          </ul>
          <Link to="/chat" className="btn btn-quiet section-action" style={{ textDecoration: "none" }}>
            Ask Abdo to add or change something
          </Link>
        </section>
      </main>
    </div>
  );
}
