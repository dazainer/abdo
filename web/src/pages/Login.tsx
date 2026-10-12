import { useState, type FormEvent } from "react";
import { api, ApiError, type Member } from "../api";
import { Sign } from "../components/Sign";
import { CodeInput } from "../components/CodeInput";

export function Login({ onLoggedIn }: { onLoggedIn: (m: Member) => void }) {
  const [step, setStep] = useState<"name" | "code">("name");
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function sendCode(e?: FormEvent) {
    e?.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.requestCode(name.trim());
      setCode("");
      setStep("code");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't send a code. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function verify(value: string) {
    setBusy(true);
    setError(null);
    try {
      const { member } = await api.verify(name.trim(), value);
      onLoggedIn(member);
    } catch (err) {
      setCode("");
      setError(
        err instanceof ApiError && err.status === 400
          ? "That code didn't work. Use the newest one Abdo sent, or send a new code."
          : err instanceof ApiError ? err.message : "Couldn't check the code. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <Sign ar="عبده" en="Abdo" large />

      {step === "name" ? (
        <form onSubmit={sendCode}>
          <label htmlFor="name">What's your name?</label>
          <input
            id="name"
            className="field"
            autoComplete="username"
            autoCapitalize="words"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="The name Abdo knows you by"
            autoFocus
          />
          <button className="btn" disabled={busy || !name.trim()}>
            {busy ? "Sending…" : "Send me a code"}
          </button>
          <p className="login-lead">Abdo sends the code to your Telegram.</p>
        </form>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); if (code.length === 6) void verify(code); }}>
          <p className="login-lead">
            If you're in the suite, Abdo just sent a 6-digit code to your Telegram. Enter it here.
          </p>
          <CodeInput value={code} onChange={setCode} onComplete={(v) => void verify(v)} disabled={busy}
                     label="6-digit code" />
          <button className="btn" disabled={busy || code.length !== 6}>
            {busy ? "Checking…" : "Log in"}
          </button>
          <p className="login-lead">
            <button type="button" className="btn-link" onClick={() => void sendCode()} disabled={busy}>
              Send a new code
            </button>
            {"  or  "}
            <button type="button" className="btn-link" onClick={() => { setStep("name"); setError(null); }}>
              use a different name
            </button>
          </p>
        </form>
      )}

      {error && <p className="notice notice-error" role="alert">{error}</p>}
    </main>
  );
}
