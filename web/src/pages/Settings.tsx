import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, ApiError, type Member, type Prefs } from "../api";
import { Sign } from "../components/Sign";
import { disablePush, enablePush, isIOS, isStandalone, pushState, type PushState } from "../lib/push";

const PUSH_NOTE: Record<PushState, string> = {
  unsupported: "This browser can't show notifications.",
  "needs-install": "On iPhone, notifications only work from the Home Screen app. Install Abdo first (below).",
  denied: "Notifications are blocked. Allow them for Abdo in your phone's settings, then come back.",
  off: "Off on this device",
  on: "On for this device",
};

export function Settings({ me, onLoggedOut, toast }: {
  me: Member;
  onLoggedOut: () => void;
  toast: (msg: string) => void;
}) {
  const [push, setPush] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    pushState().then(setPush).catch(() => setPush("unsupported"));
  }, []);

  async function togglePush() {
    if (!push) return;
    setBusy(true);
    try {
      setPush(push === "on" ? await disablePush() : await enablePush());
    } catch (err) {
      toast(err instanceof Error ? err.message : "Couldn't change notifications.");
    } finally {
      setBusy(false);
    }
  }

  async function testPush() {
    setBusy(true);
    try {
      const { sent } = await api.pushTest();
      toast(sent > 0 ? "Test notification sent" : "No device received it. Turn notifications off and on again.");
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Couldn't send a test notification.");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    try {
      await api.logout();
    } finally {
      onLoggedOut();
    }
  }

  const canToggle = push === "on" || push === "off";

  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: ["prefs"], queryFn: api.prefs });
  async function savePrefs(update: Partial<Prefs>) {
    // Flip the switch right away; put it back if the server says no.
    const before = qc.getQueryData<{ prefs: Prefs }>(["prefs"]);
    if (before) qc.setQueryData(["prefs"], { prefs: { ...before.prefs, ...update } });
    try {
      qc.setQueryData(["prefs"], await api.updatePrefs(update));
    } catch (err) {
      if (before) qc.setQueryData(["prefs"], before);
      toast(err instanceof ApiError ? err.message : "Couldn't save that setting.");
    }
  }
  const p = prefs.data?.prefs;

  return (
    <div className="page">
      <header className="screen-head"><Sign ar="الإعدادات" en="Settings" /></header>
      <main className="screen">
        <section className="section">
          <h2 className="section-title">
            Logged in as {me.name}{me.arabic_name && <> (<bdi lang="ar">{me.arabic_name}</bdi>)</>}
          </h2>
        </section>

        <section className="section" aria-labelledby="notif">
          <ul className="rows">
            <li className="row">
              <span className="row-main">
                <span className="row-title" id="notif">Notifications</span>
                <span className="row-meta">{push ? PUSH_NOTE[push] : "Checking…"}</span>
              </span>
              {canToggle && (
                <button className="switch" role="switch" aria-checked={push === "on"} aria-labelledby="notif"
                        onClick={() => void togglePush()} disabled={busy} />
              )}
            </li>
            {push === "on" && (
              <li className="row">
                <span className="row-main"><span className="row-title">Send a test notification</span></span>
                <button className="btn btn-quiet btn-small" onClick={() => void testPush()} disabled={busy}>Send</button>
              </li>
            )}
          </ul>
        </section>

        {p && (
          <section className="section" aria-labelledby="abdo-sends">
            <h2 className="section-title" id="abdo-sends">What Abdo sends you</h2>
            {push !== "on" && <p className="section-note">Sent on Telegram until notifications are on.</p>}
            <ul className="rows">
              <li className="row">
                <span className="row-main">
                  <span className="row-title" id="brief">Morning briefing</span>
                </span>
                {p.briefing && (
                  <input className="field" type="time" value={p.briefing_time} aria-label="Briefing time"
                         style={{ width: 138, flex: "0 0 138px", minHeight: 38, padding: "4px 10px" }}
                         onChange={(e) => { if (e.target.value) void savePrefs({ briefing_time: e.target.value }); }} />
                )}
                <button className="switch" role="switch" aria-checked={p.briefing} aria-labelledby="brief"
                        onClick={() => void savePrefs({ briefing: !p.briefing })} />
              </li>
              <li className="row">
                <span className="row-main">
                  <span className="row-title" id="pray">Prayer times</span>
                  <span className="row-meta">Fajr is silent</span>
                </span>
                <button className="switch" role="switch" aria-checked={p.prayers} aria-labelledby="pray"
                        onClick={() => void savePrefs({ prayers: !p.prayers })} />
              </li>
            </ul>
          </section>
        )}

        <section className="section">
          <ul className="rows">
            <li className="row">
              <span className="row-main">
                <span className="row-title">Your classes</span>
              </span>
              <Link to="/schedule" className="btn btn-quiet btn-small" style={{ textDecoration: "none" }}>Open</Link>
            </li>
          </ul>
        </section>

        {!isStandalone() && (
          <section className="section">
            <h2 className="section-title">Install Abdo on your phone</h2>
            {isIOS() ? (
              <p>In Safari, tap the Share button, then <strong>Add to Home Screen</strong>. Open Abdo from the new icon.</p>
            ) : (
              <p>Open your browser's menu and tap <strong>Install app</strong> (or <strong>Add to Home screen</strong>).</p>
            )}
          </section>
        )}

        <section className="section">
          <button className="btn btn-quiet" onClick={() => void logout()}>Log out</button>
          <p className="section-note" style={{ marginTop: 14 }}>App version {__BUILD_TIME__} UTC</p>
        </section>
      </main>
    </div>
  );
}
