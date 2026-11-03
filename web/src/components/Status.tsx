// The status contract every long-running object carries: what state it's in, why, what unblocks it.
export type StatusInfo = { state: string; message: string; next_action: string; allowed_actions: string[] };

const GOOD = new Set(["live", "ok", "connected", "ready", "done", "released", "active", "in_progress", "running", "receiving", "scheduled", "on_demand"]);
const BAD = new Set(["failed", "failing", "stale", "never_connected", "cant_see", "no_camera"]);

export function tone(state: string): "good" | "bad" | "wait" {
  if (GOOD.has(state)) return "good";
  if (BAD.has(state)) return "bad";
  return "wait";
}

/** One line: a dot for the state, the reason, and (muted) what to do next. */
export function StatusLine({ info, compact }: { info?: StatusInfo | null; compact?: boolean }) {
  if (!info) return null;
  const t = tone(info.state);
  return (
    <div className={`status-line ${t}${compact ? " compact" : ""}`} title={compact ? [info.message, info.next_action].filter(Boolean).join(" ") : undefined}>
      <span className="status-dot" aria-hidden />
      <span className="status-msg">{info.message}</span>
      {!compact && info.next_action && <span className="status-next">{info.next_action}</span>}
    </div>
  );
}
