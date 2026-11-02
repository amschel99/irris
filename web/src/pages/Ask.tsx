import { ArrowUp, History, Plus, Square, Trash2, Volume2, VolumeX } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Answer } from "../components/Answer";
import { MicButton } from "../components/media";
import { Empty, ErrorBox, Spinner } from "../components/ui";
import { askStream, del, get } from "../lib/api";
import { useSession } from "../lib/session";
import type { Message } from "../lib/types";
import { ago, errText, TOOL_LABEL, useLoad } from "../lib/util";
import { pref, setPref, speaker } from "../lib/voice";

interface Conv {
  id: string;
  title: string;
  updated: number;
}

interface Pending {
  text: string;
  steps: string[];
}

const STARTERS = [
  "What are our open deviations this week?",
  "Walk me through the mould change SOP",
  "Which machine had the most stops last month?",
  "What does our manual say about drying PA66?",
];

export function useAsk() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);

  const send = useCallback(async (text: string, extra: Record<string, unknown> = {}, conversationId?: string | null) => {
    setError("");
    const now = Date.now() / 1000;
    setMessages((m) => [...m, { id: -Date.now(), role: "user", content: text, meta: {}, created: now }]);
    setPending({ text, steps: [] });
    abort.current = new AbortController();
    let result: any = null;
    try {
      await askStream(
        { message: text, conversation_id: conversationId || undefined, ...extra },
        (ev, data) => {
          if (ev === "tool") setPending((p) => p && { ...p, steps: [...p.steps, TOOL_LABEL[data.name] || data.name] });
          if (ev === "done") result = data;
          if (ev === "error") setError(data.message || "Something went wrong.");
        },
        abort.current.signal,
      );
    } catch (e: any) {
      if (e?.name !== "AbortError") setError(errText(e));
    } finally {
      setPending(null);
      abort.current = null;
    }
    if (result) {
      setMessages((m) => [
        ...m,
        {
          id: result.message_id, role: "assistant", content: result.reply, created: Date.now() / 1000,
          meta: { sources: result.sources, tools: result.tools, actions: result.actions, seconds: result.seconds },
        },
      ]);
    }
    return result as { conversation_id: string; reply: string } | null;
  }, []);

  const stop = () => abort.current?.abort();
  return { messages, setMessages, pending, error, send, stop };
}

export function Thinking({ steps }: { steps: string[] }) {
  return (
    <div className="msg-ai" aria-live="polite">
      <div className="steps">
        {steps.map((s, i) => <div key={i} className="s">✓ {s}</div>)}
        <div className="s"><Spinner /> {steps.length ? "Working…" : "Thinking…"}</div>
      </div>
    </div>
  );
}

export function Composer({ onSend, busy, onStop, placeholder, autoFocus, mic = true }: { onSend: (t: string) => void; busy: boolean; onStop?: () => void; placeholder?: string; autoFocus?: boolean; mic?: boolean }) {
  const [text, setText] = useState("");
  const [partial, setPartial] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const submit = () => {
    if (!text.trim() || busy) return;
    onSend(text.trim());
    setText("");
  };
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(200, el.scrollHeight) + "px";
  }, [text]);
  return (
    <>
    {partial && <div className="partial" aria-live="polite">{partial}…</div>}
    <div className="composer-inner">
      <textarea
        ref={ref}
        rows={1}
        value={text}
        autoFocus={autoFocus}
        aria-label="Message"
        placeholder={placeholder || "Ask about your plant, data or procedures"}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      {mic && !text.trim() && !busy && <MicButton onText={(t) => onSend(t)} onPartial={setPartial} label="Talk to Irris" />}
      {busy && onStop ? (
        <button className="btn icon" aria-label="Stop" onClick={onStop}><Square /></button>
      ) : (
        (text.trim() || !mic) && <button className="btn primary icon" aria-label="Send" disabled={!text.trim() || busy} onClick={submit}><ArrowUp /></button>
      )}
    </div>
    </>
  );
}

