import { useMemo, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type MoneyOverview } from "../api";
import { Sign } from "../components/Sign";
import { relativeDay } from "../lib/dates";

export function fmt(cents: number): string {
  const a = Math.abs(cents);
  return `$${Math.floor(a / 100).toLocaleString("en-CA")}.${String(a % 100).padStart(2, "0")}`;
}

/** Same rule as the server (preview only; the server's split is the one recorded). */
export function previewSplit(cents: number, ids: number[]): Map<number, number> {
  const sorted = [...new Set(ids)].sort((a, b) => a - b);
  const out = new Map<number, number>();
  if (!sorted.length || cents <= 0) return out;
  const base = Math.floor(cents / sorted.length);
  const extra = cents % sorted.length;
  sorted.forEach((id, i) => out.set(id, base + (i < extra ? 1 : 0)));
  return out;
}

export function toCents(raw: string): number | null {
  const m = raw.trim().replace(/[$,\s]/g, "").match(/^(\d+)(?:\.(\d{1,2}))?$/);
  if (!m) return null;
  const cents = Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0"));
  return cents > 0 ? cents : null;
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export function Money({ toast }: { toast: (msg: string) => void }) {
  const q = useQuery({ queryKey: ["money"], queryFn: api.money, refetchInterval: 30_000 });
  return (
    <div className="page">
      <header className="screen-head"><Sign ar="الفلوس" en="Money" /></header>
      <main className="screen">
        {q.isError && <p className="notice notice-error">Couldn't load the money.</p>}
        {q.data && (
          <>
            <Summary data={q.data} toast={toast} />
            <AddExpense data={q.data} toast={toast} />
            <Recent data={q.data} toast={toast} />
          </>
        )}
      </main>
    </div>
  );
}

function Summary({ data, toast }: { data: MoneyOverview; toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const net = data.balances.find((b) => b.member_id === data.me)?.net_cents ?? 0;
  const mine = data.transfers.filter((t) => t.from_member === data.me || t.to_member === data.me);
  const others = data.transfers.filter((t) => t.from_member !== data.me && t.to_member !== data.me);

  async function markPaid(t: MoneyOverview["transfers"][number]) {
    const who = t.from_member === data.me ? `you paid ${t.to_name}` : `${t.from_name} paid you`;
    if (!window.confirm(`Record that ${who} ${fmt(t.cents)}?`)) return;
    try {
      await api.addSettlement({ from_member: t.from_member, to_member: t.to_member, amount: (t.cents / 100).toFixed(2) });
      toast("Payment recorded");
      await qc.invalidateQueries({ queryKey: ["money"] });
    } catch (err) {
      toast(errorText(err, "Couldn't record the payment."));
    }
  }

  return (
    <section className="plate money-summary" aria-labelledby="money-net">
      <p className="money-label" id="money-net">
        {net > 0 ? "You're owed" : net < 0 ? "You owe" : "You're all square"}
      </p>
      {net !== 0 && <p className="money-big">{fmt(net)}</p>}
      {mine.length > 0 && (
        <ul className="money-transfers">
          {mine.map((t) => (
            <li key={`${t.from_member}-${t.to_member}`}>
              <span>
                {t.from_member === data.me ? <>Pay <strong>{t.to_name}</strong></> : <><strong>{t.from_name}</strong> pays you</>}{" "}
                {fmt(t.cents)}
              </span>
              <button className="btn btn-small money-paid" onClick={() => void markPaid(t)}>Mark paid</button>
            </li>
          ))}
        </ul>
      )}
      {others.length > 0 && (
        <p className="money-others">
          Also in the suite: {others.map((t) => `${t.from_name} pays ${t.to_name} ${fmt(t.cents)}`).join("; ")}
        </p>
      )}
    </section>
  );
}

function AddExpense({ data, toast }: { data: MoneyOverview; toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [description, setDescription] = useState("");
  const [paidBy, setPaidBy] = useState(data.me);
  const [among, setAmong] = useState<number[]>(data.members.map((m) => m.id));
  const [busy, setBusy] = useState(false);

  const cents = toCents(amount);
  const preview = useMemo(() => (cents ? previewSplit(cents, among) : new Map()), [cents, among]);
  const shares = [...new Set(preview.values())];

  function toggle(id: number) {
    setAmong((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!cents || !description.trim() || !among.length) return;
    setBusy(true);
    try {
      await api.addExpense({
        description: description.trim(),
        amount,
        paid_by: paidBy,
        split_with: among.length === data.members.length ? undefined : among,
      });
      toast(`Added ${description.trim()}, ${fmt(cents)}`);
      setAmount("");
      setDescription("");
      setPaidBy(data.me);
      setAmong(data.members.map((m) => m.id));
      setOpen(false);
      await qc.invalidateQueries({ queryKey: ["money"] });
    } catch (err) {
      toast(errorText(err, "Couldn't add the expense."));
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="btn" style={{ width: "100%", marginTop: 14 }} onClick={() => setOpen(true)}>
        Add an expense
      </button>
    );
  }
  return (
    <section className="section" aria-labelledby="add-exp">
      <h2 className="section-title" id="add-exp">Add an expense</h2>
      <form onSubmit={submit} style={{ display: "grid", gap: 10 }}>
        <div className="inline-form" style={{ margin: 0 }}>
          <input className="field field-narrow money-amount" inputMode="decimal" placeholder="$0.00"
                 aria-label="Amount" value={amount} onChange={(e) => setAmount(e.target.value)} autoFocus />
          <input className="field" dir="auto" placeholder="What for?" aria-label="What for?"
                 value={description} onChange={(e) => setDescription(e.target.value)} maxLength={120} />
        </div>
        <label className="row-meta" htmlFor="paid-by">Paid by</label>
        <select id="paid-by" className="field" value={paidBy} onChange={(e) => setPaidBy(Number(e.target.value))}>
          {data.members.map((m) => (
            <option key={m.id} value={m.id}>{m.id === data.me ? `${m.name} (you)` : m.name}</option>
          ))}
        </select>
        <fieldset className="money-split">
          <legend className="row-meta">Split between</legend>
          {data.members.map((m) => (
            <button type="button" key={m.id} className={`chip-toggle${among.includes(m.id) ? " on" : ""}`}
                    aria-pressed={among.includes(m.id)} onClick={() => toggle(m.id)}>
              {m.name}
            </button>
          ))}
        </fieldset>
        <p className="row-meta" aria-live="polite">
          {!among.length ? "Pick at least one person."
            : cents ? (shares.length === 1 ? `${fmt(shares[0])} each` : `${fmt(Math.min(...shares))}–${fmt(Math.max(...shares))} each`)
            : "\u00a0"}
        </p>
        <div className="inline-form" style={{ margin: 0 }}>
          <button className="btn" disabled={busy || !cents || !description.trim() || !among.length}>Add expense</button>
          <button type="button" className="btn btn-quiet" onClick={() => setOpen(false)}>Cancel</button>
        </div>
      </form>
    </section>
  );
}

function Recent({ data, toast }: { data: MoneyOverview; toast: (msg: string) => void }) {
  const qc = useQueryClient();

  async function remove(id: number, what: string) {
    if (!window.confirm(`Delete "${what}"? Balances will be recalculated.`)) return;
    try {
      await api.deleteExpense(id);
      toast("Expense deleted");
      await qc.invalidateQueries({ queryKey: ["money"] });
    } catch (err) {
      toast(errorText(err, "Couldn't delete it."));
    }
  }

  return (
    <section className="section" aria-labelledby="recent-exp">
      <h2 className="section-title" id="recent-exp">Recent</h2>
      {data.expenses.length === 0 && data.payments.length === 0 && (
        <p className="empty">No expenses yet.</p>
      )}
      <ul className="rows">
        {data.expenses.map((e) => (
          <li className="row" key={`e${e.id}`}>
            <span className="row-main">
              <span className="row-title">{e.description}</span>
              <span className="row-meta">
                {relativeDay(e.spent_on)}, {e.paid_by === data.me ? "you" : e.paid_by_name} paid
                {e.shares.length < data.members.length && `, for ${e.shares.map((s) => (s.member_id === data.me ? "you" : s.name)).join(", ")}`}
              </span>
            </span>
            <span className="money-amount-cell">{fmt(e.amount_cents)}</span>
            {e.can_delete && (
              <button className="btn btn-quiet btn-small" onClick={() => void remove(e.id, e.description)}
                      aria-label={`Delete ${e.description}`}>Delete</button>
            )}
          </li>
        ))}
        {data.payments.map((p) => (
          <li className="row" key={`p${p.id}`}>
            <span className="row-main">
              <span className="row-title">{p.from_name} paid {p.to_name}</span>
              <span className="row-meta">{relativeDay(p.settled_on)}</span>
            </span>
            <span className="money-amount-cell">{fmt(p.cents)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
