import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ChatMessage, type Member } from "../api";
import { Sign } from "../components/Sign";
import { Icon } from "../components/Icon";
import { canRecord, playMp3, prepareToRecord, Recorder, rememberSpoken, spokenAudio, unlockAudio, type Recording } from "../lib/recorder";
import { clock, dayOf, relativeDay } from "../lib/dates";

type Pending = { kind: "text"; text: string } | { kind: "voice" };

export function Chat({ me, toast }: { me: Member; toast: (msg: string) => void }) {
  const qc = useQueryClient();
  const latest = useQuery({
    queryKey: ["history"],
    queryFn: () => api.history(),
    // A message sent from Telegram shows up here too (shared thread).
    refetchInterval: 5000,
  });
  const [older, setOlder] = useState<ChatMessage[]>([]);
  const [olderHasMore, setOlderHasMore] = useState<boolean | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  const [recording, setRecording] = useState(false);
  const [, setSpokenVersion] = useState(0); // re-render when a reply gains audio

  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const keepScroll = useRef<number | null>(null);
  const recorder = useRef<Recorder | null>(null);
  const pressStarted = useRef<Promise<void> | null>(null);

  const messages = useMemo(() => {
    const byId = new Map<number, ChatMessage>();
    for (const m of [...older, ...(latest.data?.messages ?? [])]) byId.set(m.id, m);
    return [...byId.values()].sort((a, b) => a.id - b.id);
  }, [older, latest.data]);

  const hasMore = olderHasMore ?? latest.data?.has_more ?? false;

  // Stick to the bottom as messages arrive, except right after loading older ones.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (keepScroll.current !== null && list) {
      list.scrollTop = list.scrollHeight - keepScroll.current;
      keepScroll.current = null;
      return;
    }
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, pending]);

  async function loadEarlier() {
    const first = messages[0];
    if (!first) return;
    keepScroll.current = (listRef.current?.scrollHeight ?? 0) - (listRef.current?.scrollTop ?? 0);
    try {
      const page = await api.history(first.id);
      setOlder((prev) => [...page.messages, ...prev]);
      setOlderHasMore(page.has_more);
    } catch (err) {
      keepScroll.current = null;
      toast(err instanceof ApiError ? err.message : "Couldn't load earlier messages.");
    }
  }

  async function sendText() {
    const value = text.trim();
    if (!value || pending) return;
    setText("");
    if (inputRef.current) inputRef.current.style.height = ""; // shrink back to one line
    setPending({ kind: "text", text: value });
    try {
      await api.chat(value);
      await qc.invalidateQueries({ queryKey: ["history"] });
    } catch (err) {
      setText(value); // give the message back so nothing is lost
      toast(err instanceof ApiError ? err.message : "Couldn't reach Abdo. Try again.");
    } finally {
      setPending(null);
    }
  }

  async function sendVoice(rec: Recording) {
    setPending({ kind: "voice" });
    try {
      const res = await api.voice(rec.blob, rec.filename);
      await qc.invalidateQueries({ queryKey: ["history"] });
      if (!res.transcript) {
        toast(res.reply);
        return;
      }
      if (res.audio) {
        rememberSpoken(res.reply, res.audio);
        setSpokenVersion((v) => v + 1);
        if (!(await playMp3(res.audio))) toast("Tap Play to hear Abdo's reply.");
      }
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Couldn't send the voice note. Try again.");
    } finally {
      setPending(null);
    }
  }

  // ---- hold to talk ---------------------------------------------------------
  async function startRecording() {
    unlockAudio(); // inside the gesture, so the spoken reply can play later (iOS)
    prepareToRecord();
    const r = new Recorder();
    recorder.current = r;
    setRecording(true);
    try {
      await r.start(() => void finishRecording());
    } catch {
      recorder.current = null;
      setRecording(false);
      toast("Abdo needs the microphone. Allow it in your browser settings, then hold to talk.");
    }
  }

  async function finishRecording() {
    await pressStarted.current;
    const r = recorder.current;
    recorder.current = null;
    setRecording(false);
    if (!r) return;
    const rec = await r.stop();
    if (!rec || rec.seconds < 0.6) {
      toast("Hold the mic button while you talk, then let go.");
      return;
    }
    void sendVoice(rec);
  }

  function onMicDown(e: PointerEvent<HTMLButtonElement>) {
    if (pending) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    pressStarted.current = startRecording();
  }

  function onMicKey(e: KeyboardEvent<HTMLButtonElement>) {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) {
      e.preventDefault();
      if (recording) void finishRecording();
      else if (!pending) pressStarted.current = startRecording();
    }
  }

  useEffect(() => () => recorder.current?.cancel(), []);

  // ---- render -----------------------------------------------------------------
  let prevDay = "";

  return (
    <div className="chat">
      <header className="screen-head"><Sign ar="عبده" en="Abdo" /></header>

      <div className="screen" ref={listRef} aria-live="polite">
        {hasMore && (
          <button className="btn btn-quiet btn-small load-earlier" onClick={() => void loadEarlier()}>
            Load earlier messages
          </button>
        )}

        {latest.isError && !latest.data && (
          <p className="notice notice-error">Couldn't load the conversation. Check your connection.</p>
        )}

        {latest.data && messages.length === 0 && !pending && (
          <div className="msg abdo">
            <div className="bubble plate" dir="auto">
              أهلاً يا {me.arabic_name ?? me.name}! Ask me about groceries, packages, the calendar, or anything else.
            </div>
          </div>
        )}

        {messages.map((m) => {
          const day = dayOf(m.created_at);
          const mark = day !== prevDay ? <div className="day-mark">{relativeDay(day)}</div> : null;
          prevDay = day;
          const mine = m.role === "user";
          const audio = mine ? undefined : spokenAudio(m.content);
          return (
            <div key={m.id} style={{ display: "contents" }}>
              {mark}
              <div className={`msg ${mine ? "mine" : "abdo"}`}>
                <div className={`bubble${mine ? "" : " plate"}`} dir="auto">{m.content}</div>
                <div className="msg-meta">
                  {m.channel === "telegram" && <><Icon name="telegram" /><span>Telegram</span></>}
                  {m.modality === "voice" && mine && <Icon name="mic" label="Voice" />}
                  <span>{clock(m.created_at)}</span>
                  {audio && (
                    <button className="replay" onClick={() => {
                      void playMp3(audio, { fromTap: true }).then((ok) => {
                        if (!ok) toast("Couldn't play the audio. Check that your volume is up.");
                      });
                    }}>Play</button>
                  )}
                </div>
              </div>
            </div>
          );
        })}

        {pending && (
          <>
            <div className="msg mine">
              <div className="bubble" dir="auto">
                {pending.kind === "text" ? pending.text : <span className="msg-meta"><Icon name="mic" /> Voice message</span>}
              </div>
            </div>
            <div className="msg abdo" aria-label="Abdo is replying">
              <div className="bubble plate typing"><span /><span /><span /></div>
            </div>
          </>
        )}
        <div ref={endRef} />
      </div>

      <div className="composer">
        {recording ? (
          <span className="rec-hint" role="status">Recording… let go to send</span>
        ) : (
          <textarea
            ref={inputRef}
            rows={1}
            dir="auto"
            value={text}
            placeholder="Message Abdo"
            aria-label="Message Abdo"
            disabled={!!pending}
            onChange={(e) => {
              setText(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${e.target.scrollHeight}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void sendText();
              }
            }}
          />
        )}
        {text.trim() && !recording ? (
          <button className="round send" onClick={() => void sendText()} disabled={!!pending} aria-label="Send">
            <Icon name="send" />
          </button>
        ) : canRecord() ? (
          <button
            className={`round mic${recording ? " recording" : ""}`}
            aria-label={recording ? "Stop and send voice message" : "Hold to talk"}
            aria-pressed={recording}
            disabled={!!pending && !recording}
            onPointerDown={onMicDown}
            onPointerUp={() => { unlockAudio(); void finishRecording(); }}
            onPointerCancel={() => { recorder.current?.cancel(); recorder.current = null; setRecording(false); }}
            onKeyDown={onMicKey}
            onContextMenu={(e) => e.preventDefault()}
          >
            <Icon name="mic" />
          </button>
        ) : null}
      </div>
    </div>
  );
}
