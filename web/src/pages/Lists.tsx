import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Package, type Poll } from "../api";
import { Sign } from "../components/Sign";
import { relativeDay } from "../lib/dates";

const DAYS = ["Today", "Tomorrow", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function Lists({ toast }: { toast: (msg: string) => void }) {
  return (
    <div className="page">
      <header className="screen-head"><Sign ar="الحاجات" en="Lists" /></header>
      <main className="screen">
        <JumpRow />
        <Groceries toast={toast} />
        <Fridge toast={toast} />
        <Polls toast={toast} />
        <Packages toast={toast} />
      </main>
    </div>
  );
}

function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

// A long page: a row of its sections with counts, one tap to each.
function JumpRow() {
  const shopping = useQuery({ queryKey: ["shopping"], queryFn: api.shopping, refetchInterval: 15000 });
  const fridge = useQuery({ queryKey: ["fridge"], queryFn: api.fridge, refetchInterval: 60_000 });
  const polls = useQuery({ queryKey: ["polls"], queryFn: api.polls, refetchInterval: 15_000 });
  const packages = useQuery({ queryKey: ["packages"], queryFn: api.packages, refetchInterval: 15000 });
  const links = [
    { id: "groceries", label: "Groceries", n: shopping.data?.items.length },
    { id: "fridge", label: "Fridge", n: fridge.data?.items.length },
    { id: "polls", label: "Polls", n: polls.data?.polls.filter((p) => !p.closed).length },
    { id: "packages", label: "Packages", n: packages.data?.packages.length },
  ];
  return (
    <nav className="jump" aria-label="Sections">
      {links.map((l) => (
        <button key={l.id} className="jump-link"
                onClick={() => document.getElementById(l.id)?.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" })}>
          {l.label}{l.n ? <span className="jump-n">{l.n}</span> : null}
        </button>
      ))}
    </nav>
  );
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

function Groceries({ toast }: { toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["shopping"], queryFn: api.shopping, refetchInterval: 15000 });
  const [item, setItem] = useState("");
  const [qty, setQty] = useState("");

  async function add(e: FormEvent) {
    e.preventDefault();
    const value = item.trim();
    if (!value) return;
    try {
      await api.addItem(value, qty.trim() || undefined);
      setItem("");
      setQty("");
      await qc.invalidateQueries({ queryKey: ["shopping"] });
    } catch (err) {
      toast(errorText(err, "Couldn't add that. Try again."));
    }
  }

  async function bought(name: string) {
    try {
      await api.boughtItem(name);
      toast(`Marked ${name} as bought`);
      await qc.invalidateQueries({ queryKey: ["shopping"] });
    } catch (err) {
      toast(errorText(err, "Couldn't update the list. Try again."));
    }
  }

  const items = list.data?.items ?? [];
  return (
    <section className="section" aria-labelledby="groceries">
      <h2 className="section-title" id="groceries">Groceries</h2>
      <form className="inline-form" onSubmit={add}>
        <input className="field" dir="auto" value={item} onChange={(e) => setItem(e.target.value)}
               placeholder="Add an item" aria-label="Item" />
        <input className="field field-narrow" dir="auto" value={qty} onChange={(e) => setQty(e.target.value)}
               placeholder="Qty" aria-label="Amount (optional)" />
        <button className="btn" disabled={!item.trim()}>Add</button>
      </form>
      {list.isError && <p className="notice notice-error">Couldn't load the list.</p>}
      {list.data && items.length === 0 && (
        <p className="empty">Nothing to buy.</p>
      )}
      <GoingShopping toast={toast} />
      <ul className="rows">
        {items.map((i) => (
          <li className="row" key={i.id}>
            <button className="check" onClick={() => void bought(i.item)} aria-label={`Mark ${i.item} as bought`} />
            <span className="row-main">
              <span className="row-title">{i.item}</span>
              {i.qty && <span className="row-meta">{i.qty}</span>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function GoingShopping({ toast }: { toast: (msg: string) => void }) {
  const [open, setOpen] = useState(false);
  const [store, setStore] = useState("");
  async function go(e: FormEvent) {
    e.preventDefault();
    try {
      const { notified } = await api.goingShopping(store.trim() || undefined);
      toast(notified ? `Told ${notified} roommate${notified === 1 ? "" : "s"} you're going` : "Nobody else to tell");
      setOpen(false);
      setStore("");
    } catch (err) {
      toast(errorText(err, "Couldn't tell the others."));
    }
  }
  if (!open) {
    return <button className="btn btn-quiet" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>I'm going shopping</button>;
  }
  return (
    <form className="inline-form" onSubmit={go}>
      <input className="field" value={store} onChange={(e) => setStore(e.target.value)} placeholder="Where? (optional)"
             aria-label="Which store" autoFocus maxLength={60} />
      <button className="btn">Tell everyone</button>
    </form>
  );
}

function Fridge({ toast }: { toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["fridge"], queryFn: api.fridge, refetchInterval: 60_000 });
  const [item, setItem] = useState("");
  const [expires, setExpires] = useState("");
  const [mine, setMine] = useState(false);

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!item.trim()) return;
    try {
      await api.addFridge({ item: item.trim(), expires_on: expires || undefined, mine });
      setItem("");
      setExpires("");
      setMine(false);
      await qc.invalidateQueries({ queryKey: ["fridge"] });
    } catch (err) {
      toast(errorText(err, "Couldn't add that."));
    }
  }

  async function done(id: number, name: string, rebuy: boolean) {
    try {
      const r = await api.fridgeDone(id, rebuy);
      toast(rebuy ? (r.added_to_list ? `${name} added to groceries` : `${name} was already on the list`) : `${name} finished`);
      await qc.invalidateQueries({ queryKey: ["fridge"] });
      await qc.invalidateQueries({ queryKey: ["shopping"] });
    } catch (err) {
      toast(errorText(err, "Couldn't update that."));
    }
  }

  const items = q.data?.items ?? [];
  return (
    <section className="section" aria-labelledby="fridge">
      <h2 className="section-title" id="fridge">Fridge</h2>
      <form onSubmit={add} style={{ display: "grid", gap: 8, margin: "8px 0 4px" }}>
        <div className="inline-form" style={{ margin: 0 }}>
          <input className="field" dir="auto" value={item} onChange={(e) => setItem(e.target.value)}
                 placeholder="Add food" aria-label="Food item" maxLength={80} />
          <input className="field" type="date" value={expires} onChange={(e) => setExpires(e.target.value)}
                 aria-label="Expires on (optional)" style={{ flex: "0 0 148px", minWidth: 0 }} />
        </div>
        <div className="inline-form" style={{ margin: 0, alignItems: "center" }}>
          <label className="check-label">
            <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> Just mine
          </label>
          <button className="btn" disabled={!item.trim()}>Add</button>
        </div>
      </form>
      {q.data && items.length === 0 && <p className="empty">Nothing in the fridge.</p>}
      <ul className="rows">
        {items.map((f) => (
          <li className="row" key={f.id}>
            <span className="row-main">
              <span className="row-title">{f.item}</span>
              <span className="row-meta">
                {f.owner ? <span className="chip">{f.mine ? "Yours" : f.owner}</span> : <span className="chip">Shared</span>}{" "}
                {f.label && <span className={`expiry expiry-${f.state}`}>{f.state === "expired" ? f.label : `expires ${f.label}`}</span>}
              </span>
            </span>
            <button className="btn btn-quiet btn-small" onClick={() => void done(f.id, f.item, false)}>Finished</button>
            <button className="btn btn-quiet btn-small" onClick={() => void done(f.id, f.item, true)}
                    aria-label={`Finished, add ${f.item} to groceries`}>Rebuy</button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Polls({ toast }: { toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["polls"], queryFn: api.polls, refetchInterval: 15_000 });
  const [creating, setCreating] = useState(false);
  const [question, setQuestion] = useState("");
  const [options, setOptions] = useState(["", ""]);

  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      await api.createPoll(question.trim(), options.map((o) => o.trim()).filter(Boolean));
      toast("Poll sent to everyone");
      setCreating(false);
      setQuestion("");
      setOptions(["", ""]);
      await qc.invalidateQueries({ queryKey: ["polls"] });
    } catch (err) {
      toast(errorText(err, "Couldn't start the poll."));
    }
  }

  async function vote(p: Poll, i: number) {
    try {
      // Tapping your current choice again takes the vote back.
      if (p.my_vote === i) await api.unvote(p.id);
      else await api.vote(p.id, i);
      await qc.invalidateQueries({ queryKey: ["polls"] });
    } catch (err) {
      toast(errorText(err, "Couldn't vote."));
    }
  }

  async function remove(p: Poll) {
    if (!window.confirm(`Delete "${p.question}" for everyone?`)) return;
    try {
      await api.deletePoll(p.id);
      await qc.invalidateQueries({ queryKey: ["polls"] });
    } catch (err) {
      toast(errorText(err, "Couldn't delete it."));
    }
  }

  async function close(p: Poll) {
    try {
      await api.closePoll(p.id);
      await qc.invalidateQueries({ queryKey: ["polls"] });
    } catch (err) {
      toast(errorText(err, "Couldn't close it."));
    }
  }

  const polls = q.data?.polls ?? [];
  const filled = options.map((o) => o.trim()).filter(Boolean).length;
  return (
    <section className="section" aria-labelledby="polls">
      <h2 className="section-title" id="polls">Polls</h2>
      {polls.filter((p) => p.closed).map((p) => {
        // Closed polls shrink to one results line and drop off after a day.
        const top = Math.max(0, ...p.options.map((o) => o.count));
        const winners = p.options.filter((o) => top > 0 && o.count === top).map((o) => o.text);
        return (
          <div className="row" key={p.id}>
            <span className="row-main">
              <span className="row-title">{p.question}</span>
              <span className="row-meta">
                {winners.length ? `${winners.join(" / ")} won, ${top} vote${top === 1 ? "" : "s"}` : "Closed, no votes"}
              </span>
            </span>
            {p.mine && <button className="btn btn-quiet btn-small" onClick={() => void remove(p)}>Delete</button>}
          </div>
        );
      })}
      {polls.filter((p) => !p.closed).map((p) => {
        const total = p.options.reduce((a, o) => a + o.count, 0);
        return (
          <div className="poll" key={p.id}>
            <p className="poll-q">{p.question}</p>
            <ul className="poll-options">
              {p.options.map((o, i) => (
                <li key={i}>
                  <button className={`poll-option${p.my_vote === i ? " chosen" : ""}`}
                          aria-pressed={p.my_vote === i} onClick={() => void vote(p, i)}>
                    <span className="poll-bar" style={{ width: total ? `${(o.count / total) * 100}%` : 0 }} />
                    <span className="poll-label">{o.text}</span>
                    <span className="poll-count">{o.count}</span>
                  </button>
                  {o.voters.length > 0 && <span className="row-meta poll-voters">{o.voters.join(", ")}</span>}
                </li>
              ))}
            </ul>
            {p.mine && (
              <div className="inline-form" style={{ margin: 0 }}>
                <button className="btn btn-quiet btn-small" onClick={() => void close(p)}>Close poll</button>
                <button className="btn btn-quiet btn-small" onClick={() => void remove(p)}>Delete</button>
              </div>
            )}
          </div>
        );
      })}
      {creating ? (
        <form onSubmit={create} style={{ display: "grid", gap: 8 }}>
          <input className="field" dir="auto" value={question} onChange={(e) => setQuestion(e.target.value)}
                 placeholder="Where are we eating?" aria-label="Question" maxLength={140} autoFocus />
          {options.map((o, i) => (
            <input key={i} className="field" dir="auto" value={o} placeholder={`Option ${i + 1}`} aria-label={`Option ${i + 1}`}
                   maxLength={60} onChange={(e) => setOptions(options.map((x, j) => (j === i ? e.target.value : x)))} />
          ))}
          <div className="inline-form" style={{ margin: 0 }}>
            {options.length < 6 && (
              <button type="button" className="btn btn-quiet" onClick={() => setOptions([...options, ""])}>Add option</button>
            )}
            <button className="btn" disabled={!question.trim() || filled < 2}>Send poll</button>
            <button type="button" className="btn btn-quiet" onClick={() => setCreating(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <button className="btn btn-quiet" style={{ marginTop: 6 }} onClick={() => setCreating(true)}>Start a poll</button>
      )}
    </section>
  );
}

function Packages({ toast }: { toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["packages"], queryFn: api.packages, refetchInterval: 15000 });
  const [adding, setAdding] = useState(false);
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [carrier, setCarrier] = useState("");
  const [day, setDay] = useState("");

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!description.trim()) return;
    try {
      await api.addPackage({
        description: description.trim(),
        owner: owner.trim() || undefined,
        carrier: carrier.trim() || undefined,
        day: day || undefined,
      });
      setDescription(""); setOwner(""); setCarrier(""); setDay(""); setAdding(false);
      await qc.invalidateQueries({ queryKey: ["packages"] });
    } catch (err) {
      toast(errorText(err, "Couldn't add the package. Try again."));
    }
  }

  async function setStatus(p: Package, status: "arrived" | "picked_up") {
    try {
      await api.setPackageStatus(p.id, status);
      toast(status === "arrived" ? `${p.description} is at the building` : `${p.description} picked up`);
      await qc.invalidateQueries({ queryKey: ["packages"] });
    } catch (err) {
      toast(errorText(err, "Couldn't update the package. Try again."));
    }
  }

  const all = list.data?.packages ?? [];
  const arrived = all.filter((p) => p.status === "arrived");
  const expected = all.filter((p) => p.status === "expected");

  const row = (p: Package) => (
    <li className="row" key={p.id}>
      <span className="row-main">
        <span className="row-title">{p.description}{p.carrier ? ` (${p.carrier})` : ""}</span>
        <span className="row-meta">
          <span className="chip">{p.owner ?? "Someone"}</span>{" "}
          {p.status === "expected" && (p.expected_on ? relativeDay(p.expected_on) : "No date yet")}
        </span>
      </span>
      {p.status === "expected" ? (
        <button className="btn btn-quiet btn-small" onClick={() => void setStatus(p, "arrived")}>Arrived</button>
      ) : (
        <button className="btn btn-small" onClick={() => void setStatus(p, "picked_up")}>Picked up</button>
      )}
    </li>
  );

  return (
    <section className="section" aria-labelledby="packages">
      <h2 className="section-title" id="packages">Packages</h2>
      {list.isError && <p className="notice notice-error">Couldn't load packages.</p>}

      {arrived.length > 0 && (
        <>
          <p className="section-note">Waiting at the building</p>
          <ul className="rows">{arrived.map(row)}</ul>
        </>
      )}
      {expected.length > 0 && (
        <>
          <p className="section-note" style={{ marginTop: 12 }}>On the way</p>
          <ul className="rows">{expected.map(row)}</ul>
        </>
      )}
      {list.data && all.length === 0 && !adding && (
        <p className="empty">No packages on the way.</p>
      )}

      {adding ? (
        <form onSubmit={add} style={{ display: "grid", gap: 8, marginTop: 10 }}>
          <input className="field" dir="auto" value={description} onChange={(e) => setDescription(e.target.value)}
                 placeholder="What is it?" aria-label="What is it?" autoFocus />
          <div className="inline-form" style={{ margin: 0 }}>
            <input className="field" dir="auto" value={owner} onChange={(e) => setOwner(e.target.value)}
                   placeholder="For (you)" aria-label="Whose is it? Leave empty for you" />
            <input className="field" dir="auto" value={carrier} onChange={(e) => setCarrier(e.target.value)}
                   placeholder="Carrier" aria-label="Carrier (optional)" />
          </div>
          <select className="field" value={day} onChange={(e) => setDay(e.target.value)} aria-label="Expected">
            <option value="">Expected: not sure</option>
            {DAYS.map((d) => <option key={d} value={d}>Expected: {d}</option>)}
          </select>
          <div className="inline-form" style={{ margin: 0 }}>
            <button className="btn" disabled={!description.trim()}>Add package</button>
            <button type="button" className="btn btn-quiet" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <button className="btn btn-quiet" style={{ marginTop: 10 }} onClick={() => setAdding(true)}>
          Add a package
        </button>
      )}
    </section>
  );
}
