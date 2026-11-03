"""The REST API, /api/v1. The dashboard uses exactly these endpoints, so anything it does, a program can do."""
from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from irris import agent, auth, connectors, irf, llm, runs, store, vision, ops
from irris.auth import Ctx, ctx
from irris.config import settings
from irris.db import dumps, get_db, loads, new_id, now
from irris.cancel import unless_gone
from irris.security import encrypt, sha256, token

router = APIRouter(prefix="/api/v1")


def _json(request_body: Any) -> dict:
    if not isinstance(request_body, dict):
        raise HTTPException(400, "send a JSON object")
    return request_body


async def body_of(request: Request) -> dict:
    try:
        return _json(await request.json())
    except ValueError:
        raise HTTPException(400, "the body isn't valid JSON")


def actor_of(c: Ctx) -> str:
    """Who did it, for the audit log: the person's email, or the API key."""
    return c.user["email"] if c.user else f"api-key:{c.workspace.get('key_name') or 'key'}"


def _err(e: Exception) -> HTTPException:
    if isinstance(e, store.NotFound):
        return HTTPException(404, str(e))
    return HTTPException(400, str(e))


# ================================================================ accounts
def _set_cookie(request: Request, response: Response, raw: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, raw, max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
                        secure=secure and settings.cookie_secure, path="/")


def _me(user: dict) -> dict:
    db = get_db()
    return {"user": auth.user_api(user), "workspaces": auth.workspaces_of(db, user["id"])}


@router.post("/auth/signup")
async def signup(request: Request, response: Response):
    b = await body_of(request)
    ip = request.client.host if request.client else "?"
    if auth.too_many(f"signup:{ip}", limit=8, window=3600):
        raise HTTPException(429, "too many sign-ups from here. Try again later.")
    db = get_db()
    user = auth.create_user(db, str(b.get("email", "")), str(b.get("password", "")), str(b.get("name", "")))
    invite = str(b.get("invite") or "")
    inv = db.one("SELECT * FROM invites WHERE token = ?", [invite]) if invite else None
    if inv:
        db.execute("INSERT INTO members (workspace_id, user_id, role, created) VALUES (?,?,?,?)",
                   [inv["workspace_id"], user["id"], inv["role"], now()])
        db.execute("DELETE FROM invites WHERE token = ?", [invite])
    else:
        auth.create_workspace(db, user["id"], str(b.get("workspace") or f"{user['name'] or 'My'} team"),
                              str(b.get("industry") or ""), str(b.get("about") or ""))
    raw = auth.new_session(db, user["id"])
    _set_cookie(request, response, raw)
    return {**_me(user), "token": raw}


@router.post("/auth/login")
async def login(request: Request, response: Response):
    b = await body_of(request)
    ip = request.client.host if request.client else "?"
    email = str(b.get("email", "")).lower()
    if auth.too_many(f"login:{ip}") or auth.too_many(f"login:{email}"):
        raise HTTPException(429, "too many attempts. Wait 10 minutes and try again.")
    db = get_db()
    user = auth.login(db, email, str(b.get("password", "")))
    raw = auth.new_session(db, user["id"])
    _set_cookie(request, response, raw)
    return {**_me(user), "token": raw}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    raw = auth.bearer(request) or request.cookies.get(auth.COOKIE, "")
    if raw:
        auth.end_session(get_db(), raw)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(auth.current_user)):
    return _me(user)


@router.post("/me")
async def update_me(request: Request, user: dict = Depends(auth.current_user)):
    b = await body_of(request)
    db = get_db()
    if "name" in b:
        db.execute("UPDATE users SET name = ? WHERE id = ?", [str(b["name"]).strip()[:120], user["id"]])
    if b.get("new_password"):
        auth.login(db, user["email"], str(b.get("password", "")))
        if len(str(b["new_password"])) < 8:
            raise HTTPException(400, "the password needs at least 8 characters")
        from irris.security import hash_password
        db.execute("UPDATE users SET pw_hash = ? WHERE id = ?", [hash_password(str(b["new_password"])), user["id"]])
    return _me(db.one("SELECT * FROM users WHERE id = ?", [user["id"]]))


@router.post("/workspaces")
async def new_workspace(request: Request, user: dict = Depends(auth.current_user)):
    b = await body_of(request)
    ws = auth.create_workspace(get_db(), user["id"], str(b.get("name", "")), str(b.get("industry") or ""),
                               str(b.get("about") or ""))
    return workspace_api(ws, "owner")


# ================================================================ workspace
def workspace_api(ws: dict, role: str) -> dict:
    models = loads(ws.get("models"), {})
    safe = {k: {"base_url": v.get("base_url", ""), "model": v.get("model", ""), "has_key": bool(v.get("api_key"))}
            for k, v in models.items()}
    return {"id": ws["id"], "name": ws["name"], "industry": ws["industry"], "about": ws["about"],
            "instructions": ws["instructions"], "models": safe, "role": role, "created": ws["created"],
            "defaults": {"llm": llm.available(None, "llm"), "vlm": llm.available(None, "vlm"),
                         "llm_model": ", ".join(m.model for m in llm.chain(None, "llm")),
                         "vlm_model": ", ".join(m.model for m in llm.chain(None, "vlm"))}}


