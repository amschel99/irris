// Guidance v2 shapes (docs/GUIDANCE_SPEC.md). Kept apart from types.ts so the Live screen can import them too.
import type { Run, RunStep, Step } from "./types";

export type StepKind = "action" | "measurement" | "timing" | "order";
export type Severity = "critical" | "major" | "minor";
export type Intervention = "remind_before" | "notify_if_missed" | "none";
export type StepStatus = "not_started" | "in_progress" | "complete" | "error" | "skipped";

export interface Question { q: string; must: boolean }

export interface GuidedStep extends Step {
  questions?: Question[];
  typical_errors?: string[];
  kind?: StepKind;
  severity?: Severity;
  reversible?: boolean;
  prereqs?: number[];
  zone?: { camera_id: string; polygon: [number, number][] } | null;
  expected_objects?: string[];
  instrument_id?: string | null;
  median_s?: number | null;
  std_s?: number | null;
  intervention?: Intervention;
  prototype?: { vector: number[]; p85: number; n: number } | null;
  recovery?: boolean;
}

export interface Vote { yes: number; no: number; unsure: number; answer: "yes" | "no" | "unsure"; must: boolean }

export interface GuidedRunStep extends RunStep, GuidedStep {
  uid: string;
  status: StepStatus;
  progress: number;
  evidence: { score: number | null; reasons: string[] } | null;
  votes: Record<string, Vote>;
  last_check: { result: string; t: number; confidence?: number | null; samples?: number | null; method?: string } | null;
  settled: boolean;
  started_at: number | null;
  completed_at: number | null;
}

export interface SayItem {
  text: string;
  severity: Severity;
  topic: string;
  kind: "warning" | "reminder" | "question" | "confirm";
  step: number | null;
  t: number;
}

export interface RecoveryPlan {
  what: string;
  impact: number[];
  why: string;
  fix: string[];
  insert: { instruction: string; check?: string }[];
  step: number;
  status: "proposed" | "accepted" | "dismissed";
  source: "model" | "rules";
  inserted?: number[];
  t: number;
}

export interface RunMetrics {
  vision_checks: number;
  abstained: number;
  abstention_rate: number;
  false_alarms: number;
  step_seconds: number[];
  median_step_s: number | null;
  readings: { step: number; auto: number; confirmed: number; method?: string }[];
  reading_error_mean: number | null;
}

export interface GuidedRun extends Omit<Run, "steps"> {
  steps: GuidedRunStep[];
  phase: "working" | "idle";
  plan_version: number;
  recovery: RecoveryPlan | null;
  say: SayItem[];
  metrics: RunMetrics;
}

export interface StepChange { index: number; instruction: string; [field: string]: unknown }

export interface Proposal {
  procedure_id: string;
  steps: GuidedStep[];
  changes: StepChange[];
  notes: string[];
  runs?: number;
}

export interface Instrument {
  id: string;
  name: string;
  type: "digital" | "analog";
  camera_id: string;
  unit: string;
  roi?: [number, number, number, number];
  format?: string;
  calib?: { center: [number, number]; min: { point: [number, number]; value: number }; max: { point: [number, number]; value: number }; direction: "cw" | "ccw"; needle: "dark" | "light" };
  limits?: { min?: number; max?: number };
}

export const STATUS_LABEL: Record<StepStatus, string> = {
  not_started: "to do", in_progress: "in progress", complete: "done", error: "problem", skipped: "skipped",
};

export const STATUS_TONE: Record<StepStatus, string> = {
  not_started: "", in_progress: "amber", complete: "lime", error: "red", skipped: "",
};
