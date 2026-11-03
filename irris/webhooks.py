"""Outgoing webhooks: Irris tells the team's other systems when something important happens.

Events: step.failed, deviation.opened, batch.released, run.completed, instrument.out_of_limits, sync.failed (plus
`ping` from the test button). Each delivery is a POST of JSON signed with HMAC-SHA256 over "<timestamp>.<body>"
using the webhook's own secret:

    X-Irris-Event: batch.released
    X-Irris-Delivery: 42
    X-Irris-Signature: t=1793690000,v1=<hex hmac>

Receivers check the signature and that t is recent. Failed deliveries retry with backoff (30 s doubling) for 6 hours.
A new webhook starts as a draft: it sends nothing (except tests) until an admin approves it, because it sends the
team's data outside Irris. Targets on private networks are refused, like connected MCP servers.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from irris import ops
from irris.db import Database, dumps, get_db, loads, new_id, now
from irris.security import decrypt, encrypt, token

EVENTS = ("step.failed", "deviation.opened", "batch.released", "run.completed", "instrument.out_of_limits", "sync.failed")
RETRY_FOR = 6 * 3600
TIMEOUT = 10.0
TRANSPORT: httpx.AsyncBaseTransport | None = None        # tests swap in a fake receiver
log = logging.getLogger("irris.webhooks")
_task: asyncio.Task | None = None


class WebhookError(Exception):
    pass


def check_target(url: str) -> str:
    from irris import mcp_client
    try:
        return mcp_client.check_url(url)
    except mcp_client.MCPError as e:
        raise WebhookError(str(e).replace("MCP URL", "URL").replace("connect a public", "use a public"))


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(t or time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    """For receivers (and tests): check a signature header."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, t), header)


def api(row: dict, *, secret: str | None = None, db: Database | None = None) -> dict:
    failing = ""
    if db is not None:
        bad = db.one("SELECT last_error, last_code FROM webhook_deliveries WHERE webhook_id = ? AND status IN "
                     "('retrying', 'failed') ORDER BY id DESC LIMIT 1", [row["id"]])
        if bad:
            failing = bad["last_error"] or f"HTTP {bad['last_code']}"
    out = {"id": row["id"], "name": row["name"], "url": row["url"], "events": loads(row["events"], []),
           "status": row["status"], "approved_by": row["approved_by"], "created": row["created"], "updated": row["updated"],
           "failing": failing}
    out["status_info"] = ops.webhook_status(out)
    if secret:
        out["secret"] = secret                              # shown once, at creation
    return out


def create(db: Database, workspace_id: str, url: str, events: list[str], name: str = "") -> dict:
    url = check_target(url)
    evs = [e for e in (events or EVENTS) if e in EVENTS] or list(EVENTS)
    secret = token("whsec_", 24)
    wid = new_id()
    db.execute("INSERT INTO webhooks (id, workspace_id, name, url, events, secret, status, created, updated) "
               "VALUES (?,?,?,?,?,?,?,?,?)", [wid, workspace_id, name[:80], url, dumps(evs), encrypt(secret), "draft", now(), now()])
    return api(db.one("SELECT * FROM webhooks WHERE id = ?", [wid]), secret=secret)


def get(db: Database, workspace_id: str, wid: str) -> dict:
    row = db.one("SELECT * FROM webhooks WHERE id = ? AND workspace_id = ?", [wid, workspace_id])
    if not row:
        raise WebhookError("webhook not found")
    return row


def listing(db: Database, workspace_id: str) -> list[dict]:
    return [api(r, db=db) for r in db.execute("SELECT * FROM webhooks WHERE workspace_id = ? ORDER BY created", [workspace_id])]


def set_status(db: Database, workspace_id: str, wid: str, status: str, actor: str) -> dict:
    row = get(db, workspace_id, wid)
    db.execute("UPDATE webhooks SET status = ?, approved_by = ?, updated = ? WHERE id = ?",
               [status, actor if status == "active" else row["approved_by"], now(), wid])
    ops.audit(db, workspace_id, actor, f"webhook.{'approved' if status == 'active' else status}", "webhook", wid,
              before={"status": row["status"]}, after={"status": status, "url": row["url"]}, detail=row["url"])
    return api(db.one("SELECT * FROM webhooks WHERE id = ?", [wid]), db=db)


def delete(db: Database, workspace_id: str, wid: str, actor: str) -> None:
    row = get(db, workspace_id, wid)
    db.execute("DELETE FROM webhook_deliveries WHERE webhook_id = ? AND status IN ('pending', 'retrying')", [wid])
    db.execute("DELETE FROM webhooks WHERE id = ?", [wid])
    ops.audit(db, workspace_id, actor, "webhook.deleted", "webhook", wid, before={"url": row["url"]})


