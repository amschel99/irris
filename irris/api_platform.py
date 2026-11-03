"""Routes for the platform features: status, the change feed, feedback, the audit log, batch sign-off, webhooks,
the draft twin (onboarding) and plain-words queries."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from irris import auth, jobs, nlquery, onboarding, ops, runs, store, webhooks
from irris.api import actor_of, body_of, camera_api, source_api
from irris.auth import Ctx, ctx
from irris.db import loads

router = APIRouter(prefix="/api/v1")


def _need_admin(c: Ctx) -> None:
    c.require_admin()


# ================================================================ status contract
@router.get("/status/{kind}/{obj_id}")
def status(kind: str, obj_id: str, c: Ctx = Depends(ctx)):
    """Why any long-running thing is in the state it's in, what unblocks it, and what can be called now.
    kind: run, job, media_job, camera, source, mcp_server, mcp_sync (id = server_id:sync_id), instrument, webhook,
    onboarding (id = anything)."""
    return {"kind": kind, "id": obj_id, **status_of(c, kind, obj_id)}


def status_of(c: Ctx, kind: str, obj_id: str) -> dict:
    db, ws = c.db, c.ws_id
    try:
        if kind == "run":
            return runs.state(db, ws, obj_id)["status_info"]
        if kind == "job":
            j = jobs.get(db, ws, obj_id)
            if j:
                return j["status_info"]
        if kind == "media_job":
            from irris import watch
            r = db.one("SELECT * FROM media_jobs WHERE id = ? AND workspace_id = ?", [obj_id, ws])
            if r:
                return watch.job_api(r)["status_info"]
        if kind == "camera":
            r = db.one("SELECT * FROM cameras WHERE id = ? AND workspace_id = ?", [obj_id, ws])
            if r:
                return camera_api(r)["status_info"]
        if kind in ("source", "mcp_server", "mcp_sync"):
            sid = obj_id.split(":")[0]
            r = db.one("SELECT * FROM sources WHERE id = ? AND workspace_id = ?", [sid, ws])
            if r and r["kind"] == "mcp":
                from irris.api_mcp_sources import server_api
                srv = server_api(r)
                if kind == "mcp_sync":
                    sync = next((s for s in srv["syncs"] if s["id"] == obj_id.partition(":")[2]), None)
                    if sync:
                        return sync["status_info"]
                else:
                    return srv["status_info"]
            elif r:
                return source_api(r)["status_info"]
        if kind == "instrument":
            from irris import instruments
            return instruments.get(db, ws, obj_id)["status_info"]
        if kind == "webhook":
            return webhooks.api(webhooks.get(db, ws, obj_id), db=db)["status_info"]
        if kind == "onboarding":
            return onboarding.api(db, ws)["status_info"]
    except (store.NotFound, webhooks.WebhookError, runs.RunError):
        pass
    if kind not in ("run", "job", "media_job", "camera", "source", "mcp_server", "mcp_sync", "instrument", "webhook",
                    "onboarding"):
        raise HTTPException(400, f"unknown kind {kind!r}")
    raise HTTPException(404, f"no {kind} with id {obj_id}")


# ================================================================ change feed
@router.get("/changes")
def changes(cursor: int = 0, limit: int = 100, kinds: str = "", c: Ctx = Depends(ctx)):
    """Everything that changed after `cursor`, oldest first. Keep the returned cursor and ask again; `more` says
    whether to ask straight away. Kinds include records.written, step.failed, deviation.opened, run.completed,
    batch.released, approval.*, instrument.out_of_limits, sync.failed."""
    return ops.changes(c.db, c.ws_id, cursor, limit, [k for k in kinds.split(",") if k] or None)


# ================================================================ feedback and audit
@router.post("/feedback")
async def feedback(request: Request, c: Ctx = Depends(ctx)):
    """Tell the Irris team something (a bug, a missing feature, a wrong answer). Free, for people and agents."""
    b = await body_of(request)
    try:
        return ops.add_feedback(c.db, c.ws_id, actor_of(c), str(b.get("text") or ""),
                                b.get("context") if isinstance(b.get("context"), dict) else None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/feedback")
def feedback_list(c: Ctx = Depends(ctx)):
    _need_admin(c)
    return {"feedback": ops.feedback(c.db, c.ws_id)}


@router.get("/audit")
def audit(kind: str = "", ref: str = "", c: Ctx = Depends(ctx)):
    _need_admin(c)
    return {"entries": ops.audit_log(c.db, c.ws_id, kind=kind, ref=ref), "chain": ops.verify_audit(c.db, c.ws_id)}


# ================================================================ batch-record sign-off (electronic signature, lite)
@router.post("/runs/{run_id}/signoff")
async def signoff(run_id: str, request: Request, c: Ctx = Depends(ctx)):
    """Sign a finished run's batch record: {"meaning": "reviewed"|"approved", "password", "comment"?}.
    A signature is a person re-entering their password (API keys can't sign). "approved" releases the batch."""
    b = await body_of(request)
    if not c.user:
        raise HTTPException(403, "only a signed-in person can sign; API keys can't")
    meaning = b.get("meaning")
    if meaning not in ("reviewed", "approved"):
        raise HTTPException(400, "meaning must be reviewed or approved")
    if auth.too_many(f"sign:{c.user['id']}", limit=5, window=600):
        raise HTTPException(429, "too many attempts; wait 10 minutes")
    try:
        auth.login(c.db, c.user["email"], str(b.get("password") or ""))
    except HTTPException:
        raise HTTPException(401, "that password isn't right")
    try:
        s = runs.state(c.db, c.ws_id, run_id)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    if s["status"] == "active":
        raise HTTPException(409, "the run is still going; finish or stop it first")
    if s["released"]:
        raise HTTPException(409, "this batch is already released")
    batch = c.db.one("SELECT id, data FROM records WHERE workspace_id = ? AND type = 'batch_record' AND ext_id = ?",
                     [c.ws_id, run_id])
    ops.decide(c.db, c.ws_id, "batch_signoff", run_id, actor=c.user["email"],
               status="approved" if meaning == "approved" else "reviewed", meaning=meaning,
               comment=str(b.get("comment") or ""), title=f"Batch record: {s['procedure']['name']} {s['label']}".strip(),
               payload={"batch_record": batch["id"] if batch else None,
                        "content_hash": ops._h(loads(batch["data"], {})) if batch else ""})
    runs.refresh_batch_record(c.db, c.ws_id, run_id)
    out = runs.state(c.db, c.ws_id, run_id)
    if out["released"]:
        ops.emit(c.db, c.ws_id, "batch.released", run_id, {"run_id": run_id, "procedure": s["procedure"]["name"],
                                                           "label": s["label"], "by": c.user["email"],
                                                           "batch_record": batch["id"] if batch else None})
    return out


# ================================================================ outgoing webhooks
@router.get("/webhooks")
def webhook_list(c: Ctx = Depends(ctx)):
    return {"webhooks": webhooks.listing(c.db, c.ws_id), "events": list(webhooks.EVENTS)}


@router.post("/webhooks")
async def webhook_create(request: Request, c: Ctx = Depends(ctx)):
    """{"url", "events"?: [...], "name"?}. Starts as a draft (sends nothing but tests) until an admin approves it.
    The signing secret is returned once."""
    _need_admin(c)
    b = await body_of(request)
    try:
        w = webhooks.create(c.db, c.ws_id, str(b.get("url") or ""), b.get("events") or [], str(b.get("name") or ""))
    except webhooks.WebhookError as e:
        raise HTTPException(400, str(e))
    ops.audit(c.db, c.ws_id, actor_of(c), "webhook.drafted", "webhook", w["id"], after={"url": w["url"], "events": w["events"]})
    return w


@router.post("/webhooks/{wid}/{action}")
async def webhook_action(wid: str, action: str, c: Ctx = Depends(ctx)):
    try:
        if action == "test":
            return await webhooks.test(c.db, c.ws_id, wid)
        _need_admin(c)
        if action == "approve":
            if not c.user:
                raise HTTPException(403, "a person approves webhooks, not an API key")
            return webhooks.set_status(c.db, c.ws_id, wid, "active", c.user["email"])
        if action == "pause":
            return webhooks.set_status(c.db, c.ws_id, wid, "paused", actor_of(c))
    except webhooks.WebhookError as e:
        raise HTTPException(404, str(e))
    raise HTTPException(400, "action must be test, approve or pause")


@router.get("/webhooks/{wid}/deliveries")
def webhook_deliveries(wid: str, c: Ctx = Depends(ctx)):
    try:
        return {"deliveries": webhooks.deliveries(c.db, c.ws_id, wid)}
    except webhooks.WebhookError as e:
        raise HTTPException(404, str(e))


@router.delete("/webhooks/{wid}")
def webhook_delete(wid: str, c: Ctx = Depends(ctx)):
    _need_admin(c)
    try:
        webhooks.delete(c.db, c.ws_id, wid, actor_of(c))
    except webhooks.WebhookError as e:
        raise HTTPException(404, str(e))
    return {"deleted": wid}


# ================================================================ draft twin
@router.get("/onboarding")
def onboarding_get(c: Ctx = Depends(ctx)):
    return onboarding.api(c.db, c.ws_id)


@router.post("/onboarding")
async def onboarding_start(request: Request, c: Ctx = Depends(ctx)):
    """{"url"} or {"answers": {"makes", "process", "market"}}: draft the twin in the background."""
    b = await body_of(request)
    try:
        return onboarding.start(c.db, c.workspace, url=str(b.get("url") or ""),
                                answers=b.get("answers") if isinstance(b.get("answers"), dict) else None,
                                actor=actor_of(c))
    except onboarding.OnboardingError as e:
        raise HTTPException(400, str(e))


@router.post("/onboarding/decide")
async def onboarding_decide(request: Request, c: Ctx = Depends(ctx)):
    """{"item": "profile"|"procedure:0"|"standard:1"|"document:2", "decision": "accept"|"reject"}"""
    b = await body_of(request)
    try:
        out = onboarding.decide(c.db, c.workspace, str(b.get("item") or ""), str(b.get("decision") or ""),
                                actor=actor_of(c))
    except onboarding.OnboardingError as e:
        raise HTTPException(400, str(e))
    return {**out, "onboarding": onboarding.api(c.db, c.ws_id)}


# ================================================================ plain-words queries
@router.post("/query/parse")
async def query_parse(request: Request, c: Ctx = Depends(ctx)):
    """{"text"} -> the structured query it means (type, subject, time window, conditions), without running it."""
    b = await body_of(request)
    text = str(b.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "send some text")
    return await asyncio.to_thread(nlquery.parse, c.db, c.ws_id, text)


@router.post("/query/run")
async def query_run(request: Request, c: Ctx = Depends(ctx)):
    """{"text"} or {"query": <a parsed query, possibly edited>}: run it."""
    b = await body_of(request)
    q = b.get("query") if isinstance(b.get("query"), dict) else None
    if q is None:
        text = str(b.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "send text or a query")
        q = await asyncio.to_thread(nlquery.parse, c.db, c.ws_id, text)
    return await asyncio.to_thread(nlquery.run, c.db, c.ws_id, q, min(int(b.get("limit") or 50), 200))


async def startup() -> None:
    await webhooks.start()


async def shutdown() -> None:
    await webhooks.stop()
