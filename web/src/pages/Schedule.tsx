import { useState } from "react";
import { Link } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ProposedClass, type ProposedExam, type SavedClass } from "../api";
import { relativeDay, termLabel } from "../lib/dates";
import { Sign } from "../components/Sign";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/** Your weekly classes. Import = paste from Quest -> Abdo proposes -> you review -> save. */
export function Schedule({ toast }: { toast: (msg: string) => void }) {
  const q = useQuery({ queryKey: ["schedule"], queryFn: api.schedule });
  const [importing, setImporting] = useState(false);
  const classes = q.data?.classes ?? [];

  return (
    <div className="page">
      <header className="screen-head"><Sign ar="الجدول" en="Classes" /></header>
      <main className="screen">
        <p style={{ marginTop: 8 }}><Link to="/settings" className="btn-link">Back</Link></p>
        {importing || (q.data && classes.length === 0) ? (
          <Importer hasExisting={classes.length > 0} onDone={() => setImporting(false)} toast={toast} />
        ) : (
          <>
            <Week classes={classes} />
            <button className="btn btn-quiet" style={{ marginTop: 14 }} onClick={() => setImporting(true)}>
              Update from Quest
            </button>
          </>
        )}
      </main>
    </div>
  );
}

function Week({ classes }: { classes: SavedClass[] }) {
  const term = classes[0] ? termLabel(classes[0].term_start) : "";
  return (
    <section className="section" aria-labelledby="week-classes">
      <h2 className="section-title" id="week-classes">Your week</h2>
      {term && <p className="section-note">Term: {term}</p>}
      {DAYS.filter((d) => classes.some((c) => c.day === d)).map((d) => (
        <div className="cal-day" key={d}>
          <div className="cal-date"><span className="cal-num" style={{ fontSize: "var(--t-lead)" }}>{d}</span></div>
          <ul className="cal-events">
            {classes.filter((c) => c.day === d).map((c, i) => (
              <li key={i}>
                <span className="cal-time">{c.start}–{c.end}</span>
                <span>{c.course} {c.kind}</span>
                {c.room && <span className="row-meta" style={{ display: "inline", marginInlineStart: 8, whiteSpace: "nowrap" }}>{c.room}</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

function Importer({ hasExisting, onDone, toast }: {
  hasExisting: boolean;
  onDone: () => void;
  toast: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [proposed, setProposed] = useState<ProposedClass[] | null>(null);
  const [termStart, setTermStart] = useState("");
  const [termEnd, setTermEnd] = useState("");
  const [skipped, setSkipped] = useState<string[]>([]);
  const [exams, setExams] = useState<(ProposedExam & { include: boolean })[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function read() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.parseSchedule(text);
      setProposed(res.classes);
      setTermStart(res.term_start);
      setTermEnd(res.term_end);
      setSkipped(res.skipped);
      setExams(res.exams.map((x) => ({ ...x, include: true })));
    } catch (err) {
      setError(errorText(err, "Couldn't read that schedule."));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!proposed) return;
    setBusy(true);
    setError(null);
    try {
      const chosen = exams.filter((x) => x.include).map(({ include: _, ...x }) => x);
      const res = await api.saveSchedule(proposed, termStart, termEnd, chosen);
      toast(`Saved ${res.saved} class meetings` + (res.exams_added ? ` and ${res.exams_added} exam${res.exams_added === 1 ? "" : "s"}` : ""));
      await qc.invalidateQueries({ queryKey: ["schedule"] });
      await qc.invalidateQueries({ queryKey: ["today"] });
      await qc.invalidateQueries({ queryKey: ["exams"] });
      onDone();
    } catch (err) {
      setError(errorText(err, "Couldn't save the schedule."));
    } finally {
      setBusy(false);
    }
  }

  if (!proposed) {
    return (
      <section className="section" aria-labelledby="import">
        <h2 className="section-title" id="import">{hasExisting ? "Update your classes" : "Add your classes"}</h2>
        <ol className="steps">
          <li>In Quest, open <strong>Class Schedule</strong>, pick the term, and choose <strong>List View</strong>.</li>
          <li>Select everything on the page, copy it, and paste it below.</li>
          <li>Check what Abdo found, then save.</li>
        </ol>
        <textarea className="field paste" value={text} onChange={(e) => setText(e.target.value)}
                  placeholder="Paste your Quest schedule here" aria-label="Your schedule from Quest" rows={8} />
        <div className="inline-form">
          <button className="btn" disabled={busy || text.trim().length < 20} onClick={() => void read()}>
            {busy ? "Reading…" : "Read my schedule"}
          </button>
          {hasExisting && <button className="btn btn-quiet" onClick={onDone}>Cancel</button>}
        </div>
        {error && <p className="notice notice-error" role="alert">{error}</p>}
      </section>
    );
  }

  return (
    <section className="section" aria-labelledby="review">
      <h2 className="section-title" id="review">Check before saving</h2>
      <p className="section-note">Saving replaces your current classes.</p>
      {skipped.length > 0 && <p className="notice">Left out: {skipped.join("; ")}.</p>}
      <div className="term-row">
        <label className="row-meta" htmlFor="ts">Term starts</label>
        <label className="row-meta" htmlFor="te">Term ends</label>
        <input id="ts" className="field" type="date" value={termStart} onChange={(e) => setTermStart(e.target.value)} />
        <input id="te" className="field" type="date" value={termEnd} onChange={(e) => setTermEnd(e.target.value)} />
      </div>
      <ul className="rows">
        {proposed.map((c, i) => (
          <li className="row" key={i}>
            <span className="row-main">
              <span className="row-title">{c.course.toUpperCase()} {c.kind.toUpperCase()} {c.section && <span className="row-meta" style={{ display: "inline" }}>{c.section}</span>}</span>
              <span className="row-meta">{c.days.join(", ")} {c.start}–{c.end}{c.room ? `, ${c.room}` : ""}</span>
            </span>
            <button className="btn btn-quiet btn-small" aria-label={`Remove ${c.course} ${c.kind}`}
                    onClick={() => setProposed(proposed.filter((_, j) => j !== i))}>Remove</button>
          </li>
        ))}
      </ul>
      {exams.length > 0 && (
        <>
          <h3 className="section-title" style={{ marginTop: 16, fontSize: "var(--t-body)" }}>Exams found</h3>
          <ul className="rows">
            {exams.map((x, i) => (
              <li className="row" key={`${x.course}-${x.date}`}>
                <input type="checkbox" checked={x.include} aria-label={`Add ${x.course} on ${x.date}`}
                       style={{ width: 22, height: 22 }}
                       onChange={(e) => setExams(exams.map((y, j) => (j === i ? { ...y, include: e.target.checked } : y)))} />
                <span className="row-main">
                  <span className="row-title">{x.course}</span>
                  <span className="row-meta">{relativeDay(x.date)}, {x.start}–{x.end}</span>
                </span>
                <select className="field" value={x.kind} aria-label={`Kind of test for ${x.course}`}
                        style={{ width: 112, minHeight: 36, padding: "4px 8px" }}
                        onChange={(e) => setExams(exams.map((y, j) => (j === i ? { ...y, kind: e.target.value as ProposedExam["kind"] } : y)))}>
                  <option value="quiz">Quiz</option>
                  <option value="midterm">Midterm</option>
                  <option value="final">Final</option>
                </select>
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="inline-form">
        <button className="btn" disabled={busy || !proposed.length} onClick={() => void save()}>
          {busy ? "Saving…" : "Save schedule"}
        </button>
        <button className="btn btn-quiet" onClick={() => { setProposed(null); setError(null); }}>Start over</button>
      </div>
      {error && <p className="notice notice-error" role="alert">{error}</p>}
    </section>
  );
}
