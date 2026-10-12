import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Package } from "../api";
import { Sign } from "../components/Sign";
import { relativeDay } from "../lib/dates";

const DAYS = ["Today", "Tomorrow", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function Lists({ toast }: { toast: (msg: string) => void }) {
  return (
    <div className="page">
      <header className="screen-head"><Sign ar="الحاجات" en="Lists" /></header>
      <main className="screen">
        <Groceries toast={toast} />
        <Packages toast={toast} />
      </main>
    </div>
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
        <p className="empty">Nothing to buy. Add something here, or tell Abdo.</p>
      )}
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
