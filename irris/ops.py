"""Cross-cutting plumbing: the change feed, the audit log, approvals, and the status contract.

- emit(): every change worth knowing about goes into one ordered feed (`GET /changes?cursor=`), and outgoing
  webhooks subscribe to it.
- audit(): an append-only, hash-chained log of approvals and changes to records of record. Each row stores the hash
  of the row before it, so editing or deleting a row breaks the chain (`verify_audit`). Nothing updates or deletes it.
- Approvals: draft -> review -> approved/rejected -> executed. Nothing is ever approved by a timer.
- status_info(): every long-running object answers "what state is it in, why, what unblocks it, what can I call".
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from irris.db import Database, dumps, loads, new_id, now


def _h(value: Any) -> str:
    if value is None or value == "":
        return ""
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# ================================================================ change feed
def emit(db: Database, workspace_id: str, kind: str, ref: str = "", data: dict | None = None) -> int:
    """Add an entry to the change feed and queue any webhooks subscribed to this event."""
    cid = db.insert_id("INSERT INTO changes (workspace_id, kind, ref, data, created) VALUES (?,?,?,?,?)",
                       [workspace_id, kind, ref, dumps(data or {}), now()])
    try:
        from irris import webhooks
        webhooks.queue(db, workspace_id, kind, {"id": cid, "event": kind, "ref": ref, "data": data or {}, "time": now()})
    except Exception:  # noqa: BLE001 - a webhook problem must never break the change itself
        pass
    return cid


def changes(db: Database, workspace_id: str, cursor: int = 0, limit: int = 100, kinds: list[str] | None = None) -> dict:
    where, params = "workspace_id = ? AND id > ?", [workspace_id, int(cursor or 0)]
    if kinds:
        where += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    limit = max(1, min(int(limit), 500))
    rows = db.execute(f"SELECT * FROM changes WHERE {where} ORDER BY id LIMIT ?", [*params, limit + 1])
    more = len(rows) > limit
    items = [{"id": r["id"], "kind": r["kind"], "ref": r["ref"], "data": loads(r["data"], {}), "time": r["created"]}
             for r in rows[:limit]]
    return {"items": items, "cursor": items[-1]["id"] if items else int(cursor or 0), "more": more}


# ================================================================ audit log (append-only, hash-chained)
def audit(db: Database, workspace_id: str, actor: str, action: str, kind: str, ref: str = "", *,
          before: Any = None, after: Any = None, detail: str = "") -> None:
    prev = db.one("SELECT id, actor, action, kind, ref, before_hash, after_hash, detail, prev_hash, created "
                  "FROM audit_log WHERE workspace_id = ? ORDER BY id DESC LIMIT 1", [workspace_id])
    db.execute("INSERT INTO audit_log (workspace_id, actor, action, kind, ref, before_hash, after_hash, detail, "
               "prev_hash, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
               [workspace_id, actor or "system", action, kind, ref, _h(before), _h(after), detail[:2000],
                _row_hash(prev) if prev else "", now()])


def _row_hash(r: dict) -> str:
    return _h([r["actor"], r["action"], r["kind"], r["ref"], r["before_hash"], r["after_hash"], r["detail"],
               r["prev_hash"], round(float(r["created"]), 6)])


def audit_log(db: Database, workspace_id: str, *, kind: str = "", ref: str = "", limit: int = 200) -> list[dict]:
    where, params = ["workspace_id = ?"], [workspace_id]
    if kind:
        where.append("kind = ?"); params.append(kind)
    if ref:
        where.append("ref = ?"); params.append(ref)
    rows = db.execute(f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", [*params, limit])
    return [{k: r[k] for k in ("id", "actor", "action", "kind", "ref", "before_hash", "after_hash", "detail", "created")}
            for r in rows]


def verify_audit(db: Database, workspace_id: str) -> dict:
    rows = db.execute("SELECT * FROM audit_log WHERE workspace_id = ? ORDER BY id", [workspace_id])
    for prev, cur in zip(rows, rows[1:]):
        if cur["prev_hash"] != _row_hash(prev):
            return {"ok": False, "broken_at": cur["id"], "entries": len(rows)}
    return {"ok": True, "entries": len(rows)}


# ================================================================ approvals
APPROVAL_KINDS = ("batch_signoff", "recovery_plan", "procedure_proposal", "mcp_sync", "webhook", "onboarding")


def approval_api(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "ref": r["ref"], "status": r["status"], "title": r["title"],
            "payload": loads(r["payload"], {}), "requested_by": r["requested_by"], "decided_by": r["decided_by"],
            "meaning": r["meaning"], "comment": r["comment"], "decided_at": r["decided_at"], "created": r["created"]}


def decide(db: Database, workspace_id: str, kind: str, ref: str, *, actor: str, status: str, meaning: str = "",
           comment: str = "", title: str = "", payload: dict | None = None) -> dict:
    """Record a person's decision (approved / rejected / reviewed), audit it, and announce it on the feed."""
    aid = new_id()
    db.execute("INSERT INTO approvals (id, workspace_id, kind, ref, status, title, payload, requested_by, decided_by, "
               "meaning, comment, decided_at, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               [aid, workspace_id, kind, ref, status, title[:200], dumps(payload or {}), actor, actor, meaning,
                comment[:1000], now(), now()])
    audit(db, workspace_id, actor, f"{kind}.{status}", kind, ref, after=payload, detail=meaning or comment)
    emit(db, workspace_id, f"approval.{status}", ref, {"kind": kind, "by": actor, "meaning": meaning})
    return approval_api(db.one("SELECT * FROM approvals WHERE id = ?", [aid]))


