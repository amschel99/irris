"""Connected MCP servers as a data source: connect, list tools, call them, and sync their data into records.

Stored as `sources` rows of kind "mcp": config = {url, server, tools, resources, syncs}, secret = encrypted headers.
A background loop runs sync rules that are due (a tool call or a resource read every N minutes) and writes what
comes back as IRF records, upserting by the server's own ids when it gives them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from irris import irf, mcp_client, ops, store
from irris.auth import Ctx, ctx
from irris.db import Database, dumps, get_db, loads, new_id, now
from irris.security import decrypt, encrypt

router = APIRouter(prefix="/api/v1")
log = logging.getLogger("irris.mcp_sources")
MIN_EVERY = 5            # minutes
_task: asyncio.Task | None = None


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        raise HTTPException(400, "the body isn't valid JSON")
    if not isinstance(b, dict):
        raise HTTPException(400, "send a JSON object")
    return b


def _headers(b: dict) -> dict:
    h = {str(k): str(v) for k, v in (b.get("headers") or {}).items() if k and v} if isinstance(b.get("headers"), dict) else {}
    tok = str(b.get("token") or "").strip()
    if tok:
        h["Authorization"] = tok if tok.lower().startswith(("bearer ", "basic ")) else f"Bearer {tok}"
    return h


def server_api(row: dict) -> dict:
    cfg = loads(row["config"], {})
    syncs = [{**s, "approved": bool(s.get("approved")), "status_info": ops.sync_status(s)} for s in cfg.get("syncs", [])]
    out = {"id": row["id"], "name": row["name"], "url": cfg.get("url", ""), "server": cfg.get("server", {}),
            "status": row["status"], "last_sync": row["last_sync"], "created": row["created"],
            "has_auth": bool(row["secret"]), "instructions": cfg.get("instructions", ""),
            "tools": cfg.get("tools", []), "resources": cfg.get("resources", []), "syncs": syncs}
    out["status_info"] = ops.server_status(out)
    return out


def _row(db: Database, ws_id: str, server_id: str) -> dict:
    row = db.one("SELECT * FROM sources WHERE id = ? AND workspace_id = ? AND kind = 'mcp'", [server_id, ws_id])
    if not row:
        raise HTTPException(404, "MCP server not found")
    return row


def _conn(row: dict) -> tuple[str, dict]:
    cfg = loads(row["config"], {})
    return cfg.get("url", ""), (loads(decrypt(row["secret"]), {}) if row["secret"] else {})


def servers(db: Database, ws_id: str) -> list[dict]:
    return [server_api(r) for r in db.execute(
        "SELECT * FROM sources WHERE workspace_id = ? AND kind = 'mcp' ORDER BY created", [ws_id])]


async def call(db: Database, ws_id: str, server_id: str, tool: str, arguments: dict | None) -> dict:
    """Used by the assistant's mcp_call tool."""
    row = _row(db, ws_id, server_id)
    cfg = loads(row["config"], {})
    if tool not in {t["name"] for t in cfg.get("tools", [])}:
        raise mcp_client.MCPError(f"{row['name']} has no tool named {tool!r}; list them with mcp_servers")
    url, headers = _conn(row)
    return await mcp_client.call_tool(url, headers, tool, arguments or {})


# ================================================================ routes
@router.get("/mcp-servers")
def list_servers(c: Ctx = Depends(ctx)):
    return {"servers": servers(c.db, c.ws_id)}


@router.post("/mcp-servers")
async def add_server(request: Request, c: Ctx = Depends(ctx)):
    c.require_admin()
    b = await _body(request)
    url = str(b.get("url") or "").strip()
    headers = _headers(b)
    try:
        info = await mcp_client.describe(url, headers)
    except mcp_client.MCPError as e:
        raise HTTPException(400, str(e))
    sid = new_id()
    name = str(b.get("name") or info["server"].get("title") or info["server"].get("name") or "MCP server").strip()[:80]
    cfg = {"url": url, **info, "syncs": []}
    status = f"connected · {len(info['tools'])} tools" + (f", {len(info['resources'])} resources" if info["resources"] else "")
    c.db.execute("INSERT INTO sources (id, workspace_id, kind, name, config, secret, status, created) VALUES (?,?,?,?,?,?,?,?)",
                 [sid, c.ws_id, "mcp", name, dumps(cfg), encrypt(dumps(headers)) if headers else "", status, now()])
    return server_api(_row(c.db, c.ws_id, sid))