export function AskPage() {
  const { workspace, toast } = useSession();
  const [params, setParams] = useSearchParams();
  const convId = params.get("c");
  const [showList, setShowList] = useState(false);
  const convs = useLoad<{ conversations: Conv[] }>(() => get("/conversations"), [workspace?.id]);
  const { messages, setMessages, pending, error, send, stop } = useAsk();
  const [loadingConv, setLoadingConv] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const started = useRef(false);
  const [speak, setSpeak] = useState(() => pref("speakReplies", false));
  const speakRef = useRef(speak);
  speakRef.current = speak;
  useEffect(() => () => speaker.stop(), []);

  useEffect(() => {
    if (!convId) {
      if (!pending) setMessages([]);
      return;
    }
    if (messages.length && pending) return;
    let cancel = false;
    setLoadingConv(true);
    get(`/conversations/${convId}`)
      .then((c) => !cancel && setMessages(c.messages))
      .catch((e) => toast(errText(e), true))
      .finally(() => !cancel && setLoadingConv(false));
    return () => {
      cancel = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convId]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  const submit = async (text: string) => {
    speaker.stop();
    const res = await send(text, {}, convId);
    if (res?.reply && speakRef.current) speaker.say(res.reply);
    if (res?.conversation_id && res.conversation_id !== convId) {
      setParams({ c: res.conversation_id }, { replace: true });
    }
    convs.reload();
  };

  useEffect(() => {
    const q = params.get("q");
    if (q && !started.current) {
      started.current = true;
      setParams({}, { replace: true });
      submit(q);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const remove = async (id: string) => {
    await del(`/conversations/${id}`).catch(() => {});
    if (id === convId) setParams({});
    convs.reload();
  };

  return (
    <div className="ask">
      <aside className={`convs${showList ? " open" : ""}`}>
        <div className="convs-head">
          <h2>Conversations</h2>
          <button className="btn sm" onClick={() => { setParams({}); setShowList(false); }}><Plus /> New</button>
        </div>
        <ul>
          {convs.data?.conversations.map((c) => (
            <li key={c.id} className="row" style={{ gap: 2 }}>
              <button aria-current={c.id === convId} onClick={() => { setParams({ c: c.id }); setShowList(false); }}>
                <span className="ct">{c.title || "Untitled"}</span>
                <span className="faint" style={{ fontSize: 12 }}>{ago(c.updated)}</span>
              </button>
              <button className="btn sm icon ghost" aria-label="Delete conversation" onClick={() => remove(c.id)}><Trash2 /></button>
            </li>
          ))}
          {convs.data && !convs.data.conversations.length && <li className="faint" style={{ padding: "8px 10px", fontSize: 13 }}>Your conversations will appear here.</li>}
        </ul>
      </aside>
      <section className="chat">
        <div className="chat-scroll" ref={scroller}>
          <div className="chat-inner">
            <div className="row between chat-tools">
              <span className="mobile-show row" style={{ gap: 6 }}>
                <button className="btn sm" onClick={() => setShowList((s) => !s)}><History /> Conversations</button>
                <button className="btn sm ghost" onClick={() => setParams({})}><Plus /> New</button>
              </span>
              <span className="grow" />
              <button
                className={`btn sm${speak ? "" : " ghost"}`}
                aria-pressed={speak}
                onClick={() => { const v = !speak; setSpeak(v); setPref("speakReplies", v); if (!v) speaker.stop(); }}
                title={speak ? "Irris reads her answers aloud" : "Answers are silent"}
              >
                {speak ? <Volume2 /> : <VolumeX />} <span className="desktop-only">{speak ? "Speaking replies" : "Speak replies"}</span>
              </button>
            </div>
            {loadingConv && <div className="loading"><Spinner /> Loading…</div>}
            {!messages.length && !pending && !loadingConv && (
              <div className="stack lg" style={{ paddingTop: "8vh" }}>
                <div>
                  <h1 style={{ fontSize: 26, fontWeight: 500, letterSpacing: "-0.02em", margin: 0 }}>What do you want to do?</h1>
                  <p className="muted" style={{ margin: "6px 0 0" }}>Irris searches your documents and records, queries your databases and looks through your cameras, then answers with sources.</p>
                </div>
                <div className="chips">
                  {STARTERS.map((s) => <button key={s} className="chip" onClick={() => submit(s)}>{s}</button>)}
                </div>
                {!workspace?.defaults.llm && !workspace?.models.llm?.base_url && (
                  <Empty title="Answers come from search until a reasoning model is set">
                    Irris will show you the closest passages from your data. Add a model in Settings → Models so it can reason, query databases and take actions.
                  </Empty>
                )}
              </div>
            )}
            {messages.map((m) =>
              m.role === "user" ? (
                <div key={m.id} className="msg-user">{m.content}</div>
              ) : (
                <Answer key={m.id} text={m.content} sources={m.meta.sources} actions={m.meta.actions} seconds={m.meta.seconds} />
              ),
            )}
            {pending && <Thinking steps={pending.steps} />}
            <ErrorBox error={error} />
          </div>
        </div>
        <div className="composer">
          <Composer onSend={submit} busy={!!pending} onStop={stop} autoFocus />
        </div>
      </section>
    </div>
  );
}
