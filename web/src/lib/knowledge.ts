// Knowledge: documents of any length, background jobs, study notes, memories, insights.

import { useEffect, useRef, useState } from "react";
import { ApiError, get, getWorkspaceId } from "./api";

export interface Job {
  id: string;
  kind: "read" | "study" | "embed" | "memory" | "insights" | "rows" | string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  title: string;
  progress: number;
  message: string;
  error: string;
  result: Record<string, any>;
  payload: Record<string, any>;
  attempts: number;
  created: number;
  updated: number;
  started: number | null;
  finished: number | null;
}

export interface OutlineItem {
  title: string;
  page: number;
  level: number;
}

export interface DocumentInfo {
  id: string;
  title: string;
  source: string;
  created: number;
  updated: number;
  file_id: string | null;
  mime: string | null;
  size: number | null;
  pages: number;
  read: number;
  unit: "page" | "part";
  status: "queued" | "reading" | "indexed";
  study: string;
  digest_id: string | null;
  empty_pages: number;
  outline_size: number;
}

export interface DocumentDetail extends DocumentInfo {
  outline: OutlineItem[];
  digest?: { text: string; key_points?: string[]; chapters?: { title: string; from: number; to: number }[]; sections?: number; facts?: number };
  jobs: Job[];
  knowledge: number;
}

export interface PageRead {
  id: string;
  title: string;
  from: number;
  to: number;
  pages: number;
  unit: string;
  content: { page: number; text: string }[];
  next: number | null;
}

export interface KnowledgeItem {
  id: string;
  title: string;
  text: string;
  subject: string;
  kind: "notes" | "digest" | "fact" | "limit" | "procedure" | "definition" | "warning" | "spec";
  document_id: string | null;
  page: number | null;
  from: number | null;
  to: number | null;
  key_points?: string[] | null;
  updated: number;
}

export interface Memory {
  id: string;
  text: string;
  kind: "episodic" | "semantic" | "procedural" | "lesson" | "reflection" | "summary" | "goal";
  scope: "team" | "personal";
  subject: string;
  predicate: string;
  importance: number;
  confidence: number;
  source_type: "user_stated" | "doc" | "tool" | "agent_inferred" | "external";
  trust: number;
  source: string;
  pinned: boolean;
  status: "active" | "superseded" | "expired" | "archived";
  superseded_by: string | null;
  evidence_ids: string[];
  uses: number;
  valid_from: number;
  valid_to: number | null;
  created: number;
  updated: number;
  last_used: number | null;
}

export interface UsageToday {
  daily_tokens: number;
  daily_calls: number;
  tokens: number;
  calls: number;
  exhausted: boolean;
  resets_in: number;
}

export const SOURCE_LABEL: Record<string, string> = {
  user_stated: "said by the team", doc: "from a document", tool: "from a tool", agent_inferred: "inferred by Irris",
  external: "external, unverified",
};

export interface Insight {
  id: string;
  title: string;
  text: string;
  subject: string;
  time: number;
  kind?: "out_of_limits" | "anomaly" | "drift";
  severity?: "high" | "medium" | "low";
}

export interface KnowledgeStatus {
  documents: number;
  pages: number;
  passages: number;
  embedded: number;
  semantic: boolean;
  knowledge: number;
  memories: number;
  active_jobs: Job[];
}

export const isActive = (j: Job | null | undefined) => !!j && (j.status === "queued" || j.status === "running");

/** Upload a file with progress. `teach` makes Irris study every page afterwards. */
export function uploadFile(file: File, onProgress: (f: number) => void, opts: { teach?: boolean } = {}): Promise<any> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/ingest/file${opts.teach ? "?teach=true" : ""}`);
    const ws = getWorkspaceId();
    if (ws) xhr.setRequestHeader("X-Workspace", ws);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(xhr.status, (body && typeof body.detail === "string" && body.detail) || `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new ApiError(0, "Upload failed. Check your connection."));
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

/** Follow one job until it finishes. */
export function useJob(id: string | null | undefined, onDone?: (j: Job) => void) {
  const [job, setJob] = useState<Job | null>(null);
  const done = useRef(onDone);
  done.current = onDone;
  useEffect(() => {
    if (!id) return;
    let stop = false;
    let timer = 0;
    const tick = async () => {
      try {
        const j = await get<Job>(`/jobs/${id}`);
        if (stop) return;
        setJob(j);
        if (isActive(j)) timer = window.setTimeout(tick, j.status === "running" ? 1200 : 2500);
        else done.current?.(j);
      } catch {
        if (!stop) timer = window.setTimeout(tick, 5000);
      }
    };
    tick();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, [id]);
  return job;
}

/** The workspace's active jobs, polled while any are running. */
export function useActiveJobs(deps: unknown[] = []) {
  const [jobs, setJobs] = useState<Job[]>([]);
  useEffect(() => {
    let stop = false;
    let timer = 0;
    const tick = async () => {
      try {
        const r = await get<{ jobs: Job[] }>("/jobs?status=active&limit=20");
        if (stop) return;
        const visible = r.jobs.filter((j) => j.kind !== "memory");
        setJobs(visible);
        timer = window.setTimeout(tick, visible.length ? 2000 : 15000);
      } catch {
        if (!stop) timer = window.setTimeout(tick, 15000);
      }
    };
    tick();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return jobs;
}

export function studyLabel(d: Pick<DocumentInfo, "study" | "status">): { text: string; tone: "" | "lime" | "amber" | "red" } {
  if (d.status !== "indexed") return { text: d.status === "reading" ? "Reading" : "Queued", tone: "amber" };
  switch (d.study) {
    case "done": return { text: "Studied", tone: "lime" };
    case "studying": return { text: "Studying", tone: "amber" };
    case "queued": return { text: "Study queued", tone: "amber" };
    case "needs a reasoning model": return { text: "Indexed", tone: "" };
    default: return { text: "Indexed", tone: "" };
  }
}

export const KIND_LABEL: Record<string, string> = {
  notes: "Notes", digest: "Digest", fact: "Fact", limit: "Limit", procedure: "Procedure", definition: "Definition",
  warning: "Warning", spec: "Spec",
};

export function size(bytes: number | null | undefined): string {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}
