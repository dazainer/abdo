import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, type BusDeparture } from "../api";
import { Sign } from "../components/Sign";
import { shortClock } from "../lib/dates";

// The suite's saved trips, each with its next departures from GRT. Route numbers sit on
// small enamel plates like the signs at a stop. Fetched only while this screen is open.
export function Buses() {
  const q = useQuery({ queryKey: ["buses"], queryFn: () => api.buses(), refetchInterval: 30_000 });
  const trips = q.data?.trips ?? [];
  const anyLive = trips.some((t) => t.departures.some((d) => d.live));

  return (
    <div className="page">
      <header className="screen-head">
        <Link to="/" className="head-back" aria-label="Back to Today">Today</Link>
        <Sign ar="الباص" en="Buses" />
      </header>
      <main className="screen">
        {q.isPending && <p className="row-meta" style={{ marginTop: 16 }}>Checking GRT…</p>}
        {q.isError && <p className="notice notice-error">Couldn't reach GRT. Try again in a minute.</p>}
        {q.data && trips.length === 0 && (
          <p className="empty">No trips saved yet.</p>
        )}
        {trips.map((t) => (
          <section className="section panel" key={t.name} aria-label={t.name}>
            <h2 className="section-title">{t.name}</h2>
            {!t.ok ? <p className="row-meta">Couldn't reach GRT for this trip.</p>
              : t.departures.length === 0 ? <p className="row-meta">Nothing in the next couple of hours.</p>
              : (
                <ul className="rows">
                  {t.departures.map((d, i) => <Departure key={i} d={d} />)}
                </ul>
              )}
          </section>
        ))}
        {q.data && trips.length > 0 && (
          <p className="section-note" style={{ marginTop: 14 }}>
            From GRT{anyLive ? ", live where marked" : ". Scheduled times: GRT's live tracking is off right now"}.
          </p>
        )}
      </main>
    </div>
  );
}

function Departure({ d }: { d: BusDeparture }) {
  return (
    <li className="row departure">
      <span className="route-plate plate" aria-label={`Route ${d.route}`}>{d.route}</span>
      <span className="row-main">
        <span className="row-title">
          {d.headsign}
          {d.live && <span className="live">{d.delay_min > 0 ? ` ${d.delay_min} min late` : " live"}</span>}
        </span>
        <span className="row-meta">
          {d.stop && `From ${d.stop}`}
          {d.then_route && `. Stay on as it becomes the ${d.then_route}`}
          {d.arrive && `. Arrives ${shortClock(d.arrive)}${d.arrive_stop ? ` at ${d.arrive_stop}` : ""}`}
        </span>
      </span>
      <span className="departure-when">
        <span className="departure-in">{d.in_min} min</span>
        <span className="row-meta">{shortClock(d.at)}</span>
      </span>
    </li>
  );
}
