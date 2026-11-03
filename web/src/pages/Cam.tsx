import { Camera, Pause, Play, RefreshCw, Video, Volume2, VolumeX } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { RecordingPreview } from "../components/media";
import { Mark, Spinner } from "../components/ui";
import { grabJpeg, keepAwake, openCamera, stopCamera } from "../lib/camera";
import { uploadRecording } from "../lib/media";
import { drawScene, mediaRect, Perceiver, type Scene } from "../lib/perceive";
import { canRecord, LiveRecorder, mmss, type Overlay } from "../lib/recorder";
import { errText, useInterval } from "../lib/util";
import { pref, setPref, speaker } from "../lib/voice";
import { labelMatches, prereqsDone, Reminders, stepStatus, Watcher, type GuidedRun, type WatchState } from "../lib/guidance";
import { scheduler, type SayItem, type Severity } from "../lib/scheduler";
import "../guidance.css";

function tone() {
  try {
    const ac = speaker.context();
    const o = ac.createOscillator();
    const g = ac.createGain();
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, ac.currentTime);
    g.gain.exponentialRampToValueAtTime(0.25, ac.currentTime + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.16);
    o.connect(g).connect(ac.destination);
    o.start();
    o.stop(ac.currentTime + 0.18);
  } catch {
    /* no audio */
  }
}

interface Said {
  id: number;
  kind: "verdict" | "alert";
  tone: string;
  head: string;
  text: string;
}

