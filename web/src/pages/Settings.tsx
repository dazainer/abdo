import { useEffect, useState } from "react";
import { api, ApiError, type Member } from "../api";
import { Sign } from "../components/Sign";
import { disablePush, enablePush, isIOS, isStandalone, pushState, type PushState } from "../lib/push";

const PUSH_NOTE: Record<PushState, string> = {
  unsupported: "This browser can't show notifications.",
  "needs-install": "On iPhone, notifications only work from the Home Screen app. Install Abdo first (below).",
  denied: "Notifications are blocked. Allow them for Abdo in your phone's settings, then come back.",
  off: "Get a notification when Abdo has something for you.",
  on: "You'll get notifications on this device.",
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
        </section>
      </main>
    </div>
  );
}
