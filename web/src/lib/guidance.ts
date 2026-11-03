// Guidance v2 in the browser (docs/GUIDANCE_SPEC.md): the fields the step tracker adds, calibrated instruments, the
// warning that fires *before* a mistake, and the reminders that come from typical step durations.

import { get, patch, post, put, del } from "./api";
import type { Det, Scene } from "./perceive";
import { asQuestion, type SayItem } from "./scheduler";
import type { Run, RunStep, Step } from "./types";

// ---------------------------------------------------------------- the contract

export type StepStatus = "not_started" | "in_progress" | "complete" | "error" | "skipped";
export type Pt = [number, number];

export interface Zone {
  camera_id?: string;
  polygon: Pt[];
}

export interface Question {
  q: string;
  must?: boolean;
}

export interface Votes {
  yes: number;
  no: number;
  unsure: number;
}

export interface GuidedStep extends Step {
  questions?: Question[];
  typical_errors?: string[];
  kind?: "action" | "measurement" | "timing" | "order";
  severity?: "critical" | "major" | "minor";
  reversible?: boolean;
  prereqs?: number[];
  zone?: Zone | null;
  expected_objects?: string[];
  instrument_id?: string | null;
  median_s?: number | null;
  std_s?: number | null;
  intervention?: "remind_before" | "notify_if_missed" | "none";
  recovery?: boolean;
}

export interface GuidedRunStep extends RunStep, GuidedStep {
  status?: StepStatus;
  progress?: number;
  evidence?: { score: number; reasons: string[] } | null;
  votes?: Votes[] | Record<string, Votes> | null;
  last_check?: { result?: string; say?: string; time?: number } | null;
  started?: number | null;
}

export interface Recovery {
  what: string;
  why?: string;
  impact?: number[];
  fix?: string[];
  insert?: GuidedStep[];
}

export interface GuidedRun extends Run {
  steps: GuidedRunStep[];
  phase?: "working" | "idle";
  plan_version?: number;
  recovery?: Recovery | null;
  say?: SayItem[];
}

export interface Instrument {
  id: string;
  camera_id: string;
  name: string;
  type: "digital" | "analog";
  roi: [number, number, number, number];
  format?: string;
  unit?: string;
  calib?: { center: Pt; min: { point: Pt; value: number }; max: { point: Pt; value: number }; scale?: "linear" | "log" };
}

export interface InstrumentReading {
  value: number | null;
  unit: string;
  method: "ocr" | "needle" | "vlm" | string;
  confidence: number;
  needs_confirm: boolean;
  reason?: string;
}

export const listInstruments = (cameraId?: string) =>
  get<{ instruments: Instrument[] } | Instrument[]>(`/instruments${cameraId ? `?camera_id=${encodeURIComponent(cameraId)}` : ""}`)
    .then((r) => (Array.isArray(r) ? r : r.instruments || []));
export const createInstrument = (body: Omit<Instrument, "id">) => post<Instrument>("/instruments", body);
export const updateInstrument = (id: string, body: Partial<Instrument>) => put<Instrument>(`/instruments/${id}`, body);
export const deleteInstrument = (id: string) => del(`/instruments/${id}`);
export const readInstrument = (id: string) => post<InstrumentReading>(`/instruments/${id}/read`, {});
export const patchStep = (procedureId: string, index: number, body: Partial<GuidedStep>) =>
  patch(`/procedures/${procedureId}/steps/${index}`, body);
export const recover = (runId: string, body: { accept?: boolean; dismiss?: boolean } = {}) =>
  post<GuidedRun | { run: GuidedRun }>(`/runs/${runId}/recover`, body).then((r: any) => (r && r.run ? r.run : r) as GuidedRun);

// ---------------------------------------------------------------- reading the run state (old runs have none of it)

export function stepStatus(run: GuidedRun, s: GuidedRunStep): StepStatus {
  if (s.status) return s.status;
  if (s.result === "pass") return "complete";
  if (s.result === "skip") return "skipped";
  if (s.result === "fail" || s.result === "danger") return "error";
  if (run.status === "active" && s.index === run.step) return "in_progress";
  return "not_started";
}

export function prereqsOf(s: GuidedStep, index: number): number[] {
  return Array.isArray(s.prereqs) ? s.prereqs : index > 0 ? [index - 1] : [];
}

export function prereqsDone(run: GuidedRun, index: number): { ok: boolean; missing: number[] } {
  const s = run.steps[index];
  if (!s) return { ok: true, missing: [] };
  const missing = prereqsOf(s, index).filter((i) => {
    const p = run.steps[i];
    return p && !["complete", "skipped"].includes(stepStatus(run, p));
  });
  return { ok: !missing.length, missing };
}