/** The paired phone's page: no sign-in, the pairing token is the credential. */
export function Cam() {
  const [params] = useSearchParams();
  const t = params.get("t") || "";
  const [info, setInfo] = useState<{ id?: string; name: string; workspace: string } | null>(null);
  const infoId = useRef("");
  infoId.current = info?.id || "";
  const [error, setError] = useState("");
  const [state, setState] = useState<"idle" | "starting" | "live" | "paused">("idle");
  const [sent, setSent] = useState(0);
  const [lastOk, setLastOk] = useState(0);
  const [facing, setFacing] = useState<"environment" | "user">("environment");
  const [said, setSaid] = useState<Said[]>([]);
  const [step, setStep] = useState<string>("");
  const [talk, setTalk] = useState(() => pref("camSpeak", true));
  const [recording, setRecording] = useState(false);
  const [recClock, setRecClock] = useState(0);
  const [clip, setClip] = useState<{ url: string; mime: string; name: string; status: { state: "uploading" | "saved" | "failed" | "reviewing" | "reviewed"; text: string } } | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const release = useRef<() => void>(() => {});
  const sock = useRef<WebSocket | null>(null);
  const rec = useRef<LiveRecorder | null>(null);
  const recMic = useRef<MediaStream | null>(null);
  const talkRef = useRef(talk);
  talkRef.current = talk;
  const perceiver = useRef<Perceiver | null>(null);
  const layer = useRef<HTMLCanvasElement>(null);
  const [scene, setScene] = useState<Scene | null>(null);
  const [reading, setReading] = useState<{ text: string; t: number; confirm?: boolean } | null>(null);
  // the run this camera is on, pushed by the server: zones, the current step, warnings before mistakes
  const [run, setRun] = useState<GuidedRun | null>(null);
  const runRef = useRef<GuidedRun | null>(null);
  runRef.current = run;
  const watcher = useRef(new Watcher());
  const reminders = useRef(new Reminders());
  const [watch, setWatch] = useState<WatchState | null>(null);
  const [flash, setFlash] = useState(0);

  // her voice on this phone comes from the workspace's TTS, through the pairing token; every line goes through the
  // scheduler (critical now, the rest when it's polite)
  useEffect(() => {
    speaker.ttsUrl = `/api/v1/cam/tts?t=${encodeURIComponent(t)}`;
    speaker.headers = () => ({});
    scheduler.enabled = () => talkRef.current;
    scheduler.onCaption = null;
    scheduler.cleared = (item) => {
      const r = runRef.current;
      if (item.step === undefined || !r) return false;
      if (r.status !== "active") return true;
      const s = r.steps[item.step];
      if (!s || ["complete", "skipped"].includes(stepStatus(r, s))) return true;
      if (item.topic?.endsWith(":wrong-object")) {
        const held = watcher.current.lastTouched;
        return !held || labelMatches(held, s.expected_objects || []);
      }
      if (item.topic?.endsWith(":prereq")) return prereqsDone(r, item.step).ok;
      return false;
    };
    watcher.current.onSay = (item) => {
      if (item.severity === "critical") {
        setFlash(Date.now());
        if (talkRef.current) tone();
      }
      setSaid((cur) => [...cur.slice(-2), { id: Date.now() + Math.random(), kind: "alert", tone: item.severity === "critical" ? "danger" : "fail", head: "WATCH", text: item.text }]);
      scheduler.say(item);
    };
    return () => {
      speaker.stop();
      scheduler.flush();
      scheduler.enabled = () => true;
      scheduler.cleared = () => false;
      watcher.current.onSay = null;
    };
  }, [t]);

  const show = useCallback((s: Omit<Said, "id">, item: Partial<SayItem> = {}) => {
    setSaid((cur) => [...cur.slice(-2), { ...s, id: Date.now() + Math.random() }]);
    if (!s.text) return;
    const severity: Severity = s.tone === "danger" ? "critical" : s.kind === "verdict" ? "major" : s.tone === "info" ? "minor" : "major";
    scheduler.say({ text: s.text, severity, kind: s.kind === "verdict" ? "verdict" : "warning", ...item });
  }, []);

  useInterval(() => {
    for (const item of reminders.current.due(runRef.current)) show({ kind: "alert", tone: "info", head: "REMINDER", text: item.text }, item);
  }, run?.status === "active" ? 1000 : null);

  useEffect(() => {
    if (!t) {
      setError("This page needs a pairing link. Open Cameras in Irris on a computer and pair this phone.");
      return;
    }
    fetch(`/api/v1/cam/info?t=${encodeURIComponent(t)}`)
      .then(async (r) => {
        const b = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(b.detail || "This pairing link isn't valid.");
        setInfo(b);
      })
      .catch((e) => setError(errText(e)));
    return () => {
      stopCamera(stream.current);
      release.current();
      sock.current?.close();
    };
  }, [t]);

  // WebSocket: frames up, verdicts and alerts down. Falls back to HTTP POSTs.
  useEffect(() => {
    if (state !== "live" || !t) return;
    let closed = false;
    let retry = 0;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const s = new WebSocket(`${proto}://${location.host}/api/v1/cam/ws?t=${encodeURIComponent(t)}`);
      s.binaryType = "arraybuffer";
      s.onmessage = (e) => {
        let m: any;
        try { m = JSON.parse(e.data); } catch { return; }
        if (m.type === "verdict") {
          const head = m.result === "pass" ? `STEP ${m.step} · OK` : `STEP ${m.step} · ${String(m.result).toUpperCase()}`;
          show({ kind: "verdict", tone: m.result, head, text: m.say || "" }, { topic: `step${m.step}:verdict:${m.result}` });
          if (m.next) {
            setStep(m.next);
            window.setTimeout(() => scheduler.say({ text: `Next. ${m.next}`, kind: "instruction", severity: "major" }), 300);
          }
        } else if (m.type === "run" && m.run) {
          setRun(m.run as GuidedRun);
          const cur = m.run.status === "active" ? m.run.steps?.[m.run.step] : null;
          if (cur?.instruction) setStep(cur.instruction);
          (m.say || []).forEach((s: SayItem) => scheduler.say(s));
        } else if (m.type === "say" && Array.isArray(m.say)) {
          m.say.forEach((s: SayItem) => scheduler.say(s));
        } else if (m.type === "reading" || m.type === "text" || m.type === "instrument") {
          const label = m.name ? String(m.name).toUpperCase() : m.type === "text" ? "TEXT" : "DISPLAY";
          const how = m.method ? String(m.method).toUpperCase() : "OCR";
          setReading({
            text: m.type === "text" ? `TEXT “${String(m.text || "").slice(0, 40)}”` : `${label} ${m.value ?? "—"} ${m.unit || ""}`.trim() + ` · ${how}${typeof m.confidence === "number" ? ` ${m.confidence.toFixed(2)}` : ""}`,
            t: Date.now(),
            confirm: !!m.needs_confirm,
          });
        } else if (m.type === "alert") {
          show({ kind: "alert", tone: m.severity, head: `${String(m.kind).toUpperCase()}${m.severity === "danger" ? " · DANGER" : ""}`, text: m.say || m.title });
        }
      };
      s.onclose = () => {
        sock.current = null;
        if (!closed && retry++ < 5) window.setTimeout(connect, 1500 * retry);
      };
      sock.current = s;
    };
    connect();
    return () => {
      closed = true;
      sock.current?.close();
      sock.current = null;
    };
  }, [state, t, show]);

  const start = async (face = facing) => {
    setError("");
    setState("starting");
    try {
      stopCamera(stream.current);
      stream.current = await openCamera(face);
      if (video.current) {
        video.current.srcObject = stream.current;
        await video.current.play().catch(() => {});
      }
      release.current();
      release.current = await keepAwake();
      speaker.context();                         // unlock audio on this tap (iOS)
      setState("live");
    } catch (e) {
      setError(errText(e));
      setState("idle");
    }
  };

  // the fast layer on the phone itself: objects and hands, drawn live
  useEffect(() => {
    const el = video.current;
    if (state !== "live" || !el || !pref("detect", true)) {
      perceiver.current?.stop();
      setScene(null);
      return;
    }
    let gone = false;
    const p = perceiver.current || (perceiver.current = new Perceiver());
    p.maxFps = 12;
    let lastUi = 0;
    p.onScene = (sc) => {
      if (layer.current) drawScene(layer.current, sc, mediaRect(el), facing === "user");
      const ws = watcher.current.update(sc, { run: runRef.current, cameraId: infoId.current });
      const now = performance.now();
      if (now - lastUi > 300 || Object.values(ws.zones).includes("warn")) {
        lastUi = now;
        setScene(sc);
        setWatch({ ...ws });
      }
    };
    if (localStorage.getItem("irris.testHooks") === "1") {
      (window as any).__irris = { scheduler, watcher: watcher.current, inject: (s: any) => p.inject(s), setRun: (r: GuidedRun) => setRun(r) };
    }
    p.init().then((ok) => !gone && ok && p.start(el));
    return () => {
      gone = true;
      p.stop();
    };
  }, [state, facing]);
  useEffect(() => () => perceiver.current?.close(), []);

  const pause = () => {
    if (rec.current?.recording) toggleRecord();
    stopCamera(stream.current);
    stream.current = null;
    release.current();
    setState("paused");
  };

  useInterval(async () => {
    if (state !== "live" || !video.current) return;
    const blob = await grabJpeg(video.current, 960, 0.75);
    if (!blob) return;
    const s = sock.current;
    if (s && s.readyState === WebSocket.OPEN) {
      if (s.bufferedAmount < 2_000_000) {
        s.send(await blob.arrayBuffer());
        setSent((n) => n + 1);
        setLastOk(Date.now());
      }
      return;
    }
    try {
      const r = await fetch(`/api/v1/cam/frame?t=${encodeURIComponent(t)}`, { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } });
      if (r.status === 401) {
        pause();
        setError("This phone was unpaired. Pair it again from Cameras in Irris.");
        return;
      }
      if (r.ok) {
        setSent((n) => n + 1);
        setLastOk(Date.now());
      }
    } catch {
      /* offline for a moment: keep trying */
    }
  }, state === "live" ? 1000 : null);

  // ---------------------------------------------------------------- recording what this phone sees
  const overlay = (): Overlay => {
    const last = said[said.length - 1];
    return {
      title: info?.name || "Irris",
      mirrored: facing === "user",
      step: step ? { k: "Next step", text: step } : null,
      verdict: last ? { tone: last.tone === "pass" ? "pass" : last.tone === "danger" || last.tone === "fail" ? "fail" : "unsure", head: last.head, text: last.text } : null,
      captions: [],
    };
  };
  const overlayRef = useRef(overlay);
  overlayRef.current = overlay;

  const toggleRecord = async () => {
    if (rec.current?.recording) {
      const res = await rec.current.stop();
      setRecording(false);
      recMic.current?.getTracks().forEach((tr) => tr.stop());
      recMic.current = null;
      if (!res) return;
      const url = URL.createObjectURL(res.blob);
      const name = `irris-${(info?.name || "phone").toLowerCase().replace(/\s+/g, "-")}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.${res.mime.includes("mp4") ? "mp4" : "webm"}`;
      setClip({ url, mime: res.mime, name, status: { state: "uploading", text: `${mmss(res.ms)} clip · uploading…` } });
      try {
        const up = await uploadRecording(res.blob, { pairToken: t, review: true });
        setClip((c) => c && { ...c, status: { state: "saved", text: up.job ? "Saved. Irris is reviewing it." : up.run_id ? "Saved to the run as evidence." : "Saved to the workspace." } });
      } catch (e) {
        setClip((c) => c && { ...c, status: { state: "failed", text: `${errText(e)} Save it to this phone instead.` } });
      }
      return;
    }
    try {
      recMic.current = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      recMic.current = null;
    }
    const mic = recMic.current;
    const r = new LiveRecorder({
      source: () => video.current,
      overlay: () => overlayRef.current(),
      box: () => document.body.getBoundingClientRect(),
      cameraStream: () => stream.current,
      mic: () => mic,
    });
    try {
      await r.start(pref("recView", true) ? "view" : "camera");
      rec.current = r;
      setRecording(true);
      setClip(null);
    } catch (e) {
      mic?.getTracks().forEach((tr) => tr.stop());
      setError(errText(e));
    }
  };
  useInterval(() => setRecClock(rec.current?.elapsed || 0), recording ? 500 : null);

  const healthy = state === "live" && Date.now() - lastOk < 4000;

  return (
    <div className="camera-page page-rec">
      <video ref={video} playsInline muted autoPlay style={{ opacity: state === "live" ? 1 : 0, transform: facing === "user" ? "scaleX(-1)" : undefined }} />
      {state === "live" && <canvas ref={layer} className="perceive-layer" aria-hidden="true" />}
      {state === "live" && run?.status === "active" && (() => {
        const r = mediaRect(video.current);
        const zones = run.steps.map((s, i) => ({ s, i })).filter(({ s, i }) => (s.zone?.polygon?.length || 0) >= 3
          && (!s.zone!.camera_id || !info?.id || s.zone!.camera_id === info.id) && (i === run.step || watch?.zones[i] === "warn"));
        if (!r || !zones.length) return null;
        return (
          <div className="pic-layer" style={{ left: r.left, top: r.top, width: r.width, height: r.height }} aria-hidden="true">
            <svg viewBox="0 0 1 1" preserveAspectRatio="none" style={{ transform: facing === "user" ? "scaleX(-1)" : undefined }}>
              {zones.map(({ s, i }) => (
                <polygon key={i} className={`zone${watch?.zones[i] === "warn" ? " warn" : ""}${i === run.step ? "" : " other"}`}
                         points={s.zone!.polygon.map((p) => p.join(",")).join(" ")} vectorEffect="non-scaling-stroke" />
              ))}
            </svg>
          </div>
        );
      })()}
      {flash > 0 && Date.now() - flash < 1500 && <div key={flash} className="warn-flash" aria-hidden="true" />}
      <div className="top">
        <Mark size={18} />
        <span style={{ fontWeight: 600, letterSpacing: "0.3em", fontSize: 12 }}>IRRIS</span>
        <div className="grow" />
        {state === "live" && (
          <span className="mono row" style={{ fontSize: 11, gap: 6 }}>
            <span className={`dot ${healthy ? "live" : ""}`} /> {recording ? `REC ${mmss(recClock)}` : healthy ? "STREAMING" : "CONNECTING"} · {sent}
          </span>
        )}
      </div>
      <div className="bottom">
        {info && <div className="mono" style={{ fontSize: 11, color: "#A3A3A8", letterSpacing: "0.06em" }}>{info.workspace.toUpperCase()} · {info.name.toUpperCase()}</div>}
        {state === "live" && run?.status === "active" && run.steps[run.step] && (
          <div className="cam-step"><span className="k">STEP {run.step + 1} OF {run.steps.length}</span>{run.steps[run.step].instruction}</div>
        )}
        {state === "live" && (scene || reading) && (
          <div className="sense" style={{ justifyContent: "center" }} role="status">
            {scene && <span className="act">{scene.activity.toUpperCase()}</span>}
            {watch?.lastTouched && <span className={`held${Object.values(watch.zones).includes("warn") ? " warn" : ""}`}>HELD · {watch.lastTouched.toUpperCase()}</span>}
            {reading && Date.now() - reading.t < 6000 && <span className="read">{reading.text.includes(" · ") ? reading.text : `${reading.text} · OCR`}{reading.confirm ? " · CONFIRM ON SCREEN" : ""}</span>}
          </div>
        )}
        {state === "live" && said.length > 0 && (
          <div className="stack sm" style={{ alignItems: "center", width: "100%" }} aria-live="polite">
            {said.slice(-2).map((s) => (
              <div key={s.id} className={`say ${s.tone}`}><span className="k">{s.head}</span>{s.text}</div>
            ))}
          </div>
        )}
        {error ? (
          <div className="big" style={{ color: "#FF6A5C", maxWidth: 360 }}>{error}</div>
        ) : state === "live" ? (
          !said.length && <div className="big">{healthy ? "Irris can see. Keep this page open." : "Connecting…"}</div>
        ) : state === "starting" ? (
          <div className="row"><Spinner /> Opening the camera…</div>
        ) : info ? (
          <div className="stack" style={{ alignItems: "center" }}>
            <div className="big">Point this phone at the work.</div>
            <p style={{ margin: 0, color: "#A3A3A8", maxWidth: 320 }}>It sends one frame a second to {info.workspace}. Irris's checks and alerts show and speak here, and your team follows on their screens.</p>
          </div>
        ) : !error ? (
          <div className="row"><Spinner /> Checking the link…</div>
        ) : null}
        {info && (
          <div className="cam-btns" style={{ marginTop: 6 }}>
            {state === "live" ? (
              <>
                <button className="btn lg" onClick={pause}><Pause /> Pause</button>
                {canRecord() && (
                  <button className={`btn lg rec-btn${recording ? " on" : ""}`} onClick={toggleRecord} aria-pressed={recording} aria-label={recording ? "Stop recording" : "Record what you see"}>
                    <Video /> {recording ? "Stop" : "Record"}
                  </button>
                )}
                <button className="btn lg icon" aria-label={talk ? "Mute Irris" : "Let Irris speak"} onClick={() => { const v = !talk; setTalk(v); setPref("camSpeak", v); if (!v) speaker.stop(); }}>{talk ? <Volume2 /> : <VolumeX />}</button>
                <button className="btn lg icon" aria-label="Switch camera" onClick={() => { const f = facing === "environment" ? "user" : "environment"; setFacing(f); start(f); }}><RefreshCw /></button>
              </>
            ) : (
              <button className="btn lg" style={{ background: "#D7FF3D", color: "#0A0A0B", borderColor: "#D7FF3D" }} onClick={() => start()} disabled={state === "starting"}>
                {state === "paused" ? <Play /> : <Camera />} {state === "paused" ? "Resume" : "Start camera"}
              </button>
            )}
          </div>
        )}
      </div>
      {clip && <RecordingPreview url={clip.url} mime={clip.mime} name={clip.name} status={clip.status} onClose={() => { URL.revokeObjectURL(clip.url); setClip(null); }} />}
    </div>
  );
}
