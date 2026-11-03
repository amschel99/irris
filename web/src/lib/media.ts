// Vision status, alerts, watch rules, review jobs and recordings: the senses' side of the API.

import { useEffect, useRef, useState } from "react";
import { ApiError, get, getWorkspaceId } from "./api";

export interface VisionLink {
  model: string;
  where: "gpu" | "cpu";
  down: boolean;
  source: string;
}

export interface VisionStatus {
  ready: boolean;
  chain: VisionLink[];
  active: VisionLink | null;
  last: { model: string; where: "gpu" | "cpu"; ms: number; kind: string; at: number; fallback: boolean } | null;
  cpu_busy: boolean;
  queue?: number;
  expected_s?: number;
  perception: { opencv: boolean; ocr: boolean; ocr_error: string };
  gate?: { called: number; skipped: number };
}

export interface Alert {
  id: string;
  time: number;
  kind: "watch" | "coach" | "notice" | "job";
  severity: "info" | "warning" | "danger";
  title: string;
  say: string;
  camera_id?: string;
  camera?: string;
  rule_id?: string;
  speak?: boolean;
  snapshot?: string;
  job_id?: string;
  record_id?: string;
}

export interface WatchRule {
  id: string;
  camera_id: string;
  condition: string;
  interval_s: number;
  severity: "info" | "warning" | "danger";
  speak: boolean;
  enabled: boolean;
  cooldown_s: number;
  last_check: number | null;
  last_match: number | null;
  active: boolean;
  last_say: string;
  checks: number;
  matches: number;
}

export interface CameraState {
  goal: string;
  coach_s: number;
  notice: boolean;
  rules?: WatchRule[];
}

export interface Job {
  id: string;
  kind: string;
  status: "running" | "done" | "failed";
  progress: number;
  note: string;
  params: Record<string, any>;
  result: {
    summary?: string;
    timeline?: { t: string; what: string }[];
    problems?: { t: string; what: string; severity?: string }[];
    readings?: { t: string; value: number; unit: string; what: string }[];
    record_id?: string;
    snapshots?: string[];
  };
  error: string;
  created: number;
}

export interface VoiceSettings {
  stt: { provider: string; base_url: string; model: string; voice: string; has_key: boolean };
  tts: { provider: string; base_url: string; model: string; voice: string; has_key: boolean };
  speak_replies: boolean;
  status: {
    stt: { whisper: boolean; loaded: boolean; error: string };
    tts: { kokoro: boolean; piper: boolean; loaded: string[]; error: string };
  };
  voices: { id: string; label: string; engine: string }[];
}

export function useVisionStatus(every = 15000) {
  const [status, setStatus] = useState<VisionStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => get<VisionStatus>("/vision/status").then((s) => alive && setStatus(s)).catch(() => {});
    load();
    const t = window.setInterval(load, every);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [every]);
  return status;
}

/** Alerts as they happen (server-sent events), with a polling fallback. */
export function subscribeAlerts(onAlert: (a: Alert) => void): () => void {
  const ws = encodeURIComponent(getWorkspaceId());
  let closed = false;
  let es: EventSource | null = null;
  let poll = 0;
  let since = Date.now() / 1000;
  const startPolling = () => {
    if (poll || closed) return;
    poll = window.setInterval(async () => {
      try {
        const r = await get<{ alerts: Alert[]; now: number }>(`/alerts?since=${since}`);
        r.alerts.forEach(onAlert);
        since = r.now;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) window.clearInterval(poll);
      }
    }, 4000);
  };
  if ("EventSource" in window) {
    es = new EventSource(`/api/v1/alerts/stream?ws=${ws}`, { withCredentials: true });
    es.addEventListener("alert", (e) => {
      try {
        const a = JSON.parse((e as MessageEvent).data) as Alert;
        since = Math.max(since, a.time);
        onAlert(a);
      } catch {
        /* malformed */
      }
    });
    es.onerror = () => {
      if (es?.readyState === EventSource.CLOSED) {
        es = null;
        startPolling();
      }
    };
  } else startPolling();
  return () => {
    closed = true;
    es?.close();
    window.clearInterval(poll);
  };
}

/** Follow a review job until it finishes. */
export function useJob(id: string | null) {
  const [job, setJob] = useState<Job | null>(null);
  const timer = useRef(0);
  useEffect(() => {
    setJob(null);
    if (!id) return;
    let alive = true;
    const load = async () => {
      try {
        const j = await get<Job>(`/media/jobs/${id}`);
        if (!alive) return;
        setJob(j);
        if (j.status === "running") timer.current = window.setTimeout(load, 2000);
      } catch {
        if (alive) timer.current = window.setTimeout(load, 4000);
      }
    };
    load();
    return () => {
      alive = false;
      window.clearTimeout(timer.current);
    };
  }, [id]);
  return job;
}

export function whereLabel(w?: "gpu" | "cpu" | null) {
  return w === "cpu" ? "CPU" : "GPU";
}

export function mediaUrl(fileId: string) {
  return `/api/v1/media/files/${fileId}?ws=${encodeURIComponent(getWorkspaceId())}`;
}

export function fileUrl(fileId: string) {
  return `/api/v1/files/${fileId}?ws=${encodeURIComponent(getWorkspaceId())}`;
}

/** Upload a recorded clip; attaches it to the run (if any) and starts a review. */
export async function uploadRecording(blob: Blob, opts: { runId?: string; cameraId?: string; review?: boolean; pairToken?: string }) {
  const fd = new FormData();
  fd.append("file", blob, blob.type.includes("mp4") ? "clip.mp4" : "clip.webm");
  if (opts.runId) fd.append("run_id", opts.runId);
  if (opts.cameraId) fd.append("camera_id", opts.cameraId);
  fd.append("review", opts.review === false ? "false" : "true");
  const url = opts.pairToken ? `/api/v1/cam/recording?t=${encodeURIComponent(opts.pairToken)}` : "/api/v1/recordings";
  const ws = getWorkspaceId();
  const r = await fetch(url, { method: "POST", credentials: "same-origin", headers: !opts.pairToken && ws ? { "X-Workspace": ws } : {}, body: fd });
  const b = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(b.detail || `Upload failed (${r.status})`);
  return b as { file_id: string; record_id: string; run_id: string; url: string; job?: Job; review_error?: string };
}
