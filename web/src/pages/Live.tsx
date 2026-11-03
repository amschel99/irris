import {
  AlertTriangle, Aperture, Camera as CamIcon, Check, ChevronLeft, CircleStop, Crop, Gauge, Hexagon, History, LifeBuoy, Mic,
  MonitorUp, MoreHorizontal, Play, ScanLine, ScanEye, SkipForward, Sparkles, Undo2, Video, Volume2, VolumeX, X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Answer } from "../components/Answer";
import { RecordingPreview, VisionBadge } from "../components/media";
import { Empty, ErrorBox, Field, Loading, ResultTag, Spinner, Toggle } from "../components/ui";
import { api, del, frameUrl, get, getWorkspaceId, post, put } from "../lib/api";
import { grabJpeg, openCamera, stopCamera } from "../lib/camera";
import { fileUrl, mediaUrl, subscribeAlerts, uploadRecording, useJob, useVisionStatus, type Alert, type CameraState } from "../lib/media";
import { canRecord, canRecordScreen, LiveRecorder, mmss, type Overlay, type RecMode } from "../lib/recorder";
import { useSession } from "../lib/session";
import type { Camera, Measured, Procedure, Run } from "../lib/types";
import { deviceName, duration, errText, num, useInterval, useLoad } from "../lib/util";
import { drawScene, Perceiver, sceneSentence, type Scene, type SceneEvent } from "../lib/perceive";
import { HandsFree, parseCommand, pref, setPref, speaker } from "../lib/voice";
import {
  createInstrument, evidenceText, labelMatches, listInstruments, patchStep, prereqsDone, readInstrument, recover, Reminders,
  stepStatus, votesFor, Watcher, type GuidedRun, type GuidedRunStep, type Instrument, type InstrumentReading, type Pt,
  type WatchState,
} from "../lib/guidance";
import { scheduler, type SayItem, type Severity } from "../lib/scheduler";
import { Composer, Thinking, useAsk } from "./Ask";
import "../guidance.css";

const DEVICE = "device";
const GRID = "grid";
const AUTO_MS = 8000;

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Where the camera picture actually sits inside its element (object-fit: contain leaves bars). */
function contentRect(el: HTMLVideoElement | HTMLImageElement | null): DOMRect | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const nw = el instanceof HTMLVideoElement ? el.videoWidth : el.naturalWidth;
  const nh = el instanceof HTMLVideoElement ? el.videoHeight : el.naturalHeight;
  if (!nw || !nh || !r.width || !r.height) return null;
  const k = Math.min(r.width / nw, r.height / nh);
  const w = nw * k;
  const h = nh * k;
  return new DOMRect(r.left + (r.width - w) / 2, r.top + (r.height - h) / 2, w, h);
}

interface Verdict {
  result: string;
  say: string;
  reading: (Measured & { method?: string; note?: string; verified?: boolean }) | null;
  at: number;
  where?: "gpu" | "cpu";
  ms?: number;
}

interface ClipState {
  url: string;
  mime: string;
  name: string;
  jobId?: string;
  status: { state: "uploading" | "saved" | "failed" | "reviewing" | "reviewed"; text: string; link?: string };
}

interface Cap {
  id: number;
  who: "irris" | "you";
  text: string;
}

function specText(r?: { min?: number; max?: number; unit?: string; label?: string }) {
  if (!r) return "";
  const u = r.unit ? ` ${r.unit}` : "";
  if (r.min !== undefined && r.max !== undefined) return `${r.min}–${r.max}${u}`;
  if (r.min !== undefined) return `≥ ${r.min}${u}`;
  if (r.max !== undefined) return `≤ ${r.max}${u}`;
  return "";
}

function lookError(e: unknown): string {
  const t = errText(e);
  if (/in time|timeout|504/i.test(t)) return "I couldn't see in time. The vision model is slow on the server's CPU right now; try again, or mark the step by hand.";
  if (/busy/i.test(t)) return "I'm still looking at the last thing. Give me a moment.";
  return t;
}

const secs = (ms?: number) => (ms === undefined ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);

export interface LiveMsg {
  type: string;
  value?: number;
  unit?: string;
  text?: string;
  confidence?: number;
  t?: number;
}

/** The camera's WebSocket. This device: its frames go up (HTTP POSTs when the socket can't open). Any camera: live
 * readings (OCR of displays), verdicts and alerts come down. */
function useCameraFeed(cameraId: string, active: boolean, video: React.RefObject<HTMLVideoElement>, send: boolean,
                       onMessage: (m: LiveMsg) => void) {
  const ws = useRef<WebSocket | null>(null);
  const [mode, setMode] = useState<"ws" | "http" | "">("");
  const handler = useRef(onMessage);
  handler.current = onMessage;
  useEffect(() => {
    if (!active || !cameraId) return;
    let closed = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      const sock = new WebSocket(`${proto}://${location.host}/api/v1/cam/ws?camera=${cameraId}&ws=${encodeURIComponent(getWorkspaceId())}`);
      sock.binaryType = "arraybuffer";
      sock.onmessage = (e) => {
        if (typeof e.data !== "string") return;
        try {
          handler.current(JSON.parse(e.data));
        } catch {
          /* not JSON */
        }
      };
      sock.onopen = () => !closed && setMode("ws");
      sock.onclose = () => { if (!closed) setMode("http"); ws.current = null; };
      sock.onerror = () => !closed && setMode("http");
      ws.current = sock;
    } catch {
      setMode("http");
    }
    return () => {
      closed = true;
      ws.current?.close();
      ws.current = null;
      setMode("");
    };
  }, [cameraId, active]);

  useInterval(async () => {
    if (!send || !active || !cameraId || !video.current) return;
    const blob = await grabJpeg(video.current);
    if (!blob) return;
    const sock = ws.current;
    if (sock && sock.readyState === WebSocket.OPEN) {
      if (sock.bufferedAmount < 2_000_000) sock.send(await blob.arrayBuffer());
      return;
    }
    api(`/cameras/${cameraId}/frame`, { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } }).catch(() => {});
  }, send && active && cameraId ? 1000 : null);
  return mode;
}

const VISUAL = /\b(see|look|looking|camera|holding|doing|this|that|these|those|here|read|reading|display|gauge|right|wrong|correct)\b/i;

