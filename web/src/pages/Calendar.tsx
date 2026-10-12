import { useQuery } from "@tanstack/react-query";
import { api, ApiError, type CalendarEvent } from "../api";
import { Sign } from "../components/Sign";
import { todayISO } from "../lib/dates";

export function Calendar() {
  const cal = useQuery({ queryKey: ["calendar"], queryFn: () => api.calendar(7), retry: 1 });

  const days = new Map<string, CalendarEvent[]>();
  for (const e of cal.data?.events ?? []) {
    days.set(e.date, [...(days.get(e.date) ?? []), e]);
  }
  const today = todayISO();

  return (
    <div className="page">
      <header className="screen-head"><Sign ar="المواعيد" en="Calendar" /></header>
      <main className="screen">
        <p className="section-note" style={{ marginTop: 8 }}>
          The next 7 days on the suite calendar. To add or change something, ask Abdo in the chat.
        </p>

        {cal.isPending && <p className="empty">Loading…</p>}

        {cal.isError && (
          <div className="notice notice-error" role="alert">
            {cal.error instanceof ApiError && cal.error.status === 503
              ? "Couldn't reach the calendar. That doesn't mean it's empty."
              : "Couldn't load the calendar."}{" "}
            <button className="btn-link" onClick={() => void cal.refetch()}>Try again</button>
          </div>
        )}

        {cal.data && !cal.data.configured && (
          <p className="empty">The suite calendar isn't connected yet.</p>
        )}

        {cal.data?.configured && days.size === 0 && (
          <p className="empty">Nothing on the calendar this week. Ask Abdo to add something.</p>
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
      </main>
    </div>
  );
}
