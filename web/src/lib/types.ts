export type Role = "owner" | "admin" | "member";

export interface User {
  id: string;
  email: string;
  name: string;
  created: number;
}

export interface WorkspaceRef {
  id: string;
  name: string;
  industry: string;
  role: Role;
}

export interface ModelSetting {
  base_url: string;
  model: string;
  has_key: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  industry: string;
  about: string;
  instructions: string;
  role: Role;
  created: number;
  models: { llm?: ModelSetting; vlm?: ModelSetting };
  defaults: { llm: boolean; vlm: boolean; llm_model: string; vlm_model: string };
}

export interface Me {
  user: User;
  workspaces: WorkspaceRef[];
}

export interface RecordSummary {
  id: string;
  type: string;
  source: string;
  external_id: string | null;
  time: number;
  subject: string;
  title: string;
  tags: string[];
  created: number;
  updated: number;
  preview?: string;
}

export interface RecordFull extends RecordSummary {
  text: string;
  data: Record<string, any>;
  units: Record<string, string>;
  links: { rel: string; to: string }[];
}

export interface Hit {
  record_id: string;
  type: string;
  source: string;
  title: string;
  subject: string;
  page: number | null;
  time: number;
  text: string;
  score: number;
}

export interface ReadingSpec {
  label?: string;
  min?: number;
  max?: number;
  unit?: string;
}

export interface Step {
  instruction: string;
  check?: string;
  caution?: string;
  reading?: ReadingSpec;
}

export interface Procedure {
  id: string;
  name: string;
  description: string;
  subject: string;
  tags: string[];
  steps: Step[];
  updated: number;
  source: string;
}

export interface Measured {
  value: number;
  unit: string;
  confidence: number;
}

export interface RunStep extends Step {
  index: number;
  result: "pass" | "fail" | "unsure" | "danger" | "skip" | null;
  say: string;
  measured: Measured | null;
  checks: number;
  snapshot?: string | null;
}

export interface RunLogEntry {
  step: number;
  result: string;
  say: string;
  reading: Measured | null;
  by: string;
  note: string;
  time: number;
  snapshot?: string | null;
  model?: string;
  where?: "gpu" | "cpu";
  ms?: number;
}

export interface Run {
  id: string;
  status: "active" | "done" | "stopped";
  step: number;
  label: string;
  procedure: { id: string; name: string };
  camera_id: string;
  started: number;
  ended: number | null;
  steps: RunStep[];
  log: RunLogEntry[];
  recordings?: { record_id: string; file_id: string; mime: string; size: number; step: number | null; created: number }[];
}

export interface RunSummary {
  id: string;
  status: string;
  step: number;
  label: string;
  procedure: { id: string; name: string };
  started: number;
  ended: number | null;
  checks: number;
  deviations: number;
}

export interface Camera {
  id: string;
  name: string;
  kind: "phone" | "url";
  created: number;
  live: boolean | null;
  last_frame_age: number | null;
}

export interface Source {
  n: number;
  record_id: string;
  title: string;
  type: string;
  source: string;
  page: number | null;
  excerpt: string;
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  meta: { sources?: Source[]; tools?: string[]; actions?: any[]; seconds?: number };
  created: number;
}

export interface DataSource {
  id: string;
  kind: "postgres" | "webhook";
  name: string;
  status: string;
  last_sync: number | null;
  created: number;
  tables?: string[];
  database?: string;
  schema?: { table: string; rows_estimate: number; columns: number }[];
  type?: string;
  source?: string;
  hint?: string;
  url?: string;
}