/** A short, low tone before a critical warning: heard over machine noise, faster than words. */
function beep() {
  try {
    const ac = speaker.context();
    const o = ac.createOscillator();
    const g = ac.createGain();
    o.type = "sine";
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

function ProgressRing({ value, label }: { value: number; label: string | number }) {
  const r = 10.5;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(1, value || 0));
  return (
    <span className="n ring" aria-label={`In progress, ${Math.round(v * 100)}%`}>
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r={r} className="track" />
        <circle cx="12" cy="12" r={r} className="fill" strokeDasharray={`${c * v} ${c}`} transform="rotate(-90 12 12)" />
      </svg>
      <span>{label}</span>
    </span>
  );
}

const severityOf = (result: string): Severity => (result === "danger" ? "critical" : result === "pass" ? "major" : result === "fail" ? "major" : "minor");

interface InstFlow {
  phase: "roi" | "type" | "center" | "min" | "max" | "values";
  roi?: [number, number, number, number];
  type: "digital" | "analog";
  name: string;
  unit: string;
  format: string;
  center?: Pt;
  min?: Pt;
  max?: Pt;
  vmin: string;
  vmax: string;
  saving?: boolean;
}

const FLOW_HINT: Record<InstFlow["phase"], string> = {
  roi: "Drag over the gauge or display · Esc to cancel",
  type: "",
  center: "Tap the centre of the dial",
  min: "Tap the lowest mark on the scale",
  max: "Tap the highest mark on the scale",
  values: "",
};

export function Live() {
  const { workspace, toast } = useSession();
  const [params, setParams] = useSearchParams();
  const cams = useLoad<{ cameras: Camera[] }>(() => get("/cameras"), [workspace?.id]);
  const procs = useLoad<{ procedures: Procedure[] }>(() => get("/procedures"), [workspace?.id]);
  const vstatus = useVisionStatus(10000);

  const [source, setSource] = useState<string>(params.get("camera") || "");
  const [deviceCam, setDeviceCam] = useState<string>("");
  const [camError, setCamError] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [facing, setFacing] = useState<"environment" | "user">("environment");
  const videoRef = useRef<HTMLVideoElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [tick, setTick] = useState(0);
  const [imgOk, setImgOk] = useState(false);

  const [run, setRun] = useState<GuidedRun | null>(null);
  const runRef = useRef<GuidedRun | null>(null);
  runRef.current = run;
  const [runLoading, setRunLoading] = useState(!!params.get("run"));
  const [procId, setProcId] = useState("");
  const [label, setLabel] = useState("");
  const [checking, setChecking] = useState(false);
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [auto, setAuto] = useState(false);
  const [voice, setVoice] = useState(() => pref("speakVerdicts", true));
  const [tab, setTab] = useState<"steps" | "ask">("steps");
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState("");
  const [note, setNote] = useState("");
  const chat = useAsk();
  const [conv, setConv] = useState<string | null>(null);

  const [caps, setCaps] = useState<Cap[]>([]);
  const [banner, setBanner] = useState<Alert | null>(null);
  const [menu, setMenu] = useState(false);
  const [camState, setCamState] = useState<CameraState | null>(null);
  const [goal, setGoal] = useState("");
  const [reviewJob, setReviewJob] = useState<string | null>(null);
  const job = useJob(reviewJob);

  const [rois, setRois] = useState<{ key: string; box: Box }[]>([]);
  const [drawing, setDrawing] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [hands, setHands] = useState(false);
  const [level, setLevel] = useState(0);
  const handsRef = useRef<HandsFree | null>(null);

  // the fast layer: on-device detection, live readings, narration
  const perceiver = useRef<Perceiver | null>(null);
  const overlayCanvas = useRef<HTMLCanvasElement>(null);
  const sceneRef = useRef<Scene | null>(null);
  const [scene, setScene] = useState<Scene | null>(null);
  const [detect, setDetect] = useState(() => pref("detect", true));
  const [detectError, setDetectError] = useState("");
  const [narrate, setNarrate] = useState(() => pref("narrate", true));
  const [liveRead, setLiveRead] = useState<LiveMsg | null>(null);
  const [looking, setLooking] = useState<{ since: number; what: string } | null>(null);
  const [lookClock, setLookClock] = useState(0);
  const narrated = useRef<{ text: string; at: number; lookAt: number }>({ text: "", at: 0, lookAt: 0 });
  const autoHands = useRef(false);

  // Guidance v2: warnings before the mistake, reminders, zones, instruments, recovery
  const watcher = useRef(new Watcher());
  const reminders = useRef(new Reminders());
  const [watch, setWatch] = useState<WatchState | null>(null);
  const userTalking = useRef(false);
  const lastHeard = useRef(0);
  const [zoneEdit, setZoneEdit] = useState<{ step: number; points: Pt[] } | null>(null);
  const dragPoint = useRef<number | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [flow, setFlow] = useState<InstFlow | null>(null);
  const [flowDrag, setFlowDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [watchInst, setWatchInst] = useState<string>("");
  const [instRead, setInstRead] = useState<{ id: string; r: InstrumentReading; t: number; confirmed?: boolean } | null>(null);
  const instBusy = useRef(false);
  const [, setLayout] = useState(0);

  const [recMode, setRecMode] = useState<"view" | "camera">(() => (pref("recView", true) ? "view" : "camera"));
  const [recording, setRecording] = useState(false);
  const [recClock, setRecClock] = useState(0);
  const recorderRef = useRef<LiveRecorder | null>(null);
  const [clip, setClip] = useState<ClipState | null>(null);
  const clipJob = useJob(clip?.status.state === "reviewing" ? clip.jobId || null : null);
  const ownMic = useRef<MediaStream | null>(null);

  const cameraId = source === DEVICE ? deviceCam : source === GRID ? "" : source;
  const camRef = useRef("");
  camRef.current = cameraId;
  const cameraList = cams.data?.cameras || [];
  const remote = cameraList.find((c) => c.id === source);
  const live = source === DEVICE ? streaming : !!remote && (remote.kind === "url" || imgOk || !!remote.live);
  const onLive = useCallback((m: LiveMsg & { run?: GuidedRun; say?: SayItem[] }) => {
    if (m.type === "reading" || m.type === "text") setLiveRead({ ...m, t: Date.now() });
    // the tracker pushes the run as it changes, with the lines it suggests saying
    if (m.type === "run" && m.run && runRef.current && m.run.id === runRef.current.id) {
      // pushes are a slim view (no log, fewer step fields): merge them into the full run, step by step
      const pushed = m.run as Run;
      setRun((prev) => prev && {
        ...prev, ...pushed, log: prev.log,
        steps: (pushed.steps || prev.steps).map((st, i) => { const uid = (st as { uid?: string }).uid; return { ...((uid && prev.steps.find((x) => (x as { uid?: string }).uid === uid)) || prev.steps[i] || {}), ...st }; }),
      });
    }
    if ((m.type === "run" || m.type === "say") && Array.isArray(m.say)) m.say.forEach((s) => scheduler.say(s));
  }, []);
  const sendMode = useCameraFeed(cameraId, live && source !== GRID, videoRef, source === DEVICE && streaming, onLive);
  const expected = vstatus?.expected_s || (vstatus?.active?.where === "gpu" ? 3 : 30);

  const caption = useCallback((who: Cap["who"], text: string) => {
    if (!text.trim()) return;
    setCaps((c) => [...c.slice(-4), { id: Date.now() + Math.random(), who, text: text.trim() }]);
  }, []);

  // everything Irris says here goes through the scheduler (critical now, major after a pause, minor later or never)
  const speakOn = useRef(false);
  useEffect(() => {
    speaker.onCaption = null;
    scheduler.onCaption = (full) => caption("irris", full);
    scheduler.enabled = () => speakOn.current;
    scheduler.ears = { speaking: () => userTalking.current, lastHeard: () => lastHeard.current };
    scheduler.cleared = (item) => {
      const r = runRef.current;
      if (item.step === undefined || !r) return false;
      if (r.status !== "active") return true;
      const s = r.steps[item.step];
      if (!s) return true;
      if (["complete", "skipped"].includes(stepStatus(r, s))) return true;
      if (item.topic?.endsWith(":wrong-object")) {
        const held = watcher.current.lastTouched;
        return !held || labelMatches(held, s.expected_objects || []);
      }
      if (item.topic?.endsWith(":prereq")) return prereqsDone(r, item.step).ok;
      return item.step !== r.step && item.kind !== "warning";
    };
    watcher.current.onSay = (item) => {
      if (item.severity === "critical" && speakOn.current) beep();
      scheduler.say(item);
    };
    if (localStorage.getItem("irris.testHooks") === "1") {
      (window as any).__irris = {
        scheduler, watcher: watcher.current, reminders: reminders.current,
        inject: (s: any) => perceiver.current?.inject(s),
        ears: (talking: boolean) => { userTalking.current = talking; if (talking) lastHeard.current = performance.now(); },
      };
    }
    return () => {
      scheduler.flush();
      scheduler.onCaption = null;
      scheduler.cleared = () => false;
      scheduler.enabled = () => true;
      scheduler.ears = { speaking: () => false, lastHeard: () => 0 };
      watcher.current.onSay = null;
      speaker.stop();
    };
  }, [caption]);

  // default source: a live remote camera, else this device
  useEffect(() => {
    if (source || !cams.data) return;
    const liveCam = cams.data.cameras.find((c) => c.live || c.kind === "url");
    setSource(liveCam ? liveCam.id : DEVICE);
  }, [cams.data, source]);

  // camera state (goal, coach, notice) for the selected camera
  useEffect(() => {
    setCamState(null);
    if (!cameraId) return;
    get<CameraState>(`/cameras/${cameraId}/state`).then((s) => { setCamState(s); setGoal(s.goal); }).catch(() => {});
    get<{ rois: { key: string; box: Box }[] }>(`/cameras/${cameraId}/rois`).then((r) => setRois(r.rois)).catch(() => setRois([]));
  }, [cameraId]);

  // alerts for this camera show as a banner on the stage (the alert center speaks them)
  useEffect(() => {
    if (!workspace?.id) return;
    return subscribeAlerts((a) => {
      if (a.kind === "job" || (cameraId && a.camera_id && a.camera_id !== cameraId)) return;
      if (a.kind === "coach" || a.kind === "notice") {
        caption("irris", a.say);
        return;
      }
      setBanner(a);
      window.setTimeout(() => setBanner((b) => (b?.id === a.id ? null : b)), a.severity === "danger" ? 20000 : 10000);
    });
  }, [workspace?.id, cameraId, caption]);

  const startDevice = useCallback(async (face = facing) => {
    setCamError("");
    try {
      stopCamera(streamRef.current);
      const stream = await openCamera(face);
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play().catch(() => {});
      }
      let id = deviceCam;
      if (!id) {
        const name = deviceName();
        const existing = (await get<{ cameras: Camera[] }>("/cameras")).cameras.find((c) => c.kind === "phone" && c.name === name);
        id = existing ? existing.id : (await post("/cameras", { kind: "phone", name })).id;
        setDeviceCam(id);
        cams.reload();
      }
      setStreaming(true);
    } catch (e) {
      setCamError(errText(e));
      setStreaming(false);
    }
  }, [deviceCam, cams, facing]);

  useEffect(() => {
    if (source !== DEVICE) {
      stopCamera(streamRef.current);
      streamRef.current = null;
      setStreaming(false);
    }
  }, [source]);
  useEffect(() => () => {
    stopCamera(streamRef.current);
    handsRef.current?.stop();
    recorderRef.current?.stop();
  }, []);

  // poll a remote camera (and every camera in the grid)
  useInterval(() => setTick((t) => t + 1), source && source !== DEVICE ? 800 : null);

  // resume a run from ?run=
  useEffect(() => {
    const id = params.get("run");
    if (!id) return;
    setRunLoading(true);
    get<Run>(`/runs/${id}`)
      .then((r) => {
        setRun(r);
        if (r.camera_id && !params.get("camera")) setSource(r.camera_id);
      })
      .catch((e) => toast(errText(e), true))
      .finally(() => setRunLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = run && run.status === "active" ? run.steps[run.step] : null;
  useEffect(() => {
    setUnit(current?.reading?.unit || "");
    setValue("");
    setNote("");
  }, [run?.id, run?.step, current?.reading?.unit]);

  speakOn.current = voice || hands;
  /** Step instructions and replies: spoken whole, after the person pauses. */
  const say = useCallback((text: string, item: Partial<SayItem> = {}) => {
    scheduler.say({ text, kind: "instruction", severity: "major", ...item });
  }, []);
  const sayAll = (items?: SayItem[] | null) => (items || []).forEach((s) => scheduler.say(s));

  /** A verdict: asked for (a reply, always spoken) or from auto-check (rate-limited, timed by severity). */
  const announce = useCallback((v: Verdict, asked = false) => {
    setVerdict(v);
    if (!v.say) return;
    const step = runRef.current?.step;
    scheduler.say({
      text: v.say,
      kind: asked ? "reply" : "verdict",
      severity: severityOf(v.result),
      topic: `step${step ?? "x"}:verdict:${v.result}`,
      step: asked ? undefined : step,
    });
  }, []);

  const start = async () => {
    if (!procId) return;
    try {
      const r = await post<Run>("/runs", { procedure_id: procId, camera_id: cameraId || "", label });
      setRun(r);
      setVerdict(null);
      setParams((p) => { p.set("run", r.id); return p; }, { replace: true });
      say(`Step 1. ${r.steps[0].instruction}`);
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const check = useCallback(async () => {
    if (!run || run.status !== "active" || checking) return;
    setChecking(true);
    try {
      setLooking({ since: Date.now(), what: "Checking the step" });
      const out = await post<{ check: Verdict; run: GuidedRun; say?: SayItem[] }>(`/runs/${run.id}/check`, { camera_id: cameraId || undefined, scene: sceneRef.current?.summary });
      announce({ ...out.check, at: Date.now() }, true);
      const advanced = out.run.step !== run.step || out.run.status !== run.status;
      setRun(out.run);
      sayAll(out.say || out.run.say);
      if (advanced && out.run.status === "active") window.setTimeout(() => say(`Next. ${out.run.steps[out.run.step].instruction}`), 1600);
      if (out.run.status === "done") window.setTimeout(() => say("That's the last step. The batch record is written."), 1600);
    } catch (e) {
      setAuto(false);
      announce({ result: "unsure", say: lookError(e), reading: null, at: Date.now() }, true);
    } finally {
      setChecking(false);
      setLooking(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, checking, cameraId, announce, say]);

  const autoCheck = useCallback(async () => {
    if (!run || run.status !== "active" || checking) return;
    setChecking(true);
    try {
      const out = await post<{ check: Verdict & { skipped?: boolean }; run: GuidedRun; say?: SayItem[] }>(`/runs/${run.id}/check/auto`, { camera_id: cameraId || undefined, scene: sceneRef.current?.summary });
      sayAll(out.say || out.run?.say);
      if (out.check.skipped) return;                 // nothing changed in view, or the model is busy: try next time
      announce({ ...out.check, at: Date.now() });
      const advanced = out.run.step !== run.step || out.run.status !== run.status;
      setRun(out.run);
      if (advanced && out.run.status === "active") window.setTimeout(() => say(`Next. ${out.run.steps[out.run.step].instruction}`), 1600);
    } catch (e) {
      setAuto(false);
      announce({ result: "unsure", say: errText(e), reading: null, at: Date.now() });
    } finally {
      setChecking(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, checking, cameraId, announce, say]);

  useInterval(autoCheck, auto && live && run?.status === "active" ? AUTO_MS : null);

  const mark = async (result: "pass" | "fail" | "skip") => {
    if (!run) return;
    const reading = value.trim() ? { value: Number(value), unit } : undefined;
    if (reading && Number.isNaN(reading.value)) return toast("The reading must be a number", true);
    try {
      const out = await post<{ check: Verdict; run: GuidedRun; say?: SayItem[] }>(`/runs/${run.id}/check`, { result, reading, note: note || undefined });
      setRun(out.run);
      const said = out.check.result === "fail" && result === "pass" ? `Out of spec: ${out.check.say}` : out.check.say;
      announce({ ...out.check, say: said || (result === "pass" ? "Step passed." : result === "skip" ? "Step skipped." : "Marked as failed."), at: Date.now() }, true);
      sayAll(out.say || out.run.say);
      if (out.run.status === "active" && out.run.step !== run.step) window.setTimeout(() => say(`Next. ${out.run.steps[out.run.step].instruction}`), 1200);
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const move = async (action: "back" | "stop", confirmIt = true) => {
    if (!run) return;
    if (action === "stop" && confirmIt && !confirm("Stop this run? Irris writes the batch record with the steps done so far.")) return;
    try {
      const r = await post<Run>(`/runs/${run.id}/${action}`);
      setRun(r);
      setVerdict(null);
      if (action === "back" && r.status === "active") say(`Back to step ${r.step + 1}. ${r.steps[r.step].instruction}`);
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const reset = () => {
    setRun(null);
    setVerdict(null);
    setAuto(false);
    setParams((p) => { p.delete("run"); return p; }, { replace: true });
  };

  const sendChat = useCallback(async (text: string, spoken = false) => {
    caption("you", text);
    const visual = !!cameraId && live && VISUAL.test(text);
    if (visual && (spoken || voice || hands)) {
      // an instant first answer from the on-device detector while the server takes a proper look
      const quick = sceneSentence(sceneRef.current);
      scheduler.say({ text: quick ? `${quick}. Let me take a closer look.` : "Let me take a look.", kind: "reply", severity: "major" });
    }
    if (visual) setLooking({ since: Date.now(), what: "Looking" });
    try {
      const res = await chat.send(text, { camera_id: cameraId || undefined, run_id: run?.id, scene: sceneRef.current?.summary }, conv);
      if (res?.conversation_id) setConv(res.conversation_id);
      if (res?.reply) {
        if (spoken && !speakOn.current) speaker.say(res.reply);        // a spoken question always gets a spoken answer
        else scheduler.say({ text: res.reply, kind: "reply", severity: "major" });
      }
    } finally {
      if (visual) setLooking(null);
    }
  }, [chat, cameraId, run?.id, conv, caption, voice, hands, live]);

  // ---------------------------------------------------------------- tools on the camera
  const saveCamState = async (body: Partial<CameraState>) => {
    if (!cameraId) return;
    try {
      const s = await put<CameraState>(`/cameras/${cameraId}/state`, body);
      setCamState((c) => ({ ...(c || {}), ...s }));
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const snapshot = async () => {
    if (!cameraId) return;
    try {
      await post(`/cameras/${cameraId}/snapshot`);
      toast("Snapshot saved to Data");
    } catch (e) {
      toast(errText(e), true);
    }
  };

  const readNow = async () => {
    if (!cameraId) return;
    setChecking(true);
    setLooking({ since: Date.now(), what: "Reading" });
    try {
      const out = await post<{ say: string; reading: Verdict["reading"]; status: string; where?: "gpu" | "cpu"; ms?: number }>(
        `/cameras/${cameraId}/read`, { what: current?.reading?.label || "the instrument in view", spec: current?.reading });
      announce({ result: out.status === "ok" ? "pass" : out.status === "danger" ? "danger" : "unsure", say: out.say, reading: out.reading, at: Date.now(), where: out.where, ms: out.ms }, true);
      if (out.reading) {
        setValue(String(out.reading.value));
        if (out.reading.unit) setUnit(out.reading.unit);
      }
    } catch (e) {
      toast(lookError(e), true);
    } finally {
      setChecking(false);
      setLooking(null);
    }
  };

  const whatHappened = async () => {
    if (!cameraId) return;
    setMenu(false);
    try {
      const j = await post(`/cameras/${cameraId}/review`, { seconds: 90, procedure_id: run?.procedure.id });
      setReviewJob(j.id);
      toast("Reviewing the last 90 seconds");
    } catch (e) {
      toast(errText(e), true);
    }
  };

  // ---------------------------------------------------------------- the fast layer: on-device detection
  useEffect(() => {
    const el = source === DEVICE ? videoRef.current : imgRef.current;
    if (!detect || !live || source === GRID || !el) {
      perceiver.current?.stop();
      sceneRef.current = null;
      setScene(null);
      const cv = overlayCanvas.current;
      if (cv) cv.getContext("2d")?.clearRect(0, 0, cv.width, cv.height);
      return;
    }
    let gone = false;
    const p = perceiver.current || (perceiver.current = new Perceiver());
    let lastUi = 0;
    p.onScene = (sc) => {
      sceneRef.current = sc;
      const cv = overlayCanvas.current;
      if (cv) drawScene(cv, sc, contentRect(el), source === DEVICE && facing === "user");
      // hands against the work zones, every frame: the warning has to come before the part goes in
      const ws = watcher.current.update(sc, { run: runRef.current, cameraId: camRef.current });
      const now = performance.now();
      if (now - lastUi > 250 || Object.values(ws.zones).includes("warn")) {
        lastUi = now;
        setScene(sc);
        setWatch({ ...ws });
      }
    };
    p.init().then((ok) => {
      if (gone) return;
      if (!ok) {
        setDetectError(p.error);
        return;
      }
      setDetectError("");
      p.start(el);
    });
    return () => {
      gone = true;
      p.stop();
    };
  }, [detect, live, source, facing, imgOk, streaming]);
  useEffect(() => () => perceiver.current?.close(), []);

  // narration: the detector notices what changed; Irris says something short only when it's useful
  const narrateRef = useRef<(e: SceneEvent) => void>(() => {});
  narrateRef.current = (e: SceneEvent) => {
    if (!narrate || !(voice || hands)) return;
    const now = Date.now();
    const n = narrated.current;
    const line = e.kind === "picked" ? `You've got the ${e.label}.` : e.kind === "put-down" ? `Put the ${e.label} down.` : "";
    if (line && line !== n.text && now - n.at > 8000 && !speaker.speaking) {
      n.text = line;
      n.at = now;
      scheduler.say({ text: line, kind: "narration", severity: "major", topic: `narrate:${e.kind}:${(e as any).label || ""}` });
    }
    // a pause after work, or a new tool: one cheap look (skipped if the vision model is busy), spoken only if it matters
    const worth = e.kind === "paused" || e.kind === "picked";
    if (worth && cameraId && vlmReady && now - n.lookAt > 45000 && !checking) {
      n.lookAt = now;
      post<{ say: string; status: string }>(`/cameras/${cameraId}/look`, {
        question: current
          ? `They are on step ${run!.step + 1}: ${current.instruction}. In one short sentence: is what they're doing right, or is something off?`
          : "In one short sentence: what is the person doing, and is anything wrong or unsafe?",
        scene: sceneRef.current?.summary,
        background: true,
      }).then((out) => {
        if (!out?.say) return;
        const useful = out.status === "warning" || out.status === "danger" || e.kind === "paused";
        if (useful && out.say !== narrated.current.text) {
          narrated.current.text = out.say;
          narrated.current.at = Date.now();
          scheduler.say({
            text: out.say,
            kind: "narration",
            severity: out.status === "danger" ? "critical" : out.status === "warning" ? "major" : "minor",
            topic: "narrate:look",
            step: current ? run!.step : undefined,
          });
        }
      }).catch(() => { /* busy or slow: stay quiet */ });
    }
  };
  useEffect(() => {
    const p = perceiver.current;
    if (p) p.onEvent = (e) => narrateRef.current(e);
  }, [scene !== null]);

  // hands-free by default: once a camera is live, Irris listens (turn it off and it stays off)
  useEffect(() => {
    if (!live || source === GRID || handsRef.current || autoHands.current || !pref("handsFree", true)) return;
    autoHands.current = true;
    toggleHands(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, source]);

  useInterval(() => setLookClock(looking ? Math.round((Date.now() - looking.since) / 1000) : 0), looking ? 1000 : null);

  // ---------------------------------------------------------------- the reading area (ROI) for this step or camera
  const roiKey = run && run.status === "active" ? `${run.procedure.id}:${run.step}` : "default";
  const activeRoi = rois.find((r) => r.key === roiKey) || rois.find((r) => r.key === "default") || null;
  const mediaEl = () => (source === DEVICE ? videoRef.current : imgRef.current);
  const toFrame = (clientX: number, clientY: number) => {
    const r = contentRect(mediaEl());
    if (!r) return null;
    return { x: Math.min(1, Math.max(0, (clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (clientY - r.top) / r.height)) };
  };
  const saveRoi = async (box: Box) => {
    if (!cameraId) return;
    try {
      await put(`/cameras/${cameraId}/roi`, { key: roiKey, box, label: current?.reading?.label || "" });
      setRois((rs) => [...rs.filter((r) => r.key !== roiKey), { key: roiKey, box }]);
      toast(roiKey === "default" ? "Reading area saved for this camera" : `Reading area saved for step ${run!.step + 1}`);
    } catch (e) {
      toast(errText(e), true);
    }
  };
  const clearRoi = async () => {
    if (!cameraId || !activeRoi) return;
    await del(`/cameras/${cameraId}/roi?key=${encodeURIComponent(activeRoi.key)}`).catch(() => {});
    setRois((rs) => rs.filter((r) => r.key !== activeRoi.key));
  };
  const roiStyle = (b: Box): React.CSSProperties | undefined => {
    const r = contentRect(mediaEl());
    const stage = stageRef.current?.getBoundingClientRect();
    if (!r || !stage) return undefined;
    return { left: r.left - stage.left + b.x * r.width, top: r.top - stage.top + b.y * r.height, width: b.w * r.width, height: b.h * r.height };
  };

  // ---------------------------------------------------------------- Guidance v2: layout, zones, instruments, reminders
  const mirrored = source === DEVICE && facing === "user";
  useEffect(() => {
    const el = stageRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setLayout((n) => n + 1));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  /** The picture's box inside the stage, for layers drawn in image coordinates. */
  const pictureStyle = (): React.CSSProperties | undefined => {
    const r = contentRect(mediaEl());
    const stage = stageRef.current?.getBoundingClientRect();
    if (!r || !stage) return undefined;
    return { left: r.left - stage.left, top: r.top - stage.top, width: r.width, height: r.height };
  };
  /** Screen point -> image point (0..1), undoing the selfie mirror. */
  const toImage = (clientX: number, clientY: number): Pt | null => {
    const p = toFrame(clientX, clientY);
    if (!p) return null;
    return [mirrored ? 1 - p.x : p.x, p.y];
  };
  const dispX = (x: number) => (mirrored ? 1 - x : x);

  // reminders from typical step durations: checked every second while a run is on
  useInterval(() => {
    for (const item of reminders.current.due(runRef.current)) scheduler.say(item);
  }, run?.status === "active" ? 1000 : null);

  // a confirmed deviation comes with a recovery plan: say it once
  const announcedRecovery = useRef("");
  useEffect(() => {
    const rec = run?.recovery;
    if (!rec || announcedRecovery.current === rec.what) return;
    announcedRecovery.current = rec.what;
    scheduler.say({ text: `${rec.what}${rec.fix?.[0] ? `. ${rec.fix[0]}` : ""}`, kind: "warning", severity: "major", topic: "recovery" });
  }, [run?.recovery]);

  const acceptRecovery = async () => {
    if (!run) return;
    try {
      const r = await recover(run.id, { accept: true });
      if (r && r.id) setRun(r);
      say("Recovery steps added.", { kind: "reply" });
    } catch (e) {
      toast(errText(e), true);
    }
  };

  // zones: the work area for a step, drawn on the picture
  const editZone = () => {
    if (!run || run.status !== "active") return toast("Start a run to draw a step's work zone", true);
    if (!live) return toast("Start a camera first", true);
    setMenu(false);
    setFlow(null);
    const z = run.steps[run.step]?.zone;
    setZoneEdit({ step: run.step, points: z?.polygon ? [...z.polygon] : [] });
  };
  const saveZone = async (clear = false) => {
    if (!run || !zoneEdit) return;
    const zone = clear ? null : { camera_id: cameraId, polygon: zoneEdit.points };
    if (!clear && zoneEdit.points.length < 3) return toast("A zone needs at least 3 points", true);
    try {
      await patchStep(run.procedure.id, zoneEdit.step, { zone });
      setRun((r) => r && { ...r, steps: r.steps.map((s, i) => (i === zoneEdit.step ? { ...s, zone } : s)) });
      toast(clear ? `Zone cleared for step ${zoneEdit.step + 1}` : `Work zone saved for step ${zoneEdit.step + 1}`);
      setZoneEdit(null);
    } catch (e) {
      toast(errText(e), true);
    }
  };
  const zoneDown = (e: React.PointerEvent) => {
    if (!zoneEdit) return;
    const p = toImage(e.clientX, e.clientY);
    const box = contentRect(mediaEl());
    if (!p || !box) return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    // grab a point within ~14 px, else add one
    const near = zoneEdit.points.findIndex(([x, y]) => Math.hypot((dispX(x) - dispX(p[0])) * box.width, (y - p[1]) * box.height) < 14);
    if (near >= 0) dragPoint.current = near;
    else {
      dragPoint.current = zoneEdit.points.length;
      setZoneEdit({ ...zoneEdit, points: [...zoneEdit.points, p] });
    }
  };
  const zoneMove = (e: React.PointerEvent) => {
    if (!zoneEdit || dragPoint.current === null) return;
    const p = toImage(e.clientX, e.clientY);
    if (!p) return;
    const i = dragPoint.current;
    setZoneEdit({ ...zoneEdit, points: zoneEdit.points.map((q, k) => (k === i ? p : q)) });
  };

  // instruments: draw, calibrate, then a live reading
  useEffect(() => {
    if (!cameraId) return setInstruments([]);
    listInstruments(cameraId).then(setInstruments).catch(() => setInstruments([]));
  }, [cameraId]);
  const linkedInst = current?.instrument_id || watchInst;
  const inst = instruments.find((i) => i.id === linkedInst) || null;
  useInterval(async () => {
    if (!inst || instBusy.current || document.hidden) return;
    instBusy.current = true;
    try {
      const r = await readInstrument(inst.id);
      setInstRead((cur) => ({ id: inst.id, r, t: Date.now(), confirmed: cur?.id === inst.id && cur.confirmed && cur.r.value === r.value }));
    } catch {
      /* no frame yet, or busy: try again next tick */
    } finally {
      instBusy.current = false;
    }
  }, live && inst && !flow ? 3000 : null);

  const startFlow = () => {
    if (!live || !cameraId) return toast("Start a camera first", true);
    setMenu(false);
    setZoneEdit(null);
    setFlow({ phase: "roi", type: "digital", name: current?.reading?.label || "", unit: current?.reading?.unit || "", format: "", vmin: "", vmax: "" });
  };
  const flowTap = (e: React.PointerEvent) => {
    if (!flow) return;
    const p = toImage(e.clientX, e.clientY);
    if (!p) return;
    if (flow.phase === "roi") {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      setFlowDrag({ x0: p[0], y0: p[1], x1: p[0], y1: p[1] });
    } else if (flow.phase === "center") setFlow({ ...flow, center: p, phase: "min" });
    else if (flow.phase === "min") setFlow({ ...flow, min: p, phase: "max" });
    else if (flow.phase === "max") setFlow({ ...flow, max: p, phase: "values" });
  };
  const flowMove = (e: React.PointerEvent) => {
    if (!flow || flow.phase !== "roi" || !flowDrag) return;
    const p = toImage(e.clientX, e.clientY);
    if (p) setFlowDrag({ ...flowDrag, x1: p[0], y1: p[1] });
  };
  const flowUp = () => {
    if (!flow || flow.phase !== "roi" || !flowDrag) return;
    const roi: [number, number, number, number] = [
      Math.min(flowDrag.x0, flowDrag.x1), Math.min(flowDrag.y0, flowDrag.y1), Math.abs(flowDrag.x1 - flowDrag.x0), Math.abs(flowDrag.y1 - flowDrag.y0),
    ];
    setFlowDrag(null);
    if (roi[2] > 0.02 && roi[3] > 0.02) setFlow({ ...flow, roi, phase: "type" });
  };
  const saveInstrument = async () => {
    if (!flow?.roi || !cameraId) return;
    const name = flow.name.trim() || (flow.type === "analog" ? "Dial" : "Display");
    let body: Omit<Instrument, "id">;
    if (flow.type === "analog") {
      const vmin = Number(flow.vmin);
      const vmax = Number(flow.vmax);
      if (!flow.center || !flow.min || !flow.max || Number.isNaN(vmin) || Number.isNaN(vmax) || flow.vmin === "" || flow.vmax === "") {
        return toast("Enter the values of the lowest and highest marks", true);
      }
      body = { camera_id: cameraId, name, type: "analog", roi: flow.roi, unit: flow.unit.trim(),
               calib: { center: flow.center, min: { point: flow.min, value: vmin }, max: { point: flow.max, value: vmax }, scale: "linear" } };
    } else {
      body = { camera_id: cameraId, name, type: "digital", roi: flow.roi, unit: flow.unit.trim(), format: flow.format.trim() || undefined };
    }
    setFlow({ ...flow, saving: true });
    try {
      const created = await createInstrument(body);
      setInstruments((xs) => [...xs.filter((x) => x.id !== created.id), created]);
      setWatchInst(created.id);
      if (run && run.status === "active") {
        await patchStep(run.procedure.id, run.step, { instrument_id: created.id }).catch(() => {});
        setRun((r) => r && { ...r, steps: r.steps.map((s, i) => (i === r.step ? { ...s, instrument_id: created.id } : s)) });
      }
      toast(`${created.name} added. Irris reads it live.`);
      setFlow(null);
    } catch (e) {
      toast(errText(e), true);
      setFlow({ ...flow, saving: false });
    }
  };
  const confirmReading = () => {
    if (!instRead || instRead.r.value === null) return;
    setValue(String(instRead.r.value));
    if (instRead.r.unit) setUnit(instRead.r.unit);
    setInstRead({ ...instRead, confirmed: true });
    toast(run?.status === "active" ? "Reading confirmed. Tap Pass to record it." : "Reading confirmed.");
  };

  // ---------------------------------------------------------------- hands-free voice
  const onUtterance = useCallback((text: string) => {
    const cmd = parseCommand(text);
    if (cmd) caption("you", text);
    switch (cmd) {
      case "check": return check();
      case "pass": case "fail": case "skip": return mark(cmd);
      case "back": return move("back");
      case "reading": return readNow();
      case "repeat":
        return current
          ? scheduler.say({ text: `Step ${run!.step + 1}. ${current.instruction}${current.check ? `. Correct looks like: ${current.check}` : ""}`, kind: "reply" })
          : verdict ? scheduler.say({ text: verdict.say, kind: "reply" }) : undefined;
      case "stop":
        scheduler.flush();
        return speaker.stop();
      case "record": return toggleRecord("view");
      default: return sendChat(text, true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [check, current, run, verdict, sendChat, caption]);
  const uttRef = useRef(onUtterance);
  uttRef.current = onUtterance;

  const [partial, setPartial] = useState("");
  const [earMode, setEarMode] = useState<"browser" | "server" | "">("");
  const toggleHands = async (auto = false) => {
    if (handsRef.current) {
      handsRef.current.stop();
      handsRef.current = null;
      setHands(false);
      setLevel(0);
      setPartial("");
      setPref("handsFree", false);
      return;
    }
    const h = new HandsFree({
      onUtterance: (t) => uttRef.current(t),
      onLevel: (l, talking) => {
        setLevel(l);
        userTalking.current = talking;
        if (talking) lastHeard.current = performance.now();
      },
      onError: (m) => toast(m, true),
      onPartial: (t) => {
        setPartial(t);
        if (t) lastHeard.current = performance.now();
      },
      onMode: (m) => setEarMode(m),
    });
    try {
      await h.start();
      handsRef.current = h;
      setHands(true);
      speakOn.current = true;
      if (!auto) setPref("handsFree", true);
      scheduler.say({ text: current ? `I'm listening. Step ${run!.step + 1}. ${current.instruction}` : "I'm listening. Ask me anything, or show me what you're working on.", kind: "reply" });
    } catch (e) {
      if (!auto) toast(errText(e), true);
    }
  };

  // ---------------------------------------------------------------- recording
  const overlay = useCallback((): Overlay => {
    const tone = verdict ? (verdict.result === "pass" ? "pass" : (verdict.result as any)) : null;
    return {
      title: run ? `${run.procedure.name}${run.label ? " · " + run.label : ""}` : remote?.name || "Irris",
      mirrored: source === DEVICE && facing === "user",
      step: current ? { k: `Step ${run!.step + 1} of ${run!.steps.length}`, text: current.instruction, spec: current.reading ? `READ ${specText(current.reading)}` : current.check ? `CORRECT: ${current.check}` : "" } : null,
      verdict: verdict && Date.now() - verdict.at < 20000 ? { tone, head: verdict.result === "pass" ? "OK" : verdict.result, text: verdict.say } : null,
      reading: verdict?.reading ? `${num(verdict.reading.value)} ${verdict.reading.unit} · conf ${verdict.reading.confidence.toFixed(2)}` : null,
      banner: banner ? { danger: banner.severity === "danger", text: `${banner.title}${banner.say ? " · " + banner.say : ""}` } : null,
      captions: caps.slice(-3).map((c) => ({ who: c.who, text: c.text })),
    };
  }, [verdict, run, current, remote, source, facing, banner, caps]);
  const overlayRef = useRef(overlay);
  overlayRef.current = overlay;

  const recordStateRef = useRef<{ runId?: string; cameraId?: string }>({});
  const onRecorded = useCallback(async (blob: Blob, mime: string, ms: number) => {
    const url = URL.createObjectURL(blob);
    const ext = mime.includes("mp4") ? "mp4" : "webm";
    const name = `irris-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.${ext}`;
    setClip({ url, mime, name, status: { state: "uploading", text: `${mmss(ms)} clip · uploading…` } });
    try {
      const up = await uploadRecording(blob, { runId: recordStateRef.current.runId, cameraId: recordStateRef.current.cameraId, review: true });
      if (up.job) {
        setClip((c) => c && { ...c, jobId: up.job!.id, status: { state: "reviewing", text: up.run_id ? "Saved to the run. Reviewing…" : "Saved. Reviewing…" } });
      } else {
        setClip((c) => c && { ...c, status: { state: "saved", text: up.run_id ? "Saved to the run as evidence." : "Saved to Data." } });
      }
      if (up.run_id && run?.id === up.run_id) get<Run>(`/runs/${up.run_id}`).then(setRun).catch(() => {});
    } catch (e) {
      setClip((c) => c && { ...c, status: { state: "failed", text: `${errText(e)} It's still here: save it to this device.` } });
    }
  }, [run?.id]);

  useEffect(() => {
    if (!clipJob || !clip || clip.status.state !== "reviewing") return;
    if (clipJob.status === "done") {
      setClip({ ...clip, status: { state: "reviewed", text: clipJob.result.summary || "Reviewed." } });
    } else if (clipJob.status === "failed") {
      setClip({ ...clip, status: { state: "saved", text: `Saved. The review didn't finish: ${clipJob.error}` } });
    } else {
      setClip({ ...clip, status: { state: "reviewing", text: `Reviewing · ${Math.round(clipJob.progress * 100)}%` } });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clipJob?.status, clipJob?.progress]);

  const toggleRecord = async (mode: RecMode = recMode) => {
    const r = recorderRef.current;
    if (r?.recording) {
      const res = await r.stop();
      setRecording(false);
      if (res) onRecorded(res.blob, res.mime, res.ms);
      return;
    }
    if (!live && mode !== "screen") return toast("Start a camera first", true);
    recordStateRef.current = { runId: run?.status === "active" ? run.id : undefined, cameraId: cameraId || undefined };
    // the mic goes in the clip even when hands-free is off
    if (!handsRef.current) {
      try {
        ownMic.current = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } catch {
        ownMic.current = null;             // no mic: the video and Irris's voice only
      }
    }
    const micStream = handsRef.current?.stream || ownMic.current;
    const rec = new LiveRecorder({
      source: () => (source === DEVICE ? videoRef.current : imgRef.current),
      overlay: () => overlayRef.current(),
      box: () => stageRef.current?.getBoundingClientRect() || null,
      cameraStream: () => (source === DEVICE ? streamRef.current : null),
      mic: () => micStream,
    });
    try {
      await rec.start(mode === "camera" && source !== DEVICE ? "view" : mode);
      recorderRef.current = rec;
      setRecording(true);
      setClip(null);
      if (mode === "screen") toast("Recording your screen");
    } catch (e) {
      ownMic.current?.getTracks().forEach((t) => t.stop());
      ownMic.current = null;
      toast(errText(e), true);
    }
  };
  useEffect(() => {
    if (recording) return;
    ownMic.current?.getTracks().forEach((t) => t.stop());
    ownMic.current = null;
  }, [recording]);
  useInterval(() => setRecClock(recorderRef.current?.elapsed || 0), recording ? 500 : null);

  const passed = useMemo(() => (run ? (run.steps || []).filter((s) => stepStatus(run, s) === "complete").length : 0), [run]);
  const deviations = useMemo(() => (run?.log || []).filter((e) => e.result === "fail" || e.result === "danger").length || 0, [run]);
  const vlmReady = vstatus ? vstatus.ready : !!(workspace?.defaults.vlm || workspace?.models.vlm?.base_url);
  const verdictTone = verdict ? (verdict.result === "pass" ? "" : verdict.result) : "";
  const shownCaps = caps.slice(-3);

  return (
    <div className="page full">
      <div className="live">
        <section className="stage" aria-label="Camera" ref={stageRef}>
          <div className="stage-top">
            <select className="select input" style={{ maxWidth: 220, height: 32 }} value={source} onChange={(e) => { setImgOk(false); setSource(e.target.value); }} aria-label="Camera">
              <option value={DEVICE}>This device</option>
              {cameraList.length > 1 && <option value={GRID}>All cameras</option>}
              {cameraList.filter((c) => c.id !== deviceCam).map((c) => (
                <option key={c.id} value={c.id}>{c.name}{c.kind === "phone" ? (c.live ? " · live" : " · offline") : ""}</option>
              ))}
            </select>
            <span className="desktop-only"><VisionBadge status={vstatus} onDark /></span>
            <div className="grow" />
            {run && (
              <span className="mono desktop-only" style={{ fontSize: 11, color: "#A3A3A8" }}>
                {run.procedure.name.toUpperCase()}{run.label ? ` · ${run.label}` : ""} · {duration(run.started, run.ended)}
              </span>
            )}
            {canRecord() && (
              <button className={`btn sm rec-btn${recording ? " on" : ""}`} onClick={() => toggleRecord()} aria-pressed={recording} aria-label={recording ? "Stop recording" : "Record what you see"} title={recording ? "Stop recording" : recMode === "view" ? "Record what you see" : "Record the camera"}>
                {recording ? mmss(recClock) : <><Video /> <span className="desktop-only">Record</span></>}
              </button>
            )}
            <button className={`btn sm voice-btn${hands ? " on" : ""}`} onClick={() => toggleHands()} aria-pressed={hands} aria-label={hands ? "Stop listening" : "Talk hands-free"} title={hands ? "Listening: say check, pass, or ask anything" : "Talk hands-free"}>
              {hands ? (
                <span className="level" aria-hidden="true">{[0.5, 1, 0.7].map((k, i) => <i key={i} style={{ height: `${4 + Math.min(10, level * 14 * k)}px` }} />)}</span>
              ) : <Mic />}
              <span className="desktop-only">{hands ? (earMode === "server" ? "Listening · server" : "Listening") : "Voice"}</span>
            </button>
            <button className="btn sm icon" onClick={() => { const v = !voice; setVoice(v); setPref("speakVerdicts", v); if (!v) speaker.stop(); }} aria-label={voice ? "Mute Irris" : "Let Irris speak"} title={voice ? "Irris speaks verdicts" : "Irris is muted"}>
              {voice ? <Volume2 /> : <VolumeX />}
            </button>
            <button className="btn sm icon" onClick={() => setMenu((m) => !m)} aria-expanded={menu} aria-label="Camera tools"><MoreHorizontal /></button>
          </div>

          {menu && (
            <div className="sheet-menu" role="menu">
              {cameraId && (
                <>
                  <button className="item" role="menuitem" onClick={() => { setMenu(false); snapshot(); }}><Aperture /> Snapshot</button>
                  <button className="item" role="menuitem" onClick={() => { setMenu(false); readNow(); }} disabled={!vlmReady && !vstatus?.perception.ocr}><Gauge /> Read the instrument</button>
                  <button className="item" role="menuitem" onClick={whatHappened} disabled={!vlmReady}><History /> What just happened? <small className="grow" style={{ textAlign: "right" }}>last 90 s</small></button>
                  <button className="item" role="menuitem" onClick={() => { setMenu(false); setDrawing(true); }} disabled={!live}><Crop /> <span>Draw the reading area<small>{run?.status === "active" ? `For step ${run.step + 1}; Irris reads a tight crop` : "For this camera"}</small></span></button>
                  {activeRoi && <button className="item" role="menuitem" onClick={() => { setMenu(false); clearRoi(); }}><X /> Clear the reading area</button>}
                  <button className="item" role="menuitem" onClick={startFlow} disabled={!live}><Gauge /> <span>Add an instrument<small>Calibrate a dial or display; Irris reads it live</small></span></button>
                  <button className="item" role="menuitem" onClick={editZone} disabled={!live || run?.status !== "active"}><Hexagon /> <span>Edit the step's work zone<small>{run?.status === "active" ? `Where step ${run.step + 1} happens; Irris warns before mistakes` : "Start a run first"}</small></span></button>
                  <hr />
                  <div style={{ padding: "6px 10px" }} className="stack sm">
                    <Field label="Goal (for coach mode)">
                      <input className="input" value={goal} onChange={(e) => setGoal(e.target.value)} onBlur={() => goal !== camState?.goal && saveCamState({ goal })} placeholder="Assemble the heater block" />
                    </Field>
                  </div>
                  <label className="item"><Sparkles /> <span>Coach<small>Speaks up for mistakes and safety</small></span>
                    <Toggle on={!!camState?.coach_s} onChange={(v) => saveCamState({ coach_s: v ? 25 : 0, goal })} label="Coach mode" />
                  </label>
                  <label className="item"><ScanLine /> <span>Notice<small>Answers questions written in view</small></span>
                    <Toggle on={!!camState?.notice} onChange={(v) => saveCamState({ notice: v })} label="Notice mode" />
                  </label>
                  <hr />
                </>
              )}
              <label className="item"><ScanEye /> <span>On-device detection<small>{detectError ? detectError : "Objects and hands, live, on this device"}</small></span>
                <Toggle on={detect} onChange={(v) => { setDetect(v); setPref("detect", v); }} label="On-device detection" />
              </label>
              <label className="item"><Sparkles /> <span>Narrate<small>Speaks up when you pick something up or pause</small></span>
                <Toggle on={narrate} onChange={(v) => { setNarrate(v); setPref("narrate", v); }} label="Narrate" />
              </label>
              <label className="item"><Video /> <span>Record what you see<small>{recMode === "view" ? "Camera, steps, verdicts, captions and voices" : "Camera and mic only"}</small></span>
                <Toggle on={recMode === "view"} onChange={(v) => { setRecMode(v ? "view" : "camera"); setPref("recView", v); }} label="Record what you see" />
              </label>
              {canRecordScreen() && (
                <button className="item" role="menuitem" onClick={() => { setMenu(false); toggleRecord("screen"); }} disabled={recording}><MonitorUp /> Record the whole screen</button>
              )}
              {source === DEVICE && streaming && (
                <button className="item" role="menuitem" onClick={() => { const f = facing === "environment" ? "user" : "environment"; setFacing(f); setMenu(false); startDevice(f); }}><CamIcon /> Switch camera</button>
              )}
              <label className="item"><Volume2 /> <span>Speak alerts</span>
                <Toggle on={pref("speakAlerts", true)} onChange={(v) => { setPref("speakAlerts", v); setMenu(false); }} label="Speak alerts" />
              </label>
            </div>
          )}

          <div className="stage-view">
            {source === GRID ? (
              <div className="stage-grid">
                {cameraList.map((c) => (
                  <button key={c.id} onClick={() => { setImgOk(false); setSource(c.id); }} aria-label={`Open ${c.name}`}>
                    {c.kind === "url" || c.live ? <img src={frameUrl(c.id, tick)} alt="" /> : <div className="off">OFFLINE</div>}
                    <span className="cam-label"><span className={`dot ${c.kind === "url" || c.live ? "live" : ""}`} /> {c.name.toUpperCase()}</span>
                  </button>
                ))}
              </div>
            ) : source === DEVICE ? (
              <>
                <video ref={videoRef} playsInline muted autoPlay style={{ display: streaming ? "block" : "none", transform: facing === "user" ? "scaleX(-1)" : undefined }} />
                {!streaming && (
                  <div className="stage-off">
                    <CamIcon />
                    <h2>Use this device's camera</h2>
                    <p>Point it at the work. Irris spots objects and hands live on this device, reads displays, checks each step against your procedure, and talks you through it.</p>
                    {camError && <div className="error-box" style={{ textAlign: "left" }}>{camError}</div>}
                    <div className="row wrap" style={{ justifyContent: "center" }}>
                      <button className="btn primary" onClick={() => startDevice()}><Play /> Start camera</button>
                      <Link className="btn" style={{ color: "var(--paper)", borderColor: "rgba(245,245,243,.2)" }} to="/cameras">Pair a phone instead</Link>
                    </div>
                  </div>
                )}
              </>
            ) : remote ? (
              <>
                <img
                  ref={imgRef}
                  key={remote.id}
                  src={frameUrl(remote.id, tick)}
                  alt={`Live view from ${remote.name}`}
                  onLoad={() => setImgOk(true)}
                  onError={() => setImgOk(false)}
                  style={{ display: imgOk ? "block" : "none" }}
                />
                {!imgOk && (
                  <div className="stage-off">
                    <CamIcon />
                    <h2>{remote.name} isn't sending video</h2>
                    <p>{remote.kind === "phone" ? "Open its pairing link on the phone and keep the page open." : "Irris can't reach this camera's URL right now."}</p>
                    <Link className="btn" style={{ color: "var(--paper)", borderColor: "rgba(245,245,243,.2)" }} to="/cameras">Cameras</Link>
                  </div>
                )}
              </>
            ) : (
              cams.loading && <Loading />
            )}
            {live && source !== GRID && detect && <canvas ref={overlayCanvas} className="perceive-layer" aria-hidden="true" />}
            {live && source !== GRID && (
              <div className={`reticle${checking ? " checking" : ""}${verdict && ["fail", "danger"].includes(verdict.result) ? " fail" : ""}`} aria-hidden="true">
                <i /><i /><i /><i />
              </div>
            )}
          </div>

          {live && source !== GRID && activeRoi && !drawing && (
            <div className="roi" style={roiStyle(activeRoi.box)} aria-hidden="true"><span>READ</span></div>
          )}
          {drawing && (
            <div
              className="roi-layer"
              role="application"
              aria-label="Drag over the instrument Irris should read"
              onPointerDown={(e) => { const p = toFrame(e.clientX, e.clientY); if (p) { (e.target as HTMLElement).setPointerCapture(e.pointerId); setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y }); } }}
              onPointerMove={(e) => { if (!drag) return; const p = toFrame(e.clientX, e.clientY); if (p) setDrag({ ...drag, x1: p.x, y1: p.y }); }}
              onPointerUp={() => {
                if (drag) {
                  const box = { x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1), w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0) };
                  if (box.w > 0.03 && box.h > 0.03) saveRoi(box);
                }
                setDrag(null);
                setDrawing(false);
              }}
              onKeyDown={(e) => e.key === "Escape" && (setDrag(null), setDrawing(false))}
              tabIndex={0}
            >
              <div className="roi-hint">Drag over the gauge or display · Esc to cancel</div>
              {drag && <div className="roi drawing" style={roiStyle({ x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1), w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0) })} />}
            </div>
          )}

          {/* work zones and instruments, drawn in picture coordinates */}
          {live && source !== GRID && !zoneEdit && (() => {
            const box = pictureStyle();
            if (!box) return null;
            const zones = run?.status === "active"
              ? run.steps.map((s, i) => ({ s, i })).filter(({ s, i }) => s.zone?.polygon?.length && s.zone.polygon.length >= 3
                && (!s.zone.camera_id || !cameraId || s.zone.camera_id === cameraId)
                && (i === run.step || watch?.zones[i] === "warn"))
              : [];
            const insts = instruments.filter((x) => x.id === linkedInst || flow);
            if (!zones.length && !insts.length) return null;
            return (
              <div className="pic-layer" style={box} aria-hidden="true">
                <svg viewBox="0 0 1 1" preserveAspectRatio="none" style={{ transform: mirrored ? "scaleX(-1)" : undefined }}>
                  {zones.map(({ s, i }) => (
                    <polygon key={i} className={`zone${watch?.zones[i] === "warn" ? " warn" : ""}${i === run!.step ? "" : " other"}`}
                             points={s.zone!.polygon.map((p) => p.join(",")).join(" ")} vectorEffect="non-scaling-stroke" />
                  ))}
                </svg>
                {zones.map(({ s, i }) => {
                  const xs = s.zone!.polygon.map((p) => dispX(p[0]));
                  const ys = s.zone!.polygon.map((p) => p[1]);
                  return (
                    <span key={`l${i}`} className={`zone-label${watch?.zones[i] === "warn" ? " warn" : ""}`} style={{ left: `${Math.min(...xs) * 100}%`, top: `${Math.min(...ys) * 100}%` }}>
                      STEP {i + 1} ZONE{watch?.zones[i] === "warn" ? " · STOP" : ""}
                    </span>
                  );
                })}
                {insts.map((x) => (
                  <div key={x.id} className="inst-box" style={{ left: `${dispX(mirrored ? x.roi[0] + x.roi[2] : x.roi[0]) * 100}%`, top: `${x.roi[1] * 100}%`, width: `${x.roi[2] * 100}%`, height: `${x.roi[3] * 100}%` }}>
                    <span>{x.name.toUpperCase()} · {x.type === "analog" ? "DIAL" : "DISPLAY"}</span>
                  </div>
                ))}
              </div>
            );
          })()}

          {zoneEdit && (() => {
            const box = pictureStyle();
            return (
              <>
                <div className="pic-layer edit" style={box} role="application" aria-label={`Tap to add the corners of step ${zoneEdit.step + 1}'s work zone; drag a corner to move it`}
                     onPointerDown={zoneDown} onPointerMove={zoneMove} onPointerUp={() => { dragPoint.current = null; }}
                     onKeyDown={(e) => { if (e.key === "Escape") setZoneEdit(null); if (e.key === "Enter") saveZone(); }} tabIndex={0}>
                  <svg viewBox="0 0 1 1" preserveAspectRatio="none" style={{ transform: mirrored ? "scaleX(-1)" : undefined }}>
                    {zoneEdit.points.length >= 2 && (
                      <polygon className="zone editing" points={zoneEdit.points.map((p) => p.join(",")).join(" ")} vectorEffect="non-scaling-stroke" />
                    )}
                  </svg>
                  {zoneEdit.points.map((p, i) => (
                    <span key={i} className="zone-handle" style={{ left: `${dispX(p[0]) * 100}%`, top: `${p[1] * 100}%` }} />
                  ))}
                </div>
                <div className="layer-bar" role="toolbar" aria-label="Work zone">
                  <span className="mono">STEP {zoneEdit.step + 1} ZONE · {zoneEdit.points.length} POINTS</span>
                  <span className="hint">Tap the corners of the work area</span>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn sm icon" aria-label="Undo the last point" onClick={() => setZoneEdit({ ...zoneEdit, points: zoneEdit.points.slice(0, -1) })} disabled={!zoneEdit.points.length}><Undo2 /></button>
                    {!!run?.steps[zoneEdit.step]?.zone && <button className="btn sm" onClick={() => saveZone(true)}>Clear</button>}
                    <button className="btn sm" onClick={() => setZoneEdit(null)}>Cancel</button>
                    <button className="btn sm primary" onClick={() => saveZone()} disabled={zoneEdit.points.length < 3}><Check /> Save zone</button>
                  </div>
                </div>
              </>
            );
          })()}

          {flow && (() => {
            const box = pictureStyle();
            const tapping = flow.phase === "roi" || flow.phase === "center" || flow.phase === "min" || flow.phase === "max";
            const mark = (p?: Pt, k?: string) => p && <span className={`calib-pt ${k}`} style={{ left: `${dispX(p[0]) * 100}%`, top: `${p[1] * 100}%` }}><i>{k?.toUpperCase()}</i></span>;
            const r = flowDrag
              ? [Math.min(flowDrag.x0, flowDrag.x1), Math.min(flowDrag.y0, flowDrag.y1), Math.abs(flowDrag.x1 - flowDrag.x0), Math.abs(flowDrag.y1 - flowDrag.y0)]
              : flow.roi;
            return (
              <>
                <div className={`pic-layer edit${tapping ? "" : " still"}`} style={box} role="application" aria-label={FLOW_HINT[flow.phase] || "Instrument"}
                     onPointerDown={tapping ? flowTap : undefined} onPointerMove={flowMove} onPointerUp={flowUp}
                     onKeyDown={(e) => e.key === "Escape" && (setFlow(null), setFlowDrag(null))} tabIndex={0}>
                  {r && <div className="inst-box drawing" style={{ left: `${dispX(mirrored ? r[0] + r[2] : r[0]) * 100}%`, top: `${r[1] * 100}%`, width: `${r[2] * 100}%`, height: `${r[3] * 100}%` }} />}
                  {flow.type === "analog" && (
                    <>
                      {mark(flow.center, "c")}
                      {mark(flow.min, "min")}
                      {mark(flow.max, "max")}
                      {flow.center && flow.min && (
                        <svg viewBox="0 0 1 1" preserveAspectRatio="none" style={{ transform: mirrored ? "scaleX(-1)" : undefined }}>
                          <line className="calib-line" x1={flow.center[0]} y1={flow.center[1]} x2={flow.min[0]} y2={flow.min[1]} vectorEffect="non-scaling-stroke" />
                          {flow.max && <line className="calib-line" x1={flow.center[0]} y1={flow.center[1]} x2={flow.max[0]} y2={flow.max[1]} vectorEffect="non-scaling-stroke" />}
                        </svg>
                      )}
                    </>
                  )}
                </div>
                {tapping && <div className="roi-hint">{FLOW_HINT[flow.phase]}</div>}
                {(flow.phase === "type" || flow.phase === "values") && (
                  <form className="inst-panel" onSubmit={(e) => {
                    e.preventDefault();
                    if (flow.phase === "type") {
                      if (flow.type === "analog") setFlow({ ...flow, phase: "center" });
                      else saveInstrument();
                    } else saveInstrument();
                  }}>
                    <div className="row between">
                      <span className="eyebrow">{flow.phase === "type" ? "New instrument" : "Scale values"}</span>
                      <button type="button" className="btn sm icon ghost" aria-label="Cancel" onClick={() => { setFlow(null); setFlowDrag(null); }}><X /></button>
                    </div>
                    {flow.phase === "type" && (
                      <>
                        <Field label="Name"><input className="input" value={flow.name} onChange={(e) => setFlow({ ...flow, name: e.target.value })} placeholder="Platen thermometer" autoFocus /></Field>
                        <div className="seg" role="radiogroup" aria-label="Type">
                          {(["digital", "analog"] as const).map((t) => (
                            <button key={t} type="button" role="radio" aria-checked={flow.type === t} className={flow.type === t ? "on" : ""} onClick={() => setFlow({ ...flow, type: t })}>
                              {t === "digital" ? "Digital display" : "Analog dial"}
                            </button>
                          ))}
                        </div>
                        <div className="row" style={{ gap: 8 }}>
                          <Field label="Unit"><input className="input mono" value={flow.unit} onChange={(e) => setFlow({ ...flow, unit: e.target.value })} placeholder="°C" /></Field>
                          {flow.type === "digital" && (
                            <Field label="Format" hint="Optional, e.g. 182.4"><input className="input mono" value={flow.format} onChange={(e) => setFlow({ ...flow, format: e.target.value })} placeholder="^\d{1,3}\.\d$" /></Field>
                          )}
                        </div>
                        <p className="faint" style={{ margin: 0, fontSize: 12 }}>
                          {flow.type === "digital"
                            ? "Irris reads the digits with OCR and only accepts a value when it matches the format and 2 of 3 reads agree."
                            : "Next: tap the dial's centre, its lowest mark and its highest mark. Irris reads the needle by geometry, not guesswork."}
                        </p>
                        <button className="btn primary" disabled={flow.saving}>{flow.saving ? <Spinner /> : flow.type === "analog" ? "Next: calibrate" : <><Check /> Save instrument</>}</button>
                      </>
                    )}
                    {flow.phase === "values" && (
                      <>
                        <div className="row" style={{ gap: 8 }}>
                          <Field label="Lowest mark"><input className="input mono" inputMode="decimal" value={flow.vmin} onChange={(e) => setFlow({ ...flow, vmin: e.target.value })} placeholder="0" autoFocus /></Field>
                          <Field label="Highest mark"><input className="input mono" inputMode="decimal" value={flow.vmax} onChange={(e) => setFlow({ ...flow, vmax: e.target.value })} placeholder="300" /></Field>
                          <Field label="Unit"><input className="input mono" value={flow.unit} onChange={(e) => setFlow({ ...flow, unit: e.target.value })} placeholder="°C" /></Field>
                        </div>
                        <div className="row" style={{ gap: 8 }}>
                          <button type="button" className="btn" onClick={() => setFlow({ ...flow, phase: "center", center: undefined, min: undefined, max: undefined })}>Redo taps</button>
                          <button className="btn primary grow" disabled={flow.saving}>{flow.saving ? <Spinner /> : <><Check /> Save instrument</>}</button>
                        </div>
                      </>
                    )}
                  </form>
                )}
              </>
            );
          })()}

          {live && source !== GRID && (
            <div className="rec">
              <span className="dot live" /> LIVE{sendMode === "ws" ? " · WS" : ""}{auto ? " · AUTO-CHECK" : ""}{camState?.coach_s ? " · COACH" : ""}
            </div>
          )}

          {banner && (
            <div className={`banner ${banner.severity === "danger" ? "danger" : ""}`} role="alert">
              <AlertTriangle />
              <div className="grow">{banner.title}{banner.say && <small>{banner.say}</small>}</div>
              <button className="btn sm icon ghost" style={{ color: "inherit" }} aria-label="Dismiss" onClick={() => setBanner(null)}><X /></button>
            </div>
          )}

          {source !== GRID && (
            <div className="hud">
              {live && (scene || liveRead || looking || partial) && (
                <div className="sense" role="status" aria-live="polite">
                  {looking && <span className="look"><Spinner /> {looking.what.toUpperCase()}… {lookClock} S · USUALLY ~{expected} S {vstatus?.active?.where === "gpu" ? "ON GPU" : "ON CPU"}</span>}
                  {scene && <span className="act"><ScanEye /> {scene.activity.toUpperCase()}{scene.fps ? ` · ${scene.fps} FPS` : ""}</span>}
                  {watch?.lastTouched && (
                    <span className={`held${Object.values(watch.zones).includes("warn") ? " warn" : ""}`}>HELD · {watch.lastTouched.toUpperCase()}</span>
                  )}
                  {liveRead && Date.now() - (liveRead.t || 0) < 6000 && (
                    <span className="read">{liveRead.type === "reading" ? `DISPLAY ${num(liveRead.value || 0)} ${liveRead.unit || ""}` : `TEXT “${(liveRead.text || "").slice(0, 48)}”`} · OCR</span>
                  )}
                  {partial && <span className="heard">“{partial}”</span>}
                </div>
              )}
              {inst && instRead?.id === inst.id && Date.now() - instRead.t < 15000 && (
                <div className={`inst-chip${instRead.r.needs_confirm && !instRead.confirmed ? " confirm" : ""}`} role="status">
                  <span className="v mono">
                    {inst.name.toUpperCase()} · {instRead.r.value === null ? "—" : `${num(instRead.r.value)} ${instRead.r.unit || inst.unit || ""}`.trim()}
                  </span>
                  <span className="m mono">{String(instRead.r.method).toUpperCase()} · CONF {instRead.r.confidence.toFixed(2)}{instRead.confirmed ? " · CONFIRMED" : ""}</span>
                  {instRead.r.reason && <span className="why">{instRead.r.reason}</span>}
                  {instRead.r.needs_confirm && !instRead.confirmed && instRead.r.value !== null && (
                    <button className="btn sm primary" onClick={confirmReading}><Check /> Confirm {num(instRead.r.value)}</button>
                  )}
                </div>
              )}
              {current && (
                <span className="hud-step">
                  STEP {run!.step + 1}/{run!.steps.length} · {checking ? "CHECKING…" : current.reading ? `READ ${specText(current.reading)}` : "WATCHING"}
                </span>
              )}
              {verdict && (
                <div className={`verdict ${verdictTone}`} role="status" key={verdict.at}>
                  <div className="k">
                    <span>{verdict.result === "pass" ? "OK" : verdict.result.toUpperCase()}</span>
                    {verdict.reading && <span>{num(verdict.reading.value)} {verdict.reading.unit} · CONF {verdict.reading.confidence.toFixed(2)}{verdict.reading.verified === true ? " · VERIFIED" : verdict.reading.verified === false ? " · UNVERIFIED" : ""}</span>}
                    {verdict.where && <span style={{ color: "#8A8A8F" }}>{verdict.where.toUpperCase()} · {secs(verdict.ms)}</span>}
                  </div>
                  {verdict.say && <p>{verdict.say}</p>}
                </div>
              )}
              {!!shownCaps.length && (
                <div className="captions" aria-live="polite">
                  {shownCaps.map((c, i) => <div key={c.id} className={`cap ${c.who === "you" ? "you" : ""}${i < shownCaps.length - 1 ? " dim" : ""}`}>{c.text}</div>)}
                </div>
              )}
            </div>
          )}

          {clip && <RecordingPreview url={clip.url} mime={clip.mime} name={clip.name} status={clip.status} onClose={() => { URL.revokeObjectURL(clip.url); setClip(null); }} />}
        </section>

        <aside className="rail">
          <div className="rail-tabs" role="tablist">
            <button role="tab" aria-selected={tab === "steps"} onClick={() => setTab("steps")}>{run ? "Procedure" : "Start"}</button>
            <button role="tab" aria-selected={tab === "ask"} onClick={() => setTab("ask")}>Ask Irris</button>
          </div>

          {tab === "steps" && (
            <>
              <div className="rail-body">
                {runLoading && <Loading />}
                {job && (
                  <div className="panel" style={{ marginBottom: 18 }}>
                    <div className="panel-head">
                      <h2 style={{ fontSize: 14 }}>What just happened</h2>
                      <button className="btn sm icon ghost" aria-label="Close" onClick={() => setReviewJob(null)}><X /></button>
                    </div>
                    <div className="panel-body stack">
                      {job.status === "running" && <div className="row muted" style={{ fontSize: 13 }}><Spinner /> {job.note} · {Math.round(job.progress * 100)}%</div>}
                      {job.status === "failed" && <div className="error-box">{job.error}</div>}
                      {job.status === "done" && (
                        <>
                          <p style={{ margin: 0 }}>{job.result.summary}</p>
                          {!!job.result.timeline?.length && (
                            <ul className="timeline">
                              {job.result.timeline.map((t, i) => <li key={i}><span className="t">{t.t}</span>{t.what}</li>)}
                              {job.result.problems?.map((p, i) => <li key={`p${i}`} className="problem"><span className="t">{p.t}</span>{p.what}</li>)}
                            </ul>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                )}
                {!run && !runLoading && (
                  <div className="stack lg">
                    <div>
                      <h2 style={{ fontSize: 17, fontWeight: 500 }}>Guided run</h2>
                      <p className="muted" style={{ margin: "4px 0 0" }}>Pick a procedure. Irris walks you through it, checks each step through the camera and keeps the evidence.</p>
                    </div>
                    {procs.loading && <Loading />}
                    <ErrorBox error={procs.error} retry={procs.reload} />
                    {procs.data && !procs.data.procedures.length && (
                      <Empty title="No procedures yet" action={<Link className="btn sm" to="/train?tab=procedures">Write a procedure</Link>}>
                        Add your SOPs in Train. Each step says what correct looks like, so Irris can check it.
                      </Empty>
                    )}
                    {!!procs.data?.procedures.length && (
                      <div className="stack">
                        <Field label="Procedure">
                          <select className="select input" value={procId} onChange={(e) => setProcId(e.target.value)}>
                            <option value="">Choose a procedure…</option>
                            {procs.data.procedures.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.steps.length} steps</option>)}
                          </select>
                        </Field>
                        <Field label="Batch or job" hint="Goes on the batch record. Optional.">
                          <input className="input mono" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="B-1042" />
                        </Field>
                        <button className="btn primary lg" disabled={!procId} onClick={start}><Play /> Start run</button>
                        {!vlmReady && (
                          <div className="note">No vision model is set up, so you'll mark steps by hand. Add one in <Link to="/settings?tab=models">Settings → Models</Link> to have Irris check them through the camera.</div>
                        )}
                        {vstatus?.active?.where === "cpu" && (
                          <div className="note">Vision is running on the server's CPU right now, so each check takes 20–40 s. Connect a GPU in <Link to="/settings?tab=models">Settings → Models</Link> for about a second.</div>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {run && (
                  <div className="stack lg">
                    {run.status !== "active" ? (
                      <div className="stack">
                        <div className="eyebrow">{run.status === "done" ? "Run complete" : "Run stopped"}</div>
                        <h2 style={{ fontSize: 18, fontWeight: 500 }}>{run.procedure.name}</h2>
                        <div className="stats" style={{ gridTemplateColumns: "1fr 1fr" }}>
                          <div className="stat"><span className="num">{passed}/{run.steps.length}</span><span className="lbl">Steps passed</span></div>
                          <div className="stat"><span className={`num${deviations ? " red" : ""}`}>{deviations}</span><span className="lbl">Deviations</span></div>
                        </div>
                        <p className="muted" style={{ margin: 0 }}>Irris wrote the batch record: every step, check, reading, photo and time.</p>
                        <div className="row wrap">
                          <Link className="btn primary" to={`/runs/${run.id}`}>View batch record</Link>
                          <button className="btn" onClick={reset}>New run</button>
                        </div>
                      </div>
                    ) : (
                      current && (() => {
                        const pre = prereqsDone(run, run.step);
                        const ev = evidenceText(current);
                        const st = stepStatus(run, current);
                        return (
                          <div className={`cur-card${st === "error" ? " err" : ""}`}>
                            <div className="row between">
                              <span className="eyebrow">Step {run.step + 1} of {run.steps.length}{current.recovery ? " · recovery" : ""}{current.severity === "critical" ? " · critical" : ""}</span>
                              {current.checks > 0 && <ResultTag result={current.result} />}
                            </div>
                            <div className="ins">{current.instruction}</div>
                            {!pre.ok && <div className="waiting mono">WAITING ON STEP {pre.missing.map((i) => i + 1).join(", ")}</div>}
                            {(current.check || current.reading || current.caution || current.expected_objects?.length) && (
                              <dl className="spec">
                                {current.check && <><dt>Correct</dt><dd>{current.check}</dd></>}
                                {current.reading && <><dt>Read</dt><dd className="mono">{current.reading.label ? `${current.reading.label} ` : ""}{specText(current.reading)}</dd></>}
                                {!!current.expected_objects?.length && <><dt>Use</dt><dd className="mono">{current.expected_objects.join(", ")}</dd></>}
                                {current.caution && <><dt>Caution</dt><dd style={{ color: "var(--amber)" }}>{current.caution}</dd></>}
                              </dl>
                            )}
                            {!!current.questions?.length && (
                              <ul className="checklist" aria-label="What Irris checks">
                                {current.questions.map((q, i) => {
                                  const v = votesFor(current, i);
                                  const total = v ? v.yes + v.no + v.unsure : 0;
                                  const mark = !v || !total ? "" : v.yes > v.no && v.yes >= v.unsure ? "yes" : v.no > v.yes && v.no >= v.unsure ? "no" : "unsure";
                                  return (
                                    <li key={i} className={mark}>
                                      <span className="tick" aria-label={mark || "not checked yet"}>{mark === "yes" ? <Check size={11} /> : mark === "no" ? <X size={11} /> : mark === "unsure" ? "?" : ""}</span>
                                      <span className="q">{q.q}{q.must !== false && <em> must</em>}</span>
                                      {!!total && <span className="votes mono" title="yes / no / unsure">{v!.yes}·{v!.no}·{v!.unsure}</span>}
                                    </li>
                                  );
                                })}
                              </ul>
                            )}
                            {ev && <div className="abstain" role="status">{ev}</div>}
                            <div className="row wrap" style={{ gap: 6 }}>
                              <button className="btn sm" onClick={editZone} disabled={!live}><Hexagon /> {current.zone ? "Edit zone" : "Draw work zone"}</button>
                              <button className="btn sm" onClick={startFlow} disabled={!live}><Gauge /> {current.instrument_id ? "New instrument" : "Add instrument"}</button>
                            </div>
                          </div>
                        );
                      })()
                    )}

                    {run.status === "active" && run.recovery && (
                      <div className="recovery-card" role="alert">
                        <div className="row between">
                          <span className="eyebrow red"><LifeBuoy size={12} /> Recovery plan</span>
                          <button className="btn sm icon ghost" aria-label="Dismiss the recovery plan" onClick={() => setRun({ ...run, recovery: null })}><X /></button>
                        </div>
                        <div className="what">{run.recovery.what}</div>
                        {(run.recovery.why || run.recovery.impact?.length) && (
                          <p className="why">
                            <b>Why it matters:</b> {run.recovery.why}
                            {run.recovery.impact?.length ? ` Affects step ${run.recovery.impact.map((i) => i + 1).join(", ")}.` : ""}
                          </p>
                        )}
                        {!!run.recovery.fix?.length && (
                          <ol className="fix">{run.recovery.fix.map((f, i) => <li key={i}>{f}</li>)}</ol>
                        )}
                        <div className="row" style={{ gap: 8 }}>
                          <button className="btn sm primary" onClick={acceptRecovery}><Check /> Accept plan{run.recovery.insert?.length ? ` · adds ${run.recovery.insert.length} step${run.recovery.insert.length > 1 ? "s" : ""}` : ""}</button>
                        </div>
                      </div>
                    )}

                    <ol className="steplist" aria-label="Steps">
                      {run.steps.map((s: GuidedRunStep) => {
                        const st = stepStatus(run, s);
                        const isCur = run.status === "active" && s.index === run.step;
                        const cls = [isCur ? "cur" : "", st === "complete" ? "pass" : st === "error" ? "fail" : st === "skipped" ? "skip" : st === "in_progress" ? "prog" : "", s.recovery ? "recovery" : ""].filter(Boolean).join(" ");
                        const ev = isCur ? "" : st === "error" || st === "in_progress" ? evidenceText(s) : "";
                        return (
                          <li key={s.index} className={cls} data-status={st}>
                            {st === "complete" ? <span className="n"><Check size={12} /></span>
                              : st === "error" ? <span className="n"><X size={12} /></span>
                              : st === "skipped" ? <span className="n">–</span>
                              : st === "in_progress" && typeof s.progress === "number" ? <ProgressRing value={s.progress} label={s.index + 1} />
                              : <span className="n">{s.index + 1}</span>}
                            <div className="grow">
                              <div className="ins">{s.instruction}{s.recovery && <span className="tag amber" style={{ marginLeft: 6 }}>RECOVERY</span>}</div>
                              {(s.say || s.measured || ev) && (
                                <div className="meta">
                                  {s.measured && <span className="mono">{num(s.measured.value)} {s.measured.unit} · </span>}
                                  {s.say}
                                  {ev && <span className="ev"> {ev}</span>}
                                </div>
                              )}
                            </div>
                            {s.snapshot && <a href={fileUrl(s.snapshot)} target="_blank" rel="noreferrer" title="Evidence"><img src={fileUrl(s.snapshot)} alt={`Evidence for step ${s.index + 1}`} style={{ width: 44, height: 33, objectFit: "cover", borderRadius: 4 }} /></a>}
                          </li>
                        );
                      })}
                    </ol>
                    {!!run.recordings?.length && (
                      <div className="stack sm">
                        <div className="eyebrow">Recordings</div>
                        {run.recordings.map((r) => (
                          <video key={r.file_id} controls preload="metadata" src={mediaUrl(r.file_id)} style={{ width: "100%", borderRadius: 6, background: "#000" }} />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {run && run.status === "active" && current && (
                <div className="rail-foot">
                  <div className="row">
                    <button className="btn primary grow" onClick={check} disabled={checking || !live || !vlmReady} title={!vlmReady ? "Set up a vision model in Settings" : !live ? "Start a camera first" : ""}>
                      {checking ? <Spinner /> : <ScanLine />} Check with camera
                    </button>
                    <label className="row nowrap" style={{ gap: 8, fontSize: 13 }}>
                      <Toggle on={auto} onChange={setAuto} label="Auto-check every 8 seconds" /> Auto
                    </label>
                  </div>
                  <div className="row">
                    <input className="input mono" style={{ height: 32 }} inputMode="decimal" placeholder={current.reading ? `Reading ${specText(current.reading)}` : "Reading (optional)"} value={value} onChange={(e) => setValue(e.target.value)} aria-label="Reading" />
                    <input className="input mono" style={{ height: 32, width: 64 }} placeholder="unit" value={unit} onChange={(e) => setUnit(e.target.value)} aria-label="Unit" />
                    {cameraId && <button className="btn sm icon" style={{ height: 32, width: 32 }} onClick={readNow} disabled={checking} aria-label="Read it from the camera" title="Read it from the camera"><Gauge /></button>}
                  </div>
                  <input className="input" style={{ height: 32 }} placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" />
                  <div className="row">
                    <button className="btn sm pass grow" onClick={() => mark("pass")}><Check /> Pass</button>
                    <button className="btn sm fail grow" onClick={() => mark("fail")}><X /> Fail</button>
                    <button className="btn sm icon" onClick={() => mark("skip")} aria-label="Skip step" title="Skip"><SkipForward /></button>
                    <button className="btn sm icon" onClick={() => move("back")} aria-label="Previous step" title="Back" disabled={run.step === 0}><ChevronLeft /></button>
                    <button className="btn sm icon danger" onClick={() => move("stop")} aria-label="Stop run" title="Stop"><CircleStop /></button>
                  </div>
                  {hands && <div className="faint" style={{ fontSize: 12 }}>Say “check”, “pass”, “fail”, “back”, “what's the reading?” or ask anything.</div>}
                </div>
              )}
            </>
          )}

          {tab === "ask" && (
            <>
              <div className="rail-body">
                <div className="mini-chat">
                  {!chat.messages.length && !chat.pending && (
                    <p className="muted" style={{ margin: 0 }}>
                      Ask about what's in front of you: “is this the right torque?”, “what does the manual say about this part?”. Irris sees the camera{run ? " and knows which step you're on" : ""}. Tap the mic to talk.
                    </p>
                  )}
                  {chat.messages.map((m) =>
                    m.role === "user" ? <div key={m.id} className="msg-user">{m.content}</div> : <Answer key={m.id} text={m.content} sources={m.meta.sources} actions={m.meta.actions} />,
                  )}
                  {chat.pending && <Thinking steps={chat.pending.steps} />}
                  <ErrorBox error={chat.error} />
                </div>
              </div>
              <div className="rail-foot">
                <Composer onSend={(t) => sendChat(t)} busy={!!chat.pending} onStop={chat.stop} placeholder="Ask about what the camera sees" />
              </div>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
