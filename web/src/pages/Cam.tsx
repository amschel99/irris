import { Camera, Pause, Play, RefreshCw, Video, Volume2, VolumeX } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { RecordingPreview } from "../components/media";
import { Mark, Spinner } from "../components/ui";
import { grabJpeg, keepAwake, openCamera, stopCamera } from "../lib/camera";
import { uploadRecording } from "../lib/media";
import { canRecord, LiveRecorder, mmss, type Overlay } from "../lib/recorder";
import { errText, useInterval } from "../lib/util";
import { pref, setPref, speaker } from "../lib/voice";

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
  const [info, setInfo] = useState<{ name: string; workspace: string } | null>(null);
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

  // her voice on this phone comes from the workspace's TTS, through the pairing token
  useEffect(() => {
    speaker.ttsUrl = `/api/v1/cam/tts?t=${encodeURIComponent(t)}`;
    speaker.headers = () => ({});
    return () => speaker.stop();
  }, [t]);

  const show = useCallback((s: Omit<Said, "id">) => {
    setSaid((cur) => [...cur.slice(-2), { ...s, id: Date.now() + Math.random() }]);
    if (talkRef.current && s.text) speaker.say(s.text, { interrupt: s.tone === "danger" || s.kind === "verdict" });
  }, []);

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
          show({ kind: "verdict", tone: m.result, head, text: m.say || "" });
          if (m.next) {
            setStep(m.next);
            window.setTimeout(() => talkRef.current && speaker.say(`Next. ${m.next}`, { interrupt: false }), 300);
          }
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