def deliveries(db: Database, workspace_id: str, wid: str, limit: int = 30) -> list[dict]:
    get(db, workspace_id, wid)
    return [{"id": r["id"], "event": r["event"], "status": r["status"], "attempts": r["attempts"], "code": r["last_code"],
             "error": r["last_error"], "created": r["created"], "delivered": r["delivered"]}
            for r in db.execute("SELECT * FROM webhook_deliveries WHERE webhook_id = ? ORDER BY id DESC LIMIT ?", [wid, limit])]


def queue(db: Database, workspace_id: str, event: str, payload: dict) -> int:
    """Called by ops.emit for every change: queue one delivery per active webhook subscribed to the event."""
    if event not in EVENTS:
        return 0
    n = 0
    for w in db.execute("SELECT id, events FROM webhooks WHERE workspace_id = ? AND status = 'active'", [workspace_id]):
        if event in loads(w["events"], []):
            db.execute("INSERT INTO webhook_deliveries (webhook_id, workspace_id, event, payload, status, next_at, created) "
                       "VALUES (?,?,?,?,?,?,?)", [w["id"], workspace_id, event, dumps(payload), "pending", now(), now()])
            n += 1
    return n


async def _post(url: str, secret: str, event: str, did: int, body: bytes) -> tuple[int | None, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "Irris-Webhooks/1.0", "X-Irris-Event": event,
               "X-Irris-Delivery": str(did), "X-Irris-Signature": sign(secret, body)}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=TRANSPORT) as c:
            r = await c.post(url, content=body, headers=headers)
        return r.status_code, "" if 200 <= r.status_code < 300 else f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return None, type(e).__name__


async def deliver(db: Database, d: dict) -> str:
    w = db.one("SELECT * FROM webhooks WHERE id = ?", [d["webhook_id"]])
    if not w:
        db.execute("UPDATE webhook_deliveries SET status = 'failed', last_error = 'webhook deleted' WHERE id = ?", [d["id"]])
        return "failed"
    try:
        check_target(w["url"])
    except WebhookError as e:
        db.execute("UPDATE webhook_deliveries SET status = 'failed', last_error = ? WHERE id = ?", [str(e)[:200], d["id"]])
        return "failed"
    body = d["payload"].encode()
    code, err = await _post(w["url"], decrypt(w["secret"]), d["event"], d["id"], body)
    attempts = d["attempts"] + 1
    if not err:
        db.execute("UPDATE webhook_deliveries SET status = 'delivered', attempts = ?, last_code = ?, last_error = '', "
                   "delivered = ? WHERE id = ?", [attempts, code, now(), d["id"]])
        return "delivered"
    if now() - d["created"] + 30 * 2 ** attempts > RETRY_FOR:
        status, nxt = "failed", now()
    else:
        status, nxt = "retrying", now() + 30 * 2 ** (attempts - 1)
    db.execute("UPDATE webhook_deliveries SET status = ?, attempts = ?, last_code = ?, last_error = ?, next_at = ? "
               "WHERE id = ?", [status, attempts, code, err[:200], nxt, d["id"]])
    return status


async def run_due(db: Database | None = None, limit: int = 50) -> int:
    db = db or get_db()
    due = db.execute("SELECT * FROM webhook_deliveries WHERE status IN ('pending', 'retrying') AND next_at <= ? "
                     "ORDER BY id LIMIT ?", [now(), limit])
    for d in due:
        await deliver(db, d)
    return len(due)


async def test(db: Database, workspace_id: str, wid: str) -> dict:
    """Send a signed `ping` now, even to a draft (so the receiver can be checked before approving)."""
    w = get(db, workspace_id, wid)
    payload = dumps({"event": "ping", "webhook_id": wid, "time": now(), "data": {"message": "Hello from Irris"}})
    did = db.insert_id("INSERT INTO webhook_deliveries (webhook_id, workspace_id, event, payload, status, next_at, created) "
                       "VALUES (?,?,?,?,?,?,?)", [wid, workspace_id, "ping", payload, "pending", now(), now()])
    d = db.one("SELECT * FROM webhook_deliveries WHERE id = ?", [did])
    status = await deliver(db, d)
    d = db.one("SELECT * FROM webhook_deliveries WHERE id = ?", [did])
    if status == "retrying":                                      # a test isn't retried
        db.execute("UPDATE webhook_deliveries SET status = 'failed' WHERE id = ?", [did])
    return {"delivered": status == "delivered", "code": d["last_code"], "error": d["last_error"]}


async def _loop() -> None:
    while True:
        try:
            await run_due()
        except Exception:  # noqa: BLE001
            log.exception("webhook delivery loop")
        await asyncio.sleep(10)


async def start() -> None:
    global _task
    if os.environ.get("IRRIS_WORKER", "1") != "0":
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    if _task:
        _task.cancel()
