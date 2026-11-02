import { AlertTriangle, Bell, Download, Mic, ScanLine, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { fileUrl, subscribeAlerts, whereLabel, type Alert, type VisionStatus } from "../lib/media";
import { useSession } from "../lib/session";
import { micSupported, openMic, partials, pref, recordClip, speaker, transcribe } from "../lib/voice";
import { RecordDrawer } from "./RecordDrawer";
import { Spinner } from "./ui";

// ---------------------------------------------------------------- where vision runs, and how fast

export function VisionBadge({ status, onDark }: { status: VisionStatus | null; onDark?: boolean }) {
  if (!status) return null;
  const cls = `vbadge${onDark ? " dark" : ""}`;
  if (!status.ready) return <span className={`${cls} off`} title="No vision model is set up">VISION OFF</span>;
  const last = status.last;
  const where = last?.where || status.active?.where || "gpu";
  const ms = last ? (last.ms >= 1000 ? `${(last.ms / 1000).toFixed(1)} s` : `${last.ms} ms`) : "";
  const fallback = where === "cpu" && status.chain.length > 1;
  return (
    <span
      className={`${cls}${where === "cpu" ? " cpu" : ""}`}
      title={`${status.chain.map((c) => `${c.model} on ${whereLabel(c.where)}${c.down ? " (unreachable)" : ""}`).join(" → ")}${status.cpu_busy ? " · CPU busy" : ""}`}
    >
      <span className="dot" /> {whereLabel(where)}{fallback ? " FALLBACK" : ""}{ms ? ` · ${ms}` : ""}
    </span>
  );
}

// ---------------------------------------------------------------- tap to talk

export function MicButton({ onText, onPartial, disabled, size = "", label = "Talk" }: {
  onText: (t: string) => void;
  onPartial?: (t: string) => void;
  disabled?: boolean;
  size?: string;
  label?: string;
}) {
  const [state, setState] = useState<"idle" | "rec" | "busy">("idle");
  const [err, setErr] = useState("");
  const clip = useRef<{ stop: () => Promise<Blob> } | null>(null);
  const mic = useRef<MediaStream | null>(null);
  const stopPartials = useRef<(() => void) | null>(null);

  useEffect(() => () => {
    mic.current?.getTracks().forEach((t) => t.stop());
    stopPartials.current?.();
  }, []);

  const toggle = useCallback(async () => {
    setErr("");
    if (state === "rec") {
      const c = clip.current;
      clip.current = null;
      stopPartials.current?.();
      stopPartials.current = null;
      setState("busy");
      try {
        const blob = await c!.stop();
        mic.current?.getTracks().forEach((t) => t.stop());
        mic.current = null;
        const text = blob.size > 1500 ? await transcribe(blob) : "";
        if (text) onText(text);
        else setErr("I didn't catch that.");
      } catch (e: any) {
        setErr(e?.message || "Couldn't understand that.");
      } finally {
        setState("idle");
        onPartial?.("");
      }
      return;
    }
    if (state !== "idle") return;
    try {
      speaker.stop();
      mic.current = await openMic();
      clip.current = recordClip(mic.current);
      stopPartials.current = onPartial ? partials(onPartial) : null;
      setState("rec");
    } catch (e: any) {
      setErr(e?.message || "Couldn't open the microphone.");
    }
  }, [state, onText, onPartial]);

  if (!micSupported()) return null;
  return (
    <span className="mic-wrap">
      <button
        type="button"
        className={`btn icon ${size} mic${state === "rec" ? " on" : ""}`}
        aria-label={state === "rec" ? "Stop and send" : label}
        aria-pressed={state === "rec"}
        title={err || (state === "rec" ? "Tap to send" : label)}
        disabled={disabled || state === "busy"}
        onClick={toggle}
      >
        {state === "busy" ? <Spinner /> : state === "rec" ? <Square /> : <Mic />}
      </button>
      {err && <span className="mic-err" role="status">{err}</span>}
    </span>
  );
}

// ---------------------------------------------------------------- alerts, everywhere in the app

export function AlertCenter() {
  const { workspace } = useSession();
  const [items, setItems] = useState<Alert[]>([]);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    if (!workspace?.id) return;
    return subscribeAlerts((a) => {
      setItems((cur) => (cur.some((x) => x.id === a.id) ? cur : [...cur.slice(-3), a]));
      if (a.speak && pref("speakAlerts", true) && !document.hidden) {
        speaker.say(`${a.camera ? a.camera + ". " : ""}${a.say || a.title}`, { interrupt: a.severity === "danger" });
      }
      const ttl = a.severity === "danger" ? 30000 : 12000;
      window.setTimeout(() => setItems((cur) => cur.filter((x) => x.id !== a.id)), ttl);
    });
  }, [workspace?.id]);

  if (!items.length && !open) return null;
  return (
    <>
    {open && <RecordDrawer id={open} onClose={() => setOpen(null)} />}
    <div className="alerts" role="region" aria-label="Alerts" aria-live="assertive">
      {items.map((a) => (
        <div key={a.id} className={`alert ${a.severity}`}>
          <div className="alert-icon">{a.kind === "job" ? <Bell /> : a.severity === "info" ? <ScanLine /> : <AlertTriangle />}</div>
          <div className="grow">
            <div className="alert-k mono">
              {a.kind === "watch" ? "WATCH" : a.kind === "coach" ? "COACH" : a.kind === "notice" ? "ANSWER" : "REVIEW"}
              {a.camera ? ` · ${a.camera.toUpperCase()}` : ""}
            </div>
            <div className="alert-t">{a.kind === "watch" || a.kind === "notice" ? a.title : a.say || a.title}</div>
            {(a.kind === "watch" || a.kind === "notice") && a.say && <div className="alert-s">{a.say}</div>}
            <div className="row" style={{ gap: 6, marginTop: 8 }}>
              {a.camera_id && <Link className="btn sm" to={`/live?camera=${a.camera_id}`}>Open Live</Link>}
              {a.record_id && <button className="btn sm" onClick={() => setOpen(a.record_id!)}>Open review</button>}
            </div>
          </div>
          {a.snapshot && <img className="alert-snap" src={fileUrl(a.snapshot)} alt="" />}
          <button className="btn sm icon ghost" aria-label="Dismiss" onClick={() => setItems((c) => c.filter((x) => x.id !== a.id))}><X /></button>
        </div>
      ))}
    </div>
    </>
  );
}

// ---------------------------------------------------------------- a finished recording

export function RecordingPreview({ url, mime, name, status, onClose }: {
  url: string;
  mime: string;
  name: string;
  status: { state: "uploading" | "saved" | "failed" | "reviewing" | "reviewed"; text: string; link?: string };
  onClose: () => void;
}) {
  return (
    <div className="rec-preview" role="dialog" aria-label="Recording">
      <div className="row between">
        <span className="eyebrow">Recording</span>
        <button className="btn sm icon ghost" aria-label="Close" onClick={onClose}><X /></button>
      </div>
      <video controls playsInline preload="metadata" src={url} />
      <div className="row wrap" style={{ gap: 8 }}>
        <a className="btn sm primary" href={url} download={name}><Download /> Save to this device</a>
        {status.link && <Link className="btn sm" to={status.link}>Open</Link>}
      </div>
      <div className={`rec-status ${status.state}`}>
        {(status.state === "uploading" || status.state === "reviewing") && <Spinner />} {status.text}
      </div>
      <span className="sr">{mime}</span>
    </div>
  );
}
