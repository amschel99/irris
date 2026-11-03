import {
  AlertTriangle, Aperture, Camera as CamIcon, Check, ChevronLeft, CircleStop, Crop, Gauge, History, Mic, MonitorUp,
  MoreHorizontal, Play, ScanLine, ScanEye, SkipForward, Sparkles, Video, Volume2, VolumeX, X,
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
import { Composer, Thinking, useAsk } from "./Ask";

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

  const [run, setRun] = useState<Run | null>(null);
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

  const [recMode, setRecMode] = useState<"view" | "camera">(() => (pref("recView", true) ? "view" : "camera"));
  const [recording, setRecording] = useState(false);
  const [recClock, setRecClock] = useState(0);
  const recorderRef = useRef<LiveRecorder | null>(null);
  const [clip, setClip] = useState<ClipState | null>(null);
  const clipJob = useJob(clip?.status.state === "reviewing" ? clip.jobId || null : null);
  const ownMic = useRef<MediaStream | null>(null);

  const cameraId = source === DEVICE ? deviceCam : source === GRID ? "" : source;
  const cameraList = cams.data?.cameras || [];
  const remote = cameraList.find((c) => c.id === source);
  const live = source === DEVICE ? streaming : !!remote && (remote.kind === "url" || imgOk || !!remote.live);
  const onLive = useCallback((m: LiveMsg) => {
    if (m.type === "reading" || m.type === "text") setLiveRead({ ...m, t: Date.now() });
  }, []);
  const sendMode = useCameraFeed(cameraId, live && source !== GRID, videoRef, source === DEVICE && streaming, onLive);
  const expected = vstatus?.expected_s || (vstatus?.active?.where === "gpu" ? 3 : 30);

  const caption = useCallback((who: Cap["who"], text: string) => {
    if (!text.trim()) return;
    setCaps((c) => [...c.slice(-4), { id: Date.now() + Math.random(), who, text: text.trim() }]);
  }, []);

  useEffect(() => {
    speaker.onCaption = (who, text) => caption(who, text);
    return () => {
      speaker.onCaption = null;
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

  const say = useCallback((text: string) => {
    if (voice || hands) speaker.say(text);
    else caption("irris", text);
  }, [voice, hands, caption]);

  const announce = useCallback((v: Verdict) => {
    setVerdict(v);
    if (v.say) say(v.say);
  }, [say]);

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
      const out = await post<{ check: Verdict; run: Run }>(`/runs/${run.id}/check`, { camera_id: cameraId || undefined, scene: sceneRef.current?.summary });
      announce({ ...out.check, at: Date.now() });
      const advanced = out.run.step !== run.step || out.run.status !== run.status;
      setRun(out.run);
      if (advanced && out.run.status === "active") window.setTimeout(() => say(`Next. ${out.run.steps[out.run.step].instruction}`), 1600);
      if (out.run.status === "done") window.setTimeout(() => say("That's the last step. The batch record is written."), 1600);
    } catch (e) {
      setAuto(false);
      announce({ result: "unsure", say: lookError(e), reading: null, at: Date.now() });
    } finally {
      setChecking(false);
      setLooking(null);
    }
  }, [run, checking, cameraId, announce, say]);

  const autoCheck = useCallback(async () => {
    if (!run || run.status !== "active" || checking) return;
    setChecking(true);
    try {
      const out = await post<{ check: Verdict & { skipped?: boolean }; run: Run }>(`/runs/${run.id}/check/auto`, { camera_id: cameraId || undefined, scene: sceneRef.current?.summary });
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
  }, [run, checking, cameraId, announce, say]);

  useInterval(autoCheck, auto && live && run?.status === "active" ? AUTO_MS : null);

  const mark = async (result: "pass" | "fail" | "skip") => {
    if (!run) return;
    const reading = value.trim() ? { value: Number(value), unit } : undefined;
    if (reading && Number.isNaN(reading.value)) return toast("The reading must be a number", true);
    try {
      const out = await post<{ check: Verdict; run: Run }>(`/runs/${run.id}/check`, { result, reading, note: note || undefined });
      setRun(out.run);
      const said = out.check.result === "fail" && result === "pass" ? `Out of spec: ${out.check.say}` : out.check.say;
      announce({ ...out.check, say: said || (result === "pass" ? "Step passed." : result === "skip" ? "Step skipped." : "Marked as failed."), at: Date.now() });
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
      speaker.say(quick ? `${quick}. Let me take a closer look.` : "Let me take a look.");
    }
    if (visual) setLooking({ since: Date.now(), what: "Looking" });
    try {
      const res = await chat.send(text, { camera_id: cameraId || undefined, run_id: run?.id, scene: sceneRef.current?.summary }, conv);
      if (res?.conversation_id) setConv(res.conversation_id);
      if (res?.reply) {
        if (spoken || voice || hands) speaker.say(res.reply);
        else caption("irris", res.reply);
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
      announce({ result: out.status === "ok" ? "pass" : out.status === "danger" ? "danger" : "unsure", say: out.say, reading: out.reading, at: Date.now(), where: out.where, ms: out.ms });
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
      const now = performance.now();
      if (now - lastUi > 250) {
        lastUi = now;
        setScene(sc);
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
      speaker.say(line, { interrupt: false });
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
          if (voice || hands) speaker.say(out.say, { interrupt: false });
          else caption("irris", out.say);
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
        return current ? speaker.say(`Step ${run!.step + 1}. ${current.instruction}${current.check ? `. Correct looks like: ${current.check}` : ""}`) : verdict ? speaker.say(verdict.say) : undefined;
      case "stop": return speaker.stop();
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
      onLevel: (l) => setLevel(l),
      onError: (m) => toast(m, true),
      onPartial: (t) => setPartial(t),
      onMode: (m) => setEarMode(m),
    });
    try {
      await h.start();
      handsRef.current = h;
      setHands(true);
      if (!auto) setPref("handsFree", true);
      speaker.say(current ? `I'm listening. Step ${run!.step + 1}. ${current.instruction}` : "I'm listening. Ask me anything, or show me what you're working on.");
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

  const passed = useMemo(() => run?.steps.filter((s) => s.result === "pass").length || 0, [run]);
  const deviations = useMemo(() => run?.log.filter((e) => e.result === "fail" || e.result === "danger").length || 0, [run]);
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
                  {liveRead && Date.now() - (liveRead.t || 0) < 6000 && (
                    <span className="read">{liveRead.type === "reading" ? `DISPLAY ${num(liveRead.value || 0)} ${liveRead.unit || ""}` : `TEXT “${(liveRead.text || "").slice(0, 48)}”`} · OCR</span>
                  )}
                  {partial && <span className="heard">“{partial}”</span>}
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
                      current && (
                        <div className="cur-card">
                          <div className="row between">
                            <span className="eyebrow">Step {run.step + 1} of {run.steps.length}</span>
                            {current.checks > 0 && <ResultTag result={current.result} />}
                          </div>
                          <div className="ins">{current.instruction}</div>
                          {(current.check || current.reading || current.caution) && (
                            <dl className="spec">
                              {current.check && <><dt>Correct</dt><dd>{current.check}</dd></>}
                              {current.reading && <><dt>Read</dt><dd className="mono">{current.reading.label ? `${current.reading.label} ` : ""}{specText(current.reading)}</dd></>}
                              {current.caution && <><dt>Caution</dt><dd style={{ color: "var(--amber)" }}>{current.caution}</dd></>}
                            </dl>
                          )}
                        </div>
                      )
                    )}

                    <ol className="steplist" aria-label="Steps">
                      {run.steps.map((s) => {
                        const cls = run.status === "active" && s.index === run.step ? "cur" : s.result || "";
                        return (
                          <li key={s.index} className={cls}>
                            <span className="n">{s.result === "pass" ? <Check size={12} /> : s.result === "fail" || s.result === "danger" ? <X size={12} /> : s.index + 1}</span>
                            <div className="grow">
                              <div className="ins">{s.instruction}</div>
                              {(s.say || s.measured) && (
                                <div className="meta">
                                  {s.measured && <span className="mono">{num(s.measured.value)} {s.measured.unit} · </span>}
                                  {s.say}
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