def approvals_for(db: Database, workspace_id: str, kind: str, ref: str) -> list[dict]:
    return [approval_api(r) for r in db.execute(
        "SELECT * FROM approvals WHERE workspace_id = ? AND kind = ? AND ref = ? ORDER BY created", [workspace_id, kind, ref])]


def signatures(db: Database, workspace_id: str, run_id: str) -> list[dict]:
    return [{"by": a["decided_by"], "meaning": a["meaning"], "status": a["status"], "comment": a["comment"],
             "time": a["decided_at"]} for a in approvals_for(db, workspace_id, "batch_signoff", run_id)]


# ================================================================ status contract
def info(state: str, message: str, next_action: str = "", allowed: list[str] | None = None) -> dict:
    return {"state": state, "message": message, "next_action": next_action, "allowed_actions": allowed or []}


def run_status(s: dict, sigs: list[dict] | None = None) -> dict:
    sigs = sigs or []
    if s["status"] == "active":
        rec = s.get("recovery") or {}
        if rec.get("status") == "proposed":
            return info("awaiting_recovery", "Something went wrong and a recovery plan is waiting for a decision.",
                        "Read the plan, then accept or dismiss it.", ["recover.accept", "recover.dismiss", "stop"])
        steps = s.get("steps") or []
        cur = steps[s["step"]] if 0 <= s.get("step", 0) < len(steps) else {}
        ev = (cur.get("evidence") or {})
        if ev.get("reasons") and ev.get("score") is not None and ev["score"] < 0.5:
            return info("cant_see", f"Step {s['step'] + 1} can't be judged: {', '.join(ev['reasons'][:2])}.",
                        "Adjust the camera so the work area is clear, or mark the step by hand.",
                        ["check", "pass", "fail", "skip", "back", "stop"])
        if s.get("phase") == "idle":
            return info("idle", f"Waiting on step {s['step'] + 1}: nothing has changed in view for a while.",
                        f"Carry on with step {s['step'] + 1}: {cur.get('instruction', '')[:80]}",
                        ["check", "pass", "fail", "skip", "back", "stop"])
        return info("in_progress", f"On step {s['step'] + 1} of {len(steps)}.",
                    f"Do step {s['step'] + 1}, then check it.", ["check", "pass", "fail", "skip", "back", "stop"])
    approved = [x for x in sigs if x["meaning"] == "approved" and x["status"] == "approved"]
    reviewed = [x for x in sigs if x["meaning"] == "reviewed"]
    if approved:
        return info("released", f"Released: approved by {approved[-1]['by']}.", "", ["read"])
    if s["status"] == "stopped":
        return info("stopped", "Stopped before the end; the batch record lists what wasn't done.",
                    "Review the batch record and sign off, or start a new run.", ["signoff"])
    if reviewed:
        return info("reviewed", f"Reviewed by {reviewed[-1]['by']}; waiting for approval.",
                    "An approver signs off with their password.", ["signoff"])
    return info("awaiting_signoff", "Finished. The batch record isn't released until someone signs it off.",
                "Review the batch record, then sign off as reviewed or approved.", ["signoff"])


