import { useCallback, useEffect, useState } from "react";
import { Route, Routes, Navigate } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Member } from "./api";
import { TabBar } from "./components/TabBar";
import { Login } from "./pages/Login";
import { Chat } from "./pages/Chat";
import { Lists } from "./pages/Lists";
import { Today } from "./pages/Today";
import { Money } from "./pages/Money";
import { Settings } from "./pages/Settings";

function useToast() {
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    if (!message) return;
    const t = window.setTimeout(() => setMessage(null), 3500);
    return () => window.clearTimeout(t);
  }, [message]);
  return { message, show: useCallback((m: string) => setMessage(m), []) };
}

export function App() {
  const qc = useQueryClient();
  const toast = useToast();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    retry: (count, err) => !(err instanceof ApiError && err.status === 401) && count < 2,
    // Switching to Telegram to read the login code and back must not refetch this
    // and remount the login screen (it would lose the code step). A 401 from any
    // other screen still sends you back to login via the cache subscription below.
    refetchOnWindowFocus: false,
  });

  // Any 401 from another screen means the session ended: back to login.
  useEffect(() => {
    return qc.getQueryCache().subscribe((event) => {
      const err = event.query.state.error;
      if (err instanceof ApiError && err.status === 401 && event.query.queryKey[0] !== "me") {
        void qc.invalidateQueries({ queryKey: ["me"] });
      }
    });
  }, [qc]);

  const loggedIn = (m: Member) => {
    qc.setQueryData(["me"], { member: m });
  };
  const loggedOut = () => {
    qc.clear();
    void qc.invalidateQueries({ queryKey: ["me"] });
  };

  let body;
  if (!me.data && !me.isFetched) {
    body = null; // first load only; later refetches never unmount the login screen
  } else if (!me.data && me.isError && !(me.error instanceof ApiError && me.error.status === 401)) {
    body = (
      <main className="login">
        <p className="notice notice-error" role="alert">{me.error.message}</p>
        <button className="btn" onClick={() => void me.refetch()}>Try again</button>
      </main>
    );
  } else if (!me.data) {
    body = <Login onLoggedIn={loggedIn} />;
  } else {
    const member = me.data.member;
    body = (
      <>
        <div className="shell">
          <Routes>
            <Route path="/" element={<Chat me={member} toast={toast.show} />} />
            <Route path="/lists" element={<Lists toast={toast.show} />} />
            <Route path="/today" element={<Today me={member} toast={toast.show} />} />
            <Route path="/calendar" element={<Navigate to="/today" replace />} />
            <Route path="/money" element={<Money toast={toast.show} />} />
            <Route path="/settings" element={<Settings me={member} onLoggedOut={loggedOut} toast={toast.show} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
        <TabBar />
      </>
    );
  }

  return (
    <>
      {body}
      {toast.message && <div className="toast" role="status">{toast.message}</div>}
    </>
  );
}