@router.post("/mcp-servers/{server_id}/refresh")
async def refresh_server(server_id: str, c: Ctx = Depends(ctx)):
    row = _row(c.db, c.ws_id, server_id)
    url, headers = _conn(row)
    cfg = loads(row["config"], {})
    try:
        info = await mcp_client.describe(url, headers)
    except mcp_client.MCPError as e:
        c.db.execute("UPDATE sources SET status = ? WHERE id = ?", [f"error: {e}"[:200], server_id])
        raise HTTPException(400, str(e))
    cfg.update(info)
    c.db.execute("UPDATE sources SET config = ?, status = ? WHERE id = ?",
                 [dumps(cfg), f"connected · {len(info['tools'])} tools", server_id])
    return server_api(_row(c.db, c.ws_id, server_id))


@router.patch("/mcp-servers/{server_id}")
async def update_server(server_id: str, request: Request, c: Ctx = Depends(ctx)):
    c.require_admin()
    b = await _body(request)
    row = _row(c.db, c.ws_id, server_id)
    if "name" in b:
        c.db.execute("UPDATE sources SET name = ? WHERE id = ?", [str(b["name"]).strip()[:80], server_id])
    if "token" in b or "headers" in b:
        h = _headers(b)
        c.db.execute("UPDATE sources SET secret = ? WHERE id = ?", [encrypt(dumps(h)) if h else "", server_id])
    return server_api(_row(c.db, c.ws_id, row["id"]))


@router.delete("/mcp-servers/{server_id}")
def delete_server(server_id: str, c: Ctx = Depends(ctx)):
    c.require_admin()
    _row(c.db, c.ws_id, server_id)
    c.db.execute("DELETE FROM sources WHERE id = ?", [server_id])
    return {"deleted": server_id}


@router.post("/mcp-servers/{server_id}/call")
async def call_server(server_id: str, request: Request, c: Ctx = Depends(ctx)):
    """Try a tool (or read a resource with {"resource": uri}). {"save": true} also stores what came back."""
    b = await _body(request)
    row = _row(c.db, c.ws_id, server_id)
    url, headers = _conn(row)
    try:
        if b.get("resource"):
            result = await mcp_client.read_resource(url, headers, str(b["resource"]))
            label = str(b["resource"])
        else:
            result = await call(c.db, c.ws_id, server_id, str(b.get("tool") or ""), b.get("arguments") or {})
            label = str(b.get("tool"))
    except mcp_client.MCPError as e:
        raise HTTPException(400, str(e))
    out = {"result": mcp_client.compact(result, limit=20_000)}
    if b.get("save"):
        try:
            recs = mcp_client.to_records(result, source=f"mcp/{row['name']}/{label}", type=str(b.get("type") or "row"),
                                         title=f"{row['name']}: {label}", now=now())
        except irf.IRFError as e:
            raise HTTPException(422, f"couldn't turn that into records: {e}")
        out["saved"] = store.write(c.db, c.ws_id, recs) if recs else {"created": 0, "updated": 0, "ids": []}
    return out


@router.post("/mcp-servers/{server_id}/syncs")
async def add_sync(server_id: str, request: Request, c: Ctx = Depends(ctx)):
    c.require_admin()
    b = await _body(request)
    row = _row(c.db, c.ws_id, server_id)
    cfg = loads(row["config"], {})
    tool, resource = str(b.get("tool") or ""), str(b.get("resource") or "")
    if not tool and not resource:
        raise HTTPException(400, "choose a tool or a resource to sync")
    if tool and tool not in {t["name"] for t in cfg.get("tools", [])}:
        raise HTTPException(400, f"no tool named {tool!r}")
    every = max(MIN_EVERY, int(b.get("every_minutes") or 60))
    sync = {"id": new_id(), "tool": tool, "resource": resource, "arguments": b.get("arguments") or {},
            "every_minutes": every, "type": str(b.get("type") or "row"), "source": str(b.get("source") or ""),
            "next_run": now(), "last_run": None, "last_status": "waiting for approval", "approved": False,
            "created_by": c.user["email"] if c.user else "api key"}
    cfg.setdefault("syncs", []).append(sync)
    c.db.execute("UPDATE sources SET config = ? WHERE id = ?", [dumps(cfg), server_id])
    ops.audit(c.db, c.ws_id, c.user["email"] if c.user else "api key", "mcp_sync.drafted", "mcp_sync",
              f"{server_id}:{sync['id']}", after={k: sync[k] for k in ("tool", "resource", "arguments", "every_minutes", "type")})
    return {**sync, "status_info": ops.sync_status(sync)}