def job_status(j: dict) -> dict:
    st = j.get("status")
    if st == "queued":
        return info("queued", "Waiting for a worker" + (" (will retry)" if j.get("attempts") else "") + ".",
                    "Nothing to do; it starts on its own.", ["cancel"])
    if st == "running":
        pct = round(100 * float(j.get("progress") or 0))
        return info("running", (j.get("message") or j.get("note") or "Working") + f" ({pct}%).", "", ["cancel"])
    if st in ("done", "succeeded"):
        return info("done", "Finished.", "", [])
    if st in ("error", "failed"):
        return info("failed", f"It failed: {(j.get('error') or 'unknown error')[:160]}",
                    "Fix the cause, then retry.", ["retry"])
    if st == "cancelled":
        return info("cancelled", "Cancelled.", "Retry it if you still need it.", ["retry"])
    return info(st or "unknown", st or "unknown", "", [])


def camera_status(c: dict) -> dict:
    if c.get("kind") == "url":
        age = c.get("last_frame_age")
        if age is None:
            return info("on_demand", "A network camera: Irris grabs a frame when it needs one.", "", ["frame", "look"])
        return info("ok", f"Last frame {age:.0f} s ago.", "", ["frame", "look"])
    if c.get("live"):
        return info("live", "Streaming now.", "", ["frame", "look", "snapshot"])
    if c.get("last_frame_age") is None:
        return info("never_connected", "This camera has never sent a picture.",
                    "Open its pairing link on the phone (Cameras -> Pair), or start the camera in Live.", ["pair"])
    return info("stale", f"No picture for {c['last_frame_age']:.0f} s: the phone stopped streaming.",
                "Open Irris on the phone again and keep the screen on.", ["pair"])


def source_status(s: dict) -> dict:
    st = s.get("status") or ""
    if st.startswith("error"):
        return info("failing", st, "Check the connection details or the database user's permissions, then sync again.",
                    ["sync", "delete"])
    if s.get("kind") == "webhook":
        return info("waiting" if "waiting" in st else "receiving", st or "Waiting for data.",
                    "Send data to the webhook URL." if "waiting" in st else "", ["delete"])
    return info("connected", st or "Connected.", "", ["sync", "query", "delete"])


def sync_status(sync: dict) -> dict:
    if not sync.get("approved"):
        return info("awaiting_approval", "This sync writes records, so it waits for an admin's approval.",
                    "Approve it to start syncing.", ["approve", "delete"])
    ls = sync.get("last_status") or ""
    if ls.startswith("error"):
        return info("failing", ls, "Check the server, token and arguments; then run it again.", ["run", "delete"])
    if not sync.get("last_run"):
        return info("scheduled", "Approved; it runs within a minute.", "", ["run", "delete"])
    return info("ok", ls, "", ["run", "delete"])


def server_status(srv: dict) -> dict:
    st = srv.get("status") or ""
    if st.startswith("error"):
        return info("failing", st, "Check the URL and token, then refresh.", ["refresh", "delete"])
    return info("connected", st or "Connected.", "", ["call", "refresh", "delete"])


def instrument_status(i: dict) -> dict:
    if i.get("type") == "analog" and not i.get("calib"):
        return info("needs_calibration", "This dial isn't calibrated yet.",
                    "Tap the dial's centre, minimum and maximum in Live.", ["update", "delete"])
    if not i.get("camera_id"):
        return info("no_camera", "No camera is linked to this instrument.", "Link it to a camera.", ["update", "delete"])
    return info("ready", "Ready to read from its camera.", "", ["read", "update", "delete"])


def webhook_status(w: dict) -> dict:
    st = w.get("status")
    if st == "draft":
        return info("awaiting_approval", "Not sending yet: a webhook sends your data outside Irris, so an admin approves it.",
                    "Send a test, then approve it.", ["test", "approve", "delete"])
    if st == "paused":
        return info("paused", "Paused.", "Approve it again to resume.", ["approve", "delete"])
    if w.get("failing"):
        return info("failing", f"Recent deliveries failed: {w['failing']}", "Check the receiving URL is up and returns 2xx.",
                    ["test", "pause", "delete"])
    return info("active", "Sending.", "", ["test", "pause", "delete"])


# ================================================================ feedback
def add_feedback(db: Database, workspace_id: str, actor: str, text: str, context: dict | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("send some text")
    fid = db.insert_id("INSERT INTO feedback (workspace_id, actor, text, context, created) VALUES (?,?,?,?,?)",
                       [workspace_id, actor, text[:4000], dumps(context or {}), now()])
    return {"id": fid, "received": True}


def feedback(db: Database, workspace_id: str, limit: int = 100) -> list[dict]:
    return [{"id": r["id"], "actor": r["actor"], "text": r["text"], "context": loads(r["context"], {}), "created": r["created"]}
            for r in db.execute("SELECT * FROM feedback WHERE workspace_id = ? ORDER BY id DESC LIMIT ?", [workspace_id, limit])]
