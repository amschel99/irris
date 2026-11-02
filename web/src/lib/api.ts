// The Irris REST API (/api/v1). Every call names the current workspace in X-Workspace.

const WS_KEY = "irris.workspace";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getWorkspaceId(): string {
  try {
    return localStorage.getItem(WS_KEY) || "";
  } catch {
    return "";
  }
}

export function setWorkspaceId(id: string) {
  try {
    localStorage.setItem(WS_KEY, id);
  } catch {
    /* private mode: the session still works, it just won't remember the choice */
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

function headers(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  const ws = getWorkspaceId();
  if (ws) h.set("X-Workspace", ws);
  return h;
}

function detail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d[0]?.msg) return String(d[0].msg);
  }
  return fallback;
}

export async function api<T = any>(path: string, opts: RequestInit & { json?: unknown; quiet401?: boolean } = {}): Promise<T> {
  const { json, quiet401, ...init } = opts;
  const h = headers(init.headers);
  if (json !== undefined) {
    h.set("Content-Type", "application/json");
    init.body = JSON.stringify(json);
  }
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { credentials: "same-origin", ...init, headers: h });
  } catch {
    throw new ApiError(0, "Can't reach Irris. Check your connection.");
  }
  if (res.status === 401 && !quiet401) onUnauthorized();
  const ctype = res.headers.get("content-type") || "";
  const body = ctype.includes("json") ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) throw new ApiError(res.status, detail(body, `Request failed (${res.status})`));
  return body as T;
}

export const get = <T = any>(p: string) => api<T>(p);
export const post = <T = any>(p: string, json?: unknown) => api<T>(p, { method: "POST", json: json ?? {} });
export const put = <T = any>(p: string, json?: unknown) => api<T>(p, { method: "PUT", json: json ?? {} });
export const patch = <T = any>(p: string, json?: unknown) => api<T>(p, { method: "PATCH", json: json ?? {} });
export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });

/** Upload one file with progress (fetch has no upload progress). */
export function upload(file: File, onProgress: (f: number) => void): Promise<any> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/ingest/file");
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
      if (xhr.status === 401) onUnauthorized();
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(xhr.status, detail(body, `Upload failed (${xhr.status})`)));
    };
    xhr.onerror = () => reject(new ApiError(0, "Upload failed. Check your connection."));
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

/** POST /ask with stream: true. Calls onEvent for each server-sent event. */
export async function askStream(
  body: Record<string, unknown>,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const h = headers({ "Content-Type": "application/json", Accept: "text/event-stream" });
  const res = await fetch("/api/v1/ask", {
    method: "POST",
    credentials: "same-origin",
    headers: h,
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  });
  if (res.status === 401) onUnauthorized();
  if (!res.ok || !res.body) {
    const b = await res.json().catch(() => null);
    throw new ApiError(res.status, detail(b, `Request failed (${res.status})`));
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let ev = "message";
      let data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      try {
        onEvent(ev, data ? JSON.parse(data) : {});
      } catch {
        onEvent(ev, {});
      }
    }
  }
}

/** URL of a camera's latest frame, for <img>. Images can't send headers, so the workspace goes in ?ws=. */
export function frameUrl(cameraId: string, bust: number): string {
  return `/api/v1/cameras/${cameraId}/frame.jpg?ws=${encodeURIComponent(getWorkspaceId())}&t=${bust}`;
}