export function votesFor(s: GuidedRunStep, i: number): Votes | null {
  const v = s.votes;
  if (!v) return null;
  if (Array.isArray(v)) return v[i] || null;
  const q = s.questions?.[i]?.q;
  return (q && v[q]) || v[String(i)] || null;
}

/** "Can't see the valve: tilt the camera" from the evidence the tracker attached. */
export function evidenceText(s: GuidedRunStep): string {
  const ev = s.evidence;
  if (!ev || !ev.reasons?.length) return "";
  const r = ev.reasons.map((x) => x.toLowerCase());
  const tips: string[] = [];
  if (r.some((x) => /blur/.test(x))) tips.push("hold the camera steady");
  if (r.some((x) => /dark|light|glare|bright/.test(x))) tips.push("add some light");
  if (r.some((x) => /not visible|hidden|out of view|occlud|cover/.test(x))) tips.push("tilt the camera so it's in view");
  if (r.some((x) => /small|far/.test(x))) tips.push("move the camera closer");
  const head = ev.reasons.join(" · ");
  return tips.length ? `Can't judge: ${head}. ${tips[0].charAt(0).toUpperCase() + tips[0].slice(1)}.` : `Can't judge: ${head}.`;
}

// ---------------------------------------------------------------- geometry

export function inPolygon(p: { x: number; y: number }, poly: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > p.y !== yj > p.y && p.x < ((xj - xi) * (p.y - yi)) / (yj - yi || 1e-9) + xi) inside = !inside;
  }
  return inside;
}

const TIPS = [4, 8, 12, 16, 20];

function inBox(p: { x: number; y: number }, b: Det["box"], pad = 0.02) {
  return p.x >= b.x - pad && p.x <= b.x + b.w + pad && p.y >= b.y - pad && p.y <= b.y + b.h + pad;
}

export function labelMatches(label: string, expected: string[]): boolean {
  const l = label.toLowerCase().trim();
  return expected.some((e) => {
    const x = e.toLowerCase().trim();
    return !!x && (l === x || l.includes(x) || x.includes(l));
  });
}

// ---------------------------------------------------------------- warn before the mistake (TRAFA)

export interface WatchContext {
  run: GuidedRun | null;
  cameraId: string;
}

export interface WatchState {
  lastTouched: string | null;
  /** step index -> "warn" while a warning about that zone is live */
  zones: Record<number, "warn" | "ok">;
  predicted: { x: number; y: number }[];
}

/**
 * Watches the hands against the run's work zones on every detector frame:
 * - last touched object = an object box with a fingertip inside it for 3+ frames in a row;
 * - each wrist's position 0.5 s and 1 s ahead, from its velocity;
 * - hand about to enter the current step's zone holding something the step doesn't expect -> critical warning;
 * - hand in (or about to enter) the zone of a step whose prereqs aren't done -> major "Step 2 first."
 * The model isn't involved: it's the detector plus geometry, so it fires before the mistake, not after.
 */
export class Watcher {
  lastTouched: string | null = null;
  lastTouchedAt = 0;
  onSay: ((item: SayItem) => void) | null = null;
  /** Below this, a warning about the same zone isn't repeated (the scheduler adds its own 25 s per topic). */
  debounceMs = 4000;

  private streak = new Map<string, number>();
  private trail: { t: number; x: number; y: number }[][] = [[], []];
  private fired = new Map<string, number>();
  private wasInZone = new Map<number, boolean>();
  state: WatchState = { lastTouched: null, zones: {}, predicted: [] };

  reset() {
    this.lastTouched = null;
    this.lastTouchedAt = 0;
    this.streak.clear();
    this.trail = [[], []];
    this.fired.clear();
    this.wasInZone.clear();
    this.state = { lastTouched: null, zones: {}, predicted: [] };
  }