@router.get("/workspace")
def get_workspace(c: Ctx = Depends(ctx)):
    return workspace_api(c.workspace, c.role)


@router.patch("/workspace")
async def patch_workspace(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    c.require_admin()
    for field, limit in (("name", 120), ("industry", 120), ("about", 8000), ("instructions", 8000)):
        if field in b:
            c.db.execute(f"UPDATE workspaces SET {field} = ? WHERE id = ?", [str(b[field] or "").strip()[:limit], c.ws_id])
    if isinstance(b.get("models"), dict):
        models = loads(c.workspace.get("models"), {})
        for kind in ("llm", "vlm"):
            m = b["models"].get(kind)
            if m is None:
                continue
            if not m:                                      # {} or null -> back to the server default
                models.pop(kind, None)
                continue
            cur = models.get(kind, {})
            cur["base_url"] = str(m.get("base_url") or "").strip()
            cur["model"] = str(m.get("model") or "").strip()
            if m.get("api_key"):
                cur["api_key"] = encrypt(str(m["api_key"]).strip())
            elif not cur["base_url"]:
                cur.pop("api_key", None)
            models[kind] = cur
        c.db.execute("UPDATE workspaces SET models = ? WHERE id = ?", [dumps(models), c.ws_id])
    ws = c.db.one("SELECT * FROM workspaces WHERE id = ?", [c.ws_id])
    return workspace_api(ws, c.role)


@router.get("/members")
def members(c: Ctx = Depends(ctx)):
    rows = c.db.execute("SELECT u.id, u.email, u.name, m.role, m.created FROM members m JOIN users u ON u.id = m.user_id "
                        "WHERE m.workspace_id = ? ORDER BY m.created", [c.ws_id])
    invites = c.db.execute("SELECT token, email, role, created FROM invites WHERE workspace_id = ?", [c.ws_id])
    return {"members": rows, "invites": [{"email": i["email"], "role": i["role"], "created": i["created"],
                                          "link": f"/app/signup?invite={i['token']}"} for i in invites]}


@router.post("/members")
async def add_member(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    c.require_admin()
    email = str(b.get("email", "")).strip().lower()
    role = b.get("role") if b.get("role") in ("admin", "member") else "member"
    if "@" not in email:
        raise HTTPException(400, "enter a valid email")
    user = c.db.one("SELECT id FROM users WHERE email = ?", [email])
    if user:
        if c.db.one("SELECT 1 AS x FROM members WHERE workspace_id = ? AND user_id = ?", [c.ws_id, user["id"]]):
            raise HTTPException(409, "already a member")
        c.db.execute("INSERT INTO members (workspace_id, user_id, role, created) VALUES (?,?,?,?)",
                     [c.ws_id, user["id"], role, now()])
        return {"added": email}
    tok = token("inv_", 18)
    c.db.execute("INSERT INTO invites (token, workspace_id, email, role, created) VALUES (?,?,?,?,?)",
                 [tok, c.ws_id, email, role, now()])
    return {"invited": email, "link": f"/app/signup?invite={tok}"}


@router.delete("/members/{user_id}")
def remove_member(user_id: str, c: Ctx = Depends(ctx)):
    c.require_admin()
    row = c.db.one("SELECT role FROM members WHERE workspace_id = ? AND user_id = ?", [c.ws_id, user_id])
    if not row:
        raise HTTPException(404, "not a member")
    if row["role"] == "owner":
        raise HTTPException(400, "the owner can't be removed")
    c.db.execute("DELETE FROM members WHERE workspace_id = ? AND user_id = ?", [c.ws_id, user_id])
    return {"removed": user_id}


# ================================================================ API keys
@router.get("/keys")
def keys(c: Ctx = Depends(ctx)):
    return {"keys": c.db.execute("SELECT id, name, prefix, created, last_used FROM api_keys WHERE workspace_id = ? "
                                 "ORDER BY created DESC", [c.ws_id])}


@router.post("/keys")
async def new_key(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    c.require_admin()
    return auth.create_key(c.db, c.ws_id, str(b.get("name") or "API key"))


@router.delete("/keys/{key_id}")
def delete_key(key_id: str, c: Ctx = Depends(ctx)):
    c.require_admin()
    c.db.execute("DELETE FROM api_keys WHERE id = ? AND workspace_id = ?", [key_id, c.ws_id])
    return {"deleted": key_id}


# ================================================================ ingest (IRF)
def ingest_payload(c_db, ws_id: str, payload: Any, default_type: str, default_source: str) -> dict:
    try:
        recs = irf.normalize_many(payload, default_type=default_type or "row", default_source=default_source, now=now())
    except irf.IRFError as e:
        raise HTTPException(422, str(e))
    if len(recs) > 10_000:
        raise HTTPException(413, "at most 10,000 records per request; send the rest in another")
    res = store.write(c_db, ws_id, recs)
    return {"accepted": len(recs), **res}


async def _read_payload(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"the body is over {settings.max_upload_mb} MB")
    ctype = request.headers.get("content-type", "")
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        if "ndjson" in ctype or "jsonl" in ctype:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return json.loads(text)
    except ValueError as e:
        raise HTTPException(400, f"the body isn't valid JSON: {e}")


@router.post("/ingest")
async def ingest(request: Request, c: Ctx = Depends(ctx)):
    payload = await _read_payload(request)
    q = request.query_params
    return ingest_payload(c.db, c.ws_id, payload, q.get("type", ""), q.get("source") or "api")


# POST /ingest/file and GET /files/{id} live in api_knowledge.py (files of any size, read in the background).


@router.post("/hooks/{secret}")
async def webhook(secret: str, request: Request):
    db = get_db()
    src = db.one("SELECT * FROM sources WHERE kind = 'webhook' AND secret = ?", [sha256(secret)])
    if not src:
        raise HTTPException(404, "unknown webhook")
    payload = await _read_payload(request)
    cfg = loads(src["config"], {})
    q = request.query_params
    res = ingest_payload(db, src["workspace_id"], payload, q.get("type") or cfg.get("type") or "event",
                         q.get("source") or cfg.get("source") or f"webhook/{src['name']}")
    db.execute("UPDATE sources SET last_sync = ?, status = ? WHERE id = ?", [now(), f"received {res['accepted']}", src["id"]])
    return res


# ================================================================ records and search
@router.get("/records")
def list_records(type: str = "", subject: str = "", source: str = "", q: str = "", limit: int = 50, offset: int = 0,
                 since: float | None = None, c: Ctx = Depends(ctx)):
    if q:
        hits = store.search(c.db, c.ws_id, q, limit=min(limit, 50), types=[type] if type else None)
        return {"total": len(hits), "hits": hits}
    return store.query(c.db, c.ws_id, type=type, subject=subject, source=source, since=since, limit=limit,
                       offset=offset)


@router.get("/records/{record_id}")
def get_record(record_id: str, c: Ctx = Depends(ctx)):
    try:
        return store.get(c.db, c.ws_id, record_id)
    except store.NotFound as e:
        raise _err(e)


@router.delete("/records/{record_id}")
def delete_record(record_id: str, c: Ctx = Depends(ctx)):
    try:
        store.delete(c.db, c.ws_id, record_id)
    except store.NotFound as e:
        raise _err(e)
    return {"deleted": record_id}


@router.get("/search")
def search(q: str, limit: int = 10, type: str = "", c: Ctx = Depends(ctx)):
    return {"hits": store.search(c.db, c.ws_id, q, limit=min(limit, 50), types=[type] if type else None)}


@router.get("/stats")
def stats(c: Ctx = Depends(ctx)):
    return store.stats(c.db, c.ws_id)


@router.get("/overview")
def overview(c: Ctx = Depends(ctx)):
    s = store.stats(c.db, c.ws_id)
    cams = cameras_list(c)
    recent = store.query(c.db, c.ws_id, limit=12)["records"]
    rs = runs.list_runs(c.db, c.ws_id, limit=6)
    readings = store.query(c.db, c.ws_id, type="reading", limit=60, full=True)["records"]
    week = now() - 7 * 86400
    week_runs = [r for r in runs.list_runs(c.db, c.ws_id, limit=500) if r["started"] >= week]
    return {"workspace": workspace_api(c.workspace, c.role), "stats": s, "recent": recent, "runs": rs,
            "cameras": cams["cameras"], "procedures": len(store.procedures(c.db, c.ws_id)),
            "sources": len(c.db.execute("SELECT id FROM sources WHERE workspace_id = ?", [c.ws_id])),
            "readings": [{"time": r["time"], "subject": r["subject"], "title": r["title"],
                          "value": r["data"].get("value"), "unit": r["data"].get("unit")} for r in readings],
            "week": {"runs": len(week_runs), "done": sum(1 for r in week_runs if r["status"] == "done"),
                     "deviations": sum(r["deviations"] for r in week_runs),
                     "checks": sum(r["checks"] for r in week_runs)}}


# ================================================================ sources
def source_api(row: dict) -> dict:
    cfg = loads(row["config"], {})
    out = {"id": row["id"], "kind": row["kind"], "name": row["name"], "status": row["status"],
           "last_sync": row["last_sync"], "created": row["created"]}
    if row["kind"] == "postgres":
        out.update(tables=cfg.get("tables", []), database=cfg.get("database", ""),
                   schema=[{"table": t["table"], "rows_estimate": t.get("rows_estimate"), "columns": len(t["columns"])}
                           for t in cfg.get("schema", [])])
    if row["kind"] == "webhook":
        out.update(type=cfg.get("type", "event"), source=cfg.get("source", ""), hint=cfg.get("hint", ""))
    out["status_info"] = ops.source_status(out)
    return out


@router.get("/sources")
def sources(c: Ctx = Depends(ctx)):
    rows = c.db.execute("SELECT * FROM sources WHERE workspace_id = ? ORDER BY created DESC", [c.ws_id])
    return {"sources": [source_api(r) for r in rows]}


@router.post("/sources")
async def add_source(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    c.require_admin()
    kind = b.get("kind")
    name = str(b.get("name") or "").strip()[:80]
    sid = new_id()
    if kind == "postgres":
        url = str(b.get("url") or "").strip()
        if not url.startswith(("postgres://", "postgresql://")):
            raise HTTPException(400, "paste a Postgres connection string (postgresql://user:pass@host:5432/db)")
        try:
            info = await asyncio.to_thread(connectors.test, url)
            tables = await asyncio.to_thread(connectors.schema, url)
        except connectors.ConnectorError as e:
            raise HTTPException(400, str(e))
        cfg = {"database": info["database"], "version": info["version"], "schema": tables, "tables": []}
        c.db.execute("INSERT INTO sources (id, workspace_id, kind, name, config, secret, status, created) "
                     "VALUES (?,?,?,?,?,?,?,?)", [sid, c.ws_id, "postgres", name or info["database"], dumps(cfg),
                                                  encrypt(url), f"connected · {len(tables)} tables", now()])
        return source_api(c.db.one("SELECT * FROM sources WHERE id = ?", [sid]))
    if kind == "webhook":
        secret = token("whk_", 24)
        cfg = {"type": str(b.get("type") or "event"), "source": str(b.get("source") or ""), "hint": secret[:8]}
        c.db.execute("INSERT INTO sources (id, workspace_id, kind, name, config, secret, status, created) "
                     "VALUES (?,?,?,?,?,?,?,?)", [sid, c.ws_id, "webhook", name or "Webhook", dumps(cfg),
                                                  sha256(secret), "waiting for data", now()])
        return {**source_api(c.db.one("SELECT * FROM sources WHERE id = ?", [sid])), "url": f"/api/v1/hooks/{secret}"}
    raise HTTPException(400, "kind must be postgres or webhook")


def _source(c: Ctx, source_id: str) -> dict:
    row = c.db.one("SELECT * FROM sources WHERE id = ? AND workspace_id = ?", [source_id, c.ws_id])
    if not row:
        raise HTTPException(404, "source not found")
    return row


@router.patch("/sources/{source_id}")
async def patch_source(source_id: str, request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    c.require_admin()
    row = _source(c, source_id)
    cfg = loads(row["config"], {})
    if "name" in b:
        c.db.execute("UPDATE sources SET name = ? WHERE id = ?", [str(b["name"]).strip()[:80], source_id])
    if row["kind"] == "postgres" and isinstance(b.get("tables"), list):
        known = {t["table"] for t in cfg.get("schema", [])}
        cfg["tables"] = [t for t in b["tables"] if t in known]
    if row["kind"] == "webhook":
        for k in ("type", "source"):
            if k in b:
                cfg[k] = str(b[k] or "")
    c.db.execute("UPDATE sources SET config = ? WHERE id = ?", [dumps(cfg), source_id])
    return source_api(_source(c, source_id))


@router.post("/sources/{source_id}/sync")
async def sync_source(source_id: str, c: Ctx = Depends(ctx)):
    row = _source(c, source_id)
    if row["kind"] != "postgres":
        raise HTTPException(400, "only database sources sync")
    from irris.security import decrypt
    url, cfg = decrypt(row["secret"]), loads(row["config"], {})
    try:
        cfg["schema"] = await asyncio.to_thread(connectors.schema, url)
        total = 0
        for table in cfg.get("tables", []):
            recs = await asyncio.to_thread(connectors.sync_table, url, table, f"db/{row['name']}", now())
            if recs:
                store.write(c.db, c.ws_id, recs)
            total += len(recs)
    except connectors.ConnectorError as e:
        c.db.execute("UPDATE sources SET status = ? WHERE id = ?", [f"error: {e}"[:200], source_id])
        ops.emit(c.db, c.ws_id, "sync.failed", source_id, {"source": row["name"], "kind": "postgres", "error": str(e)[:300]})
        raise HTTPException(400, str(e))
    status = f"synced {total} rows from {len(cfg.get('tables', []))} tables" if cfg.get("tables") else \
        f"connected · {len(cfg['schema'])} tables (live queries only)"
    c.db.execute("UPDATE sources SET config = ?, status = ?, last_sync = ? WHERE id = ?",
                 [dumps(cfg), status, now(), source_id])
    return source_api(_source(c, source_id))


@router.post("/sources/{source_id}/query")
async def query_source(source_id: str, request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    row = _source(c, source_id)
    if row["kind"] != "postgres":
        raise HTTPException(400, "only database sources take queries")
    from irris.security import decrypt
    try:
        return await asyncio.to_thread(connectors.run, decrypt(row["secret"]), str(b.get("sql", "")))
    except connectors.ConnectorError as e:
        raise HTTPException(400, str(e))


@router.get("/sources/{source_id}/schema")
def source_schema(source_id: str, c: Ctx = Depends(ctx)):
    return {"tables": loads(_source(c, source_id)["config"], {}).get("schema", [])}


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, c: Ctx = Depends(ctx)):
    c.require_admin()
    _source(c, source_id)
    c.db.execute("DELETE FROM sources WHERE id = ?", [source_id])
    return {"deleted": source_id}


# ================================================================ training: procedures, lessons, instructions
@router.get("/procedures")
def list_procedures(c: Ctx = Depends(ctx)):
    return {"procedures": store.procedures(c.db, c.ws_id)}


@router.post("/procedures")
async def create_procedure(request: Request, c: Ctx = Depends(ctx)):
    try:
        out = store.save_procedure(c.db, c.ws_id, await body_of(request))
    except (irf.IRFError, store.NotFound) as e:
        raise _err(e)
    ops.audit(c.db, c.ws_id, actor_of(c), "procedure.created", "procedure", out["id"], after=out["steps"], detail=out["name"])
    return out


@router.get("/procedures/{proc_id}")
def get_procedure(proc_id: str, c: Ctx = Depends(ctx)):
    try:
        return store.procedure(c.db, c.ws_id, proc_id)
    except store.NotFound as e:
        raise _err(e)


@router.put("/procedures/{proc_id}")
async def update_procedure(proc_id: str, request: Request, c: Ctx = Depends(ctx)):
    try:
        before = store.procedure(c.db, c.ws_id, proc_id)
        out = store.save_procedure(c.db, c.ws_id, await body_of(request), record_id=proc_id)
    except (irf.IRFError, store.NotFound) as e:
        raise _err(e)
    ops.audit(c.db, c.ws_id, actor_of(c), "procedure.updated", "procedure", proc_id, before=before["steps"],
              after=out["steps"], detail=out["name"])
    return out


@router.delete("/procedures/{proc_id}")
def delete_procedure(proc_id: str, c: Ctx = Depends(ctx)):
    try:
        before = store.procedure(c.db, c.ws_id, proc_id)
        store.delete(c.db, c.ws_id, proc_id)
    except store.NotFound as e:
        raise _err(e)
    ops.audit(c.db, c.ws_id, actor_of(c), "procedure.deleted", "procedure", proc_id, before=before["steps"],
              detail=before["name"])
    return {"deleted": proc_id}


@router.post("/procedures/{proc_id}/draft-checks")
async def draft_checks(proc_id: str, c: Ctx = Depends(ctx)):
    """Yes/no camera questions, typical errors, kind, severity and reversibility for every step (a proposal: save it
    with PUT /procedures/{id})."""
    from irris import learning
    try:
        return await learning.draft_checks(c.db, c.workspace, proc_id)
    except store.NotFound as e:
        raise _err(e)
    except llm.ModelError as e:
        raise HTTPException(502 if "needs a reasoning model" not in str(e) else 400, str(e))


@router.post("/procedures/{proc_id}/learn")
async def learn_procedure(proc_id: str, c: Ctx = Depends(ctx)):
    """Step order, typical durations and what "done" looks like, learned from the procedure's finished runs
    (a proposal: save it with PUT /procedures/{id})."""
    from irris import learning
    try:
        return await learning.learn(c.db, c.workspace, proc_id)
    except store.NotFound as e:
        raise _err(e)


@router.patch("/procedures/{proc_id}/steps/{index}")
async def patch_step(proc_id: str, index: int, request: Request, c: Ctx = Depends(ctx)):
    """Partial update of one step (zones, instrument links, questions...)."""
    b = await body_of(request)
    try:
        p = store.procedure(c.db, c.ws_id, proc_id)
        if not 0 <= index < len(p["steps"]):
            raise HTTPException(404, "no such step")
        steps = [dict(s) for s in p["steps"]]
        for k, v in b.items():
            if v is None:
                steps[index].pop(k, None)
            else:
                steps[index][k] = v
        return store.save_procedure(c.db, c.ws_id, {**p, "steps": steps}, record_id=proc_id)
    except (irf.IRFError, store.NotFound) as e:
        raise _err(e)


@router.post("/procedures/draft")
async def draft_procedure(request: Request, c: Ctx = Depends(ctx)):
    """Draft an SOP from a description and the team's documents (the team edits it before saving)."""
    b = await body_of(request)
    goal = str(b.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "describe the procedure")
    models = llm.chain(c.workspace, "llm")
    if not models:
        raise HTTPException(400, "drafting needs a reasoning model (Settings → Models)")
    hits = store.search(c.db, c.ws_id, goal, limit=6, types=["document", "lesson", "procedure"])
    ctx_text = "\n\n".join(f"[{h['title'] or h['type']}{', p. ' + str(h['page']) if h['page'] else ''}]\n{h['text'][:1200]}"
                           for h in hits)
    prompt = ("Draft a standard operating procedure as JSON: {\"name\": str, \"description\": str, \"steps\": "
              "[{\"instruction\": str, \"check\": str (what correct looks like on camera), \"caution\"?: str, "
              "\"reading\"?: {\"label\": str, \"min\": number, \"max\": number, \"unit\": str}}]}. 4-15 steps, each one "
              "physical action a camera can verify. Use the team's documents when they cover it; never invent limits "
              "they don't give (leave reading out instead).\n\nProcedure: " + goal +
              ("\n\nTeam documents:\n" + ctx_text if ctx_text else ""))
    try:
        msg = await llm.chat_chain(models, [{"role": "user", "content": prompt}], json_mode=True, max_tokens=2500)
    except llm.ModelError as e:
        raise HTTPException(502, str(e))
    out = llm.parse_json(msg.get("content") or "")
    try:
        out["steps"] = store.clean_steps(out.get("steps"))
    except irf.IRFError:
        raise HTTPException(502, "the model didn't return usable steps; try describing it differently")
    return {"name": str(out.get("name") or goal[:80]), "description": str(out.get("description") or ""),
            "steps": out["steps"], "sources": [h["title"] for h in hits]}


@router.get("/lessons")
def list_lessons(c: Ctx = Depends(ctx)):
    return {"lessons": store.lessons(c.db, c.ws_id)}


@router.post("/lessons")
async def create_lesson(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    try:
        return store.add_lesson(c.db, c.ws_id, str(b.get("text", "")), str(b.get("subject") or ""),
                                source=f"team/{c.user['email']}" if c.user else "api")
    except irf.IRFError as e:
        raise _err(e)


@router.delete("/lessons/{lesson_id}")
def delete_lesson(lesson_id: str, c: Ctx = Depends(ctx)):
    try:
        store.delete(c.db, c.ws_id, lesson_id)
    except store.NotFound as e:
        raise _err(e)
    return {"deleted": lesson_id}


@router.get("/training/export")
def training_export(c: Ctx = Depends(ctx)):
    """JSONL for fine-tuning: every conversation in chat format, then lessons and procedures."""
    def lines():
        convs = c.db.execute("SELECT id FROM conversations WHERE workspace_id = ? ORDER BY created", [c.ws_id])
        for conv in convs:
            msgs = c.db.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id", [conv["id"]])
            if len(msgs) >= 2:
                yield dumps({"messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
                             "conversation_id": conv["id"]}) + "\n"
        for l in store.lessons(c.db, c.ws_id, limit=10_000):
            yield dumps({"lesson": l["text"], "subject": l["subject"]}) + "\n"
        for p in store.procedures(c.db, c.ws_id):
            yield dumps({"procedure": p["name"], "steps": p["steps"]}) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="irris-training.jsonl"'})


# ================================================================ assistant
@router.post("/ask")
async def ask(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    message = str(b.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "send a message")
    camera_id = str(b.get("camera_id") or "")
    if camera_id and not c.db.one("SELECT id FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, c.ws_id]):
        raise HTTPException(404, "camera not found")
    run_ctx = ""
    if b.get("run_id"):
        try:
            s = runs.state(c.db, c.ws_id, str(b["run_id"]))
            if s["status"] == "active":
                st = s["steps"][s["step"]]
                run_ctx = (f"[They're running '{s['procedure']['name']}', step {s['step'] + 1} of {len(s['steps'])}: "
                           f"{st['instruction']}]")
        except store.NotFound:
            pass
    kwargs = dict(conversation_id=b.get("conversation_id") or None, user_id=c.user_id, camera_id=camera_id,
                  frame=vision.hub.get(camera_id) if camera_id else None, context=run_ctx,
                  scene=str(b.get("scene") or "")[:800])
    if not b.get("stream"):
        try:
            return await agent.answer(c.db, c.workspace, message, **kwargs)
        except store.NotFound as e:
            raise _err(e)

    q: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await q.put((event, data))

    async def work():
        try:
            res = await agent.answer(c.db, c.workspace, message, emit=emit, **kwargs)
            await q.put(("done", res))
        except Exception as e:  # noqa: BLE001
            await q.put(("error", {"message": str(e)[:300]}))

    task = asyncio.create_task(work())

    async def stream():
        try:
            while True:
                event, data = await q.get()
                yield f"event: {event}\ndata: {dumps(data)}\n\n"
                if event in ("done", "error"):
                    break
        finally:
            if not task.done():
                await task          # finish and save the turn even if the client left

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@router.get("/conversations")
def conversations(c: Ctx = Depends(ctx)):
    where, params = "workspace_id = ?", [c.ws_id]
    if c.user:
        where += " AND (user_id = ? OR user_id = '')"
        params.append(c.user_id)
    rows = c.db.execute(f"SELECT id, title, created, updated FROM conversations WHERE {where} "
                        f"ORDER BY updated DESC LIMIT 100", params)
    return {"conversations": rows}


@router.get("/conversations/{conv_id}")
def conversation(conv_id: str, c: Ctx = Depends(ctx)):
    conv = c.db.one("SELECT * FROM conversations WHERE id = ? AND workspace_id = ?", [conv_id, c.ws_id])
    if not conv:
        raise HTTPException(404, "conversation not found")
    msgs = c.db.execute("SELECT id, role, content, meta, created FROM messages WHERE conversation_id = ? ORDER BY id",
                        [conv_id])
    return {"id": conv["id"], "title": conv["title"],
            "messages": [{**m, "meta": loads(m["meta"], {})} for m in msgs]}


@router.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: str, c: Ctx = Depends(ctx)):
    c.db.execute("DELETE FROM messages WHERE conversation_id IN "
                 "(SELECT id FROM conversations WHERE id = ? AND workspace_id = ?)", [conv_id, c.ws_id])
    c.db.execute("DELETE FROM conversations WHERE id = ? AND workspace_id = ?", [conv_id, c.ws_id])
    return {"deleted": conv_id}


# ================================================================ cameras
def camera_api(row: dict) -> dict:
    age = vision.hub.age(row["id"])
    live = age is not None and age < vision.FRESH_SECONDS
    out = {"id": row["id"], "name": row["name"], "kind": row["kind"], "created": row["created"],
           "live": live if row["kind"] == "phone" else None, "last_frame_age": age}
    out["status_info"] = ops.camera_status(out)
    return out


def cameras_list(c: Ctx) -> dict:
    rows = c.db.execute("SELECT * FROM cameras WHERE workspace_id = ? ORDER BY created", [c.ws_id])
    return {"cameras": [camera_api(r) for r in rows]}


@router.get("/cameras")
def cameras(c: Ctx = Depends(ctx)):
    return cameras_list(c)


@router.post("/cameras")
async def add_camera(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    kind = b.get("kind") or "phone"
    name = str(b.get("name") or "").strip()[:80]
    cid = new_id()
    if kind == "url":
        url = str(b.get("url") or "").strip()
        if not url.startswith(("http://", "https://", "rtsp://")):
            raise HTTPException(400, "camera URLs start with http://, https:// or rtsp://")
        try:
            frame = await vision.fetch_external(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"couldn't get a picture from that URL: {str(e)[:160]}")
        c.db.execute("INSERT INTO cameras (id, workspace_id, name, kind, url, created) VALUES (?,?,?,?,?,?)",
                     [cid, c.ws_id, name or "Camera", "url", encrypt(url), now()])
        vision.hub.put(cid, frame)
        return camera_api(c.db.one("SELECT * FROM cameras WHERE id = ?", [cid]))
    if kind != "phone":
        raise HTTPException(400, "kind must be phone or url")
    raw = token("cam_", 24)
    c.db.execute("INSERT INTO cameras (id, workspace_id, name, kind, token_hash, created) VALUES (?,?,?,?,?,?)",
                 [cid, c.ws_id, name or "Phone", "phone", sha256(raw), now()])
    return {**camera_api(c.db.one("SELECT * FROM cameras WHERE id = ?", [cid])), "pair_url": f"/app/cam?t={raw}",
            "token": raw}


@router.post("/cameras/{camera_id}/pair")
def repair_camera(camera_id: str, c: Ctx = Depends(ctx)):
    row = c.db.one("SELECT * FROM cameras WHERE id = ? AND workspace_id = ? AND kind = 'phone'", [camera_id, c.ws_id])
    if not row:
        raise HTTPException(404, "camera not found")
    raw = token("cam_", 24)
    c.db.execute("UPDATE cameras SET token_hash = ? WHERE id = ?", [sha256(raw), camera_id])
    return {"pair_url": f"/app/cam?t={raw}", "token": raw}


@router.delete("/cameras/{camera_id}")
def delete_camera(camera_id: str, c: Ctx = Depends(ctx)):
    c.db.execute("DELETE FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, c.ws_id])
    return {"deleted": camera_id}


@router.get("/cameras/{camera_id}/frame.jpg")
async def camera_frame(camera_id: str, c: Ctx = Depends(ctx)):
    frame = await agent.camera_frame(c.db, c.ws_id, camera_id)
    if frame is None:
        raise HTTPException(404, "no live frame")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/cameras/{camera_id}/frame")
async def push_frame_signed_in(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    """A signed-in browser streaming its own camera (Live → This device)."""
    if not c.db.one("SELECT id FROM cameras WHERE id = ? AND workspace_id = ?", [camera_id, c.ws_id]):
        raise HTTPException(404, "camera not found")
    jpeg = await request.body()
    if not jpeg[:2] == b"\xff\xd8" or len(jpeg) > 4_000_000:
        raise HTTPException(400, "send one JPEG frame (under 4 MB)")
    vision.hub.put(camera_id, jpeg)
    return {"ok": True}


@router.post("/cam/frame")
async def push_frame_paired(request: Request, t: str = ""):
    """A paired phone (no account on the phone; the pairing token is the credential)."""
    db = get_db()
    row = db.one("SELECT id, name, workspace_id FROM cameras WHERE token_hash = ? AND kind = 'phone'", [sha256(t)])
    if not row:
        raise HTTPException(401, "this pairing link is no longer valid. Pair the phone again.")
    jpeg = await request.body()
    if not jpeg[:2] == b"\xff\xd8" or len(jpeg) > 4_000_000:
        raise HTTPException(400, "send one JPEG frame (under 4 MB)")
    vision.hub.put(row["id"], jpeg)
    return {"ok": True, "camera": row["name"]}


@router.get("/cam/info")
def paired_info(t: str = ""):
    db = get_db()
    row = db.one("SELECT c.id, c.name, w.name AS workspace FROM cameras c JOIN workspaces w ON w.id = c.workspace_id "
                 "WHERE c.token_hash = ? AND c.kind = 'phone'", [sha256(t)])
    if not row:
        raise HTTPException(401, "this pairing link is no longer valid. Pair the phone again.")
    return row


@router.post("/cameras/{camera_id}/look")
async def camera_look(camera_id: str, request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    frame = await agent.camera_frame(c.db, c.ws_id, camera_id)
    if frame is None:
        raise HTTPException(409, "no live frame from that camera")
    models = llm.chain(c.workspace, "vlm")
    if not models:
        raise HTTPException(400, "no vision model is set up (Settings → Models)")
    try:
        return await unless_gone(request, vision.look(models, frame, str(b.get("question") or "What do you see?"),
                                                      workspace_id=c.ws_id, scene=str(b.get("scene") or ""),
                                                      background=bool(b.get("background"))))
    except vision.VisionBusy as e:
        raise HTTPException(409, str(e))
    except vision.VisionTimeout as e:
        raise HTTPException(504, str(e))
    except llm.ModelError as e:
        raise HTTPException(502, str(e))


# ================================================================ guided runs
@router.get("/runs")
def list_runs(c: Ctx = Depends(ctx)):
    return {"runs": runs.list_runs(c.db, c.ws_id, limit=100)}


@router.post("/runs")
async def start_run(request: Request, c: Ctx = Depends(ctx)):
    b = await body_of(request)
    try:
        return runs.start(c.db, c.ws_id, str(b.get("procedure_id") or ""), camera_id=str(b.get("camera_id") or ""),
                          user_id=c.user_id, label=str(b.get("label") or ""))
    except (store.NotFound, runs.RunError) as e:
        raise _err(e)


@router.get("/runs/{run_id}")
def get_run(run_id: str, c: Ctx = Depends(ctx)):
    try:
        return runs.state(c.db, c.ws_id, run_id)
    except store.NotFound as e:
        raise _err(e)


@router.post("/runs/{run_id}/check")
async def check_run(run_id: str, request: Request, c: Ctx = Depends(ctx)):
    """Check the current step through the camera (body: {"camera_id"?}), or mark it by hand
    (body: {"result": "pass|fail|skip", "note"?, "reading"?: {"value", "unit"}})."""
    b = await body_of(request)
    try:
        if b.get("result"):
            reading = None
            if isinstance(b.get("reading"), dict) and b["reading"].get("value") not in (None, ""):
                reading = {"value": float(b["reading"]["value"]), "unit": str(b["reading"].get("unit") or ""),
                           "confidence": 1.0}
            state = runs.state(c.db, c.ws_id, run_id)
            target = int(b["step"]) if b.get("step") is not None else state["step"]
            if reading and state["status"] == "active" and 0 <= target < len(state["steps"]):
                ok, why = vision.judge_reading(state["steps"][target], reading)
                if ok is False and b["result"] == "pass":
                    b["result"], b["note"] = "fail", why
            by = c.user["email"] if c.user else "api"
            run = runs.record(c.db, c.ws_id, run_id, str(b["result"]), say=str(b.get("note") or ""),
                              reading=reading, by=by, note=str(b.get("note") or ""),
                              step=int(b["step"]) if b.get("step") is not None else None)
            if run.get("recovery") is None and runs._wants_recovery(c.db, c.ws_id, run_id):
                runs._schedule_recovery(c.db, c.workspace, run_id)
            return {"check": {"result": b["result"], "say": b.get("note") or "", "reading": reading}, "run": run}
        state = runs.state(c.db, c.ws_id, run_id)
        cam = str(b.get("camera_id") or state["camera_id"] or "")
        frame = await agent.camera_frame(c.db, c.ws_id, cam)
        if frame is None:
            raise HTTPException(409, "no live camera frame to check. Start the camera first.")
        return await unless_gone(request, runs.vision_check(c.db, c.workspace, run_id, frame,
                                                            scene=str(b.get("scene") or ""),
                                                            step=int(b["step"]) if b.get("step") is not None else None,
                                                            hands=b.get("hands")))
    except (store.NotFound, runs.RunError, ValueError) as e:
        raise _err(e)
    except vision.VisionTimeout as e:
        raise HTTPException(504, str(e))
    except llm.ModelError as e:
        raise HTTPException(502, str(e))


@router.post("/runs/{run_id}/recover")
async def recover_run(run_id: str, request: Request, c: Ctx = Depends(ctx)):
    """{} or {"step", "error"}: propose a recovery plan (what happened, what it breaks, why, how to fix it).
    {"accept": true}: insert the proposed recovery steps into the run (or reopen the step);
    {"accept": false} or {"dismiss": true}: dismiss it."""
    b = await body_of(request)
    try:
        if b.get("dismiss"):
            return runs.accept_recovery(c.db, c.ws_id, run_id, accept=False, actor=actor_of(c))
        if "accept" in b:
            return runs.accept_recovery(c.db, c.ws_id, run_id, accept=bool(b["accept"]), actor=actor_of(c))
        return await runs.propose_recovery(c.db, c.workspace, run_id,
                                           step=int(b["step"]) if b.get("step") is not None else None,
                                           error=str(b.get("error") or ""))
    except (store.NotFound, runs.RunError, ValueError) as e:
        raise _err(e)


def move_run(run_id: str, action: str, c: Ctx) -> dict:
    try:
        return runs.move(c.db, c.ws_id, run_id, action)
    except (store.NotFound, runs.RunError) as e:
        raise _err(e)


@router.post("/runs/{run_id}/back")
def run_back(run_id: str, c: Ctx = Depends(ctx)):
    """Go back a step (reopens the previous step)."""
    return move_run(run_id, "back", c)


@router.post("/runs/{run_id}/stop")
def run_stop(run_id: str, c: Ctx = Depends(ctx)):
    """Stop the run; its batch record lists what wasn't done."""
    return move_run(run_id, "stop", c)


# ================================================================ health and the standard
@router.get("/health")
def health():
    db = get_db()
    db.one("SELECT 1 AS ok")
    return {"ok": True, "database": "postgres" if db.pg else "sqlite", "version": "1.0",
            "llm": llm.available(None, "llm"), "vlm": llm.available(None, "vlm"), "time": time.time()}


@router.get("/irf")
def irf_spec():
    from irris.config import ROOT
    path = ROOT / "docs" / "IRF.md"
    return PlainTextResponse(path.read_text(encoding="utf-8") if path.exists() else "", media_type="text/markdown")