@router.post("/mcp-servers/{server_id}/syncs/{sync_id}/approve")
async def approve_sync(server_id: str, sync_id: str, request: Request, c: Ctx = Depends(ctx)):
    """A sync writes records on a schedule, so it runs only after an admin approves it ({"approve": false} pauses)."""
    c.require_admin()
    b = await request.json() if (await request.body()) else {}
    row = _row(c.db, c.ws_id, server_id)
    cfg = loads(row["config"], {})
    sync = next((s for s in cfg.get("syncs", []) if s["id"] == sync_id), None)
    if not sync:
        raise HTTPException(404, "sync not found")
    approve = b.get("approve", True) is not False
    actor = c.user["email"] if c.user else "api key"
    sync.update(approved=approve, approved_by=actor if approve else "", next_run=now(),
                last_status="approved" if approve else "paused")
    c.db.execute("UPDATE sources SET config = ? WHERE id = ?", [dumps(cfg), server_id])
    ops.decide(c.db, c.ws_id, "mcp_sync", f"{server_id}:{sync_id}", actor=actor,
               status="approved" if approve else "rejected", title=f"{row['name']}: {sync['tool'] or sync['resource']}",
               payload={k: sync[k] for k in ("tool", "resource", "arguments", "every_minutes", "type")})
    return {**sync, "status_info": ops.sync_status(sync)}


@router.delete("/mcp-servers/{server_id}/syncs/{sync_id}")
def delete_sync(server_id: str, sync_id: str, c: Ctx = Depends(ctx)):
    c.require_admin()
    row = _row(c.db, c.ws_id, server_id)
    cfg = loads(row["config"], {})
    cfg["syncs"] = [s for s in cfg.get("syncs", []) if s["id"] != sync_id]
    c.db.execute("UPDATE sources SET config = ? WHERE id = ?", [dumps(cfg), server_id])
    return {"deleted": sync_id}


@router.post("/mcp-servers/{server_id}/syncs/{sync_id}/run")
async def run_sync_now(server_id: str, sync_id: str, c: Ctx = Depends(ctx)):
    _row(c.db, c.ws_id, server_id)
    return await run_sync(c.db, server_id, sync_id)


# ================================================================ syncing
async def run_sync(db: Database, server_id: str, sync_id: str) -> dict:
    row = db.one("SELECT * FROM sources WHERE id = ? AND kind = 'mcp'", [server_id])
    if not row:
        return {"status": "gone"}
    cfg = loads(row["config"], {})
    sync = next((s for s in cfg.get("syncs", []) if s["id"] == sync_id), None)
    if not sync:
        return {"status": "gone"}
    if not sync.get("approved"):
        return {"status": "waiting for approval: an admin approves a sync before it writes records"}
    url, headers = _conn(row)
    label = sync["tool"] or sync["resource"]
    t0 = time.time()
    try:
        if sync["tool"]:
            result = await mcp_client.call_tool(url, headers, sync["tool"], sync.get("arguments") or {})
        else:
            result = await mcp_client.read_resource(url, headers, sync["resource"])
        if isinstance(result, dict) and result.get("isError"):
            raise mcp_client.MCPError(json.dumps(mcp_client.compact(result, 300).get("content"))[:200])
        recs = mcp_client.to_records(result, source=sync.get("source") or f"mcp/{row['name']}/{label}",
                                     type=sync.get("type") or "row", title=f"{row['name']}: {label}", now=now())
        res = store.write(db, row["workspace_id"], recs) if recs else {"created": 0, "updated": 0}
        status = f"ok: {res['created']} new, {res['updated']} updated ({time.time() - t0:.1f} s)"
    except (mcp_client.MCPError, irf.IRFError) as e:
        status = f"error: {e}"[:240]
        ops.emit(db, row["workspace_id"], "sync.failed", f"{server_id}:{sync_id}",
                 {"source": row["name"], "kind": "mcp", "sync": label, "error": str(e)[:300]})
    # re-read: the config may have changed while the call ran
    row = db.one("SELECT config FROM sources WHERE id = ?", [server_id])
    if row:
        cfg = loads(row["config"], {})
        for s in cfg.get("syncs", []):
            if s["id"] == sync_id:
                s.update(last_run=now(), last_status=status, next_run=now() + s["every_minutes"] * 60)
        db.execute("UPDATE sources SET config = ?, last_sync = ? WHERE id = ?", [dumps(cfg), now(), server_id])
    return {"status": status}


async def run_due(db: Database) -> int:
    ran = 0
    for row in db.execute("SELECT id, config FROM sources WHERE kind = 'mcp'"):
        for s in loads(row["config"], {}).get("syncs", []):
            if s.get("approved") and (s.get("next_run") or 0) <= now():
                await run_sync(db, row["id"], s["id"])
                ran += 1
    return ran


async def _loop() -> None:
    while True:
        try:
            await run_due(get_db())
        except Exception:  # noqa: BLE001 - one bad server must not stop the loop
            log.exception("MCP sync loop")
        await asyncio.sleep(60)


async def startup() -> None:
    global _task
    import os
    if os.environ.get("IRRIS_WORKER", "1") != "0":
        _task = asyncio.create_task(_loop())


async def shutdown() -> None:
    if _task:
        _task.cancel()