  update(scene: Scene, ctx: WatchContext, now = performance.now()): WatchState {
    // 1. what the fingertips are on
    const touching = new Set<string>();
    for (const hand of scene.hands) {
      const tips = TIPS.map((i) => hand.points[i]).filter(Boolean);
      for (const o of scene.objects) {
        if (o.label === "person") continue;
        if (tips.some((p) => inBox(p, o.box))) touching.add(o.label);
      }
    }
    for (const l of [...this.streak.keys()]) if (!touching.has(l)) this.streak.delete(l);
    for (const l of touching) {
      const n = (this.streak.get(l) || 0) + 1;
      this.streak.set(l, n);
      if (n >= 3) {
        this.lastTouched = l;
        this.lastTouchedAt = now;
      }
    }
    if (this.lastTouched && now - this.lastTouchedAt > 8000 && !touching.size) this.lastTouched = null;

    // 2. where each wrist is going
    const predicted: { x: number; y: number }[] = [];
    const wrists: { x: number; y: number }[] = [];
    scene.hands.slice(0, 2).forEach((hand, i) => {
      const w = hand.points[0] || hand.center;
      wrists.push(w);
      const tr = (this.trail[i] = [...(this.trail[i] || []), { t: now, x: w.x, y: w.y }].filter((p) => now - p.t <= 400));
      const first = tr[0];
      const dt = (now - first.t) / 1000;
      if (dt < 0.08) return;
      const vx = (w.x - first.x) / dt;
      const vy = (w.y - first.y) / dt;
      for (const h of [0.5, 1]) predicted.push({ x: w.x + vx * h, y: w.y + vy * h });
    });
    for (let i = scene.hands.length; i < 2; i++) this.trail[i] = [];

    const zones: WatchState["zones"] = {};
    const run = ctx.run;
    if (run && run.status === "active") {
      run.steps.forEach((s, i) => {
        const z = s.zone;
        if (!z || !z.polygon || z.polygon.length < 3) return;
        if (z.camera_id && ctx.cameraId && z.camera_id !== ctx.cameraId) return;
        const status = stepStatus(run, s);
        if (status === "complete" || status === "skipped") return;
        const inNow = wrists.some((w) => inPolygon(w, z.polygon));
        const entering = !inNow && predicted.some((p) => inPolygon(p, z.polygon));
        const was = this.wasInZone.get(i) || false;
        this.wasInZone.set(i, inNow);
        zones[i] = "ok";
        if (!inNow && !entering) return;

        // wrong part, about to go in
        if (i === run.step && s.expected_objects?.length && this.lastTouched && !labelMatches(this.lastTouched, s.expected_objects)) {
          if (entering || (inNow && !was)) {
            zones[i] = "warn";
            this.fire(`step${i}:wrong-object`, now, {
              text: `Wrong part. Use the ${s.expected_objects[0]}.`,
              severity: "critical",
              kind: "warning",
              topic: `step${i}:wrong-object`,
              step: i,
            });
          } else {
            const at = this.fired.get(`step${i}:wrong-object`);
            if (at !== undefined && now - at < 6000) zones[i] = "warn";
          }
        }

        // a step whose prereqs aren't done yet
        const pre = prereqsDone(run, i);
        if (!pre.ok) {
          zones[i] = "warn";
          const first = Math.min(...pre.missing);
          this.fire(`step${i}:prereq`, now, {
            text: `Step ${first + 1} first.`,
            severity: "major",
            kind: "warning",
            topic: `step${i}:prereq`,
            step: i,
          });
        }
      });
    }
    this.state = { lastTouched: this.lastTouched, zones, predicted };
    return this.state;
  }

  private fire(key: string, now: number, item: SayItem) {
    const last = this.fired.get(key);
    if (last !== undefined && now - last < this.debounceMs) return;
    this.fired.set(key, now);
    this.onSay?.(item);
  }
}

// ---------------------------------------------------------------- reminders from typical durations (PrISM)

/**
 * From when the current step began and its typical duration:
 * - remind_before (irreversible steps): a heads-up at median - 15 s, before the point of no return;
 * - notify_if_missed: "Have you done X?" at median + 15 s if the step still isn't complete.
 * Returns the reminders that are due now; each fires once per step start.
 */
export class Reminders {
  private fired = new Set<string>();
  private startedAt = new Map<string, number>();

  due(run: GuidedRun | null, now = Date.now()): SayItem[] {
    if (!run || run.status !== "active") return [];
    const s = run.steps[run.step];
    if (!s) return [];
    const key = `${run.id}:${run.step}`;
    if (!this.startedAt.has(key)) this.startedAt.set(key, s.started ? s.started * 1000 : now);
    const t0 = this.startedAt.get(key)!;
    const median = typeof s.median_s === "number" && s.median_s > 0 ? s.median_s : null;
    if (!median) return [];
    const intervention = s.intervention || (s.reversible === false ? "remind_before" : "notify_if_missed");
    const elapsed = (now - t0) / 1000;
    const out: SayItem[] = [];
    const done = ["complete", "skipped"].includes(stepStatus(run, s));
    if (intervention === "remind_before" && elapsed >= Math.max(2, median - 15) && !this.fired.has(key + ":before") && !done) {
      this.fired.add(key + ":before");
      out.push({
        text: `Before you go on: ${s.caution || s.check || s.instruction}. Ready?`,
        severity: "major",
        kind: "reminder",
        topic: `step${run.step}:remind`,
        step: run.step,
      });
    }
    if (intervention === "notify_if_missed" && elapsed >= median + 15 && !this.fired.has(key + ":missed") && !done) {
      this.fired.add(key + ":missed");
      out.push({ text: asQuestion(s.instruction), severity: "major", kind: "question", topic: `step${run.step}:missed`, step: run.step });
    }
    return out;
  }
}
