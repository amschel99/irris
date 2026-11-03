"""MCP server at /mcp: streamable HTTP, stateless, JSON responses. Authenticated with a workspace API key
(`Authorization: Bearer irr_...`, `X-API-Key`, or `?key=` for clients that only take a URL)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from irris import agent, auth, irf, runs, store
from irris.config import ROOT
from irris.db import dumps, now

router = APIRouter()
ANON_INSTRUCTIONS = ("Without a key you can read the Irris Record Format (resource irris://irf) and validate data "
                     "against it (irf_validate). Add a workspace API key (Authorization: Bearer irr_...) for everything else.")
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Irris is the AI twin of a manufacturing or engineering team: their documents, data, databases, "
                "procedures, cameras and lessons. Use `ask` for anything open-ended: it plans and uses every tool "
                "itself and cites its sources. Use the other tools for direct reads and writes. `ingest` takes any "
                "data in the Irris Record Format (see the irris://irf resource) or any JSON.")

TOOLS: list[dict] = [
    {"name": "ask", "description": "Ask Irris anything about the team's operation, products, data and procedures. She "
     "searches, queries their databases and looks through cameras herself, and cites sources. Pass the returned "
     "conversation_id to continue.",
     "inputSchema": {"type": "object", "properties": {"message": {"type": "string"},
                                                       "conversation_id": {"type": "string"},
                                                       "camera_id": {"type": "string"}}, "required": ["message"]}},
    {"name": "ingest", "description": "Send data to Irris: one IRF record, a list of records, or any JSON (each "
     "object becomes a record of `type`). Same source + id updates instead of duplicating.",
     "inputSchema": {"type": "object", "properties": {
         "records": {"description": "IRF record(s) or any JSON"},
         "type": {"type": "string", "description": "type for non-IRF JSON (default row)"},
         "source": {"type": "string", "description": "default source (default mcp)"}}, "required": ["records"]}},
    *[{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]}
      for t in agent.TOOLS if t["name"] in ("search", "records", "databases", "sql", "look", "procedures",
                                             "start_run", "remember", "documents", "outline", "read", "search_in",
                                             "stats", "recall")],
    {"name": "run_status", "description": "The state of a guided run: every step with its result and reading.",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
    {"name": "check_step", "description": "Check the current step of a guided run through its camera, or mark it by "
     "hand with result pass, fail or skip (and an optional reading).",
     "inputSchema": {"type": "object", "properties": {
         "run_id": {"type": "string"}, "result": {"type": "string", "enum": ["pass", "fail", "skip"]},
         "note": {"type": "string"}, "reading": {"type": "object", "properties": {
             "value": {"type": "number"}, "unit": {"type": "string"}}}}, "required": ["run_id"]}},
    {"name": "cameras", "description": "The team's cameras and whether each is live.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _media_tools() -> list[dict]:
    try:
        from irris.api_media import MCP_TOOLS
        return MCP_TOOLS
    except ImportError:
        return []


TOOLS += _media_tools()

# Generic tools over the whole REST API (driven by the OpenAPI spec), and the status contract.
TOOLS += [
    {"name": "status", "description": "Why a long-running thing is in its state, what unblocks it, and what can be "
     "called now. kind: run, job, media_job, camera, source, mcp_server, mcp_sync, instrument, webhook, onboarding.",
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string"}, "id": {"type": "string"}},
                     "required": ["kind", "id"]}},
    {"name": "api_list", "description": "Every REST endpoint (method, path, summary), optionally filtered by words. Use "
     "with api_describe / api_read / api_change to reach anything the dashboard can do.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "api_describe", "description": "One endpoint's parameters and body schema.",
     "inputSchema": {"type": "object", "properties": {"method": {"type": "string"}, "path": {"type": "string"}},
                     "required": ["method", "path"]}},
    {"name": "api_read", "description": "Call a GET endpoint, e.g. path '/api/v1/runs' or '/api/v1/records' with params.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "params": {"type": "object"}},
                     "required": ["path"]}},
    {"name": "api_change", "description": "Call a POST, PUT, PATCH or DELETE endpoint with a JSON body. It changes the "
     "team's data: confirm with the person before calling it. Needs a key with admin rights.",
     "inputSchema": {"type": "object", "properties": {
         "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]}, "path": {"type": "string"},
         "body": {"type": "object"}, "idempotency_key": {"type": "string"}}, "required": ["method", "path"]}},
]

# Without a key, only these work: learn the standard and check data against it.
ANON_TOOLS = [
    {"name": "irf_validate", "description": "Check records against the Irris Record Format and show how Irris would "
     "store them (normalised), without storing anything. No key needed.",
     "inputSchema": {"type": "object", "properties": {"records": {"description": "IRF record(s) or any JSON"},
                                                       "type": {"type": "string"}}, "required": ["records"]}},
]
TOOLS += ANON_TOOLS
ANON_NAMES = {t["name"] for t in ANON_TOOLS}


def _text(value: Any, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else dumps(value)}], "isError": error}


def irf_validate(args: dict) -> dict:
    try:
        recs = irf.normalize_many(args.get("records"), default_type=args.get("type") or "row", default_source="mcp",
                                  now=now())
    except irf.IRFError as e:
        return _text({"valid": False, "errors": str(e)}, True)
    return _text({"valid": True, "count": len(recs), "normalised": recs[:20], "truncated": len(recs) > 20})


def _ops(spec: dict) -> list[dict]:
    out = []
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                desc = (op.get("description") or "").split("\n")[0][:160]
                out.append({"method": method.upper(), "path": path,
                            "summary": (op.get("summary") or "") + (" - " + desc if desc else "")})
    return out


async def api_call(request: Request, method: str, path: str, *, params: dict | None = None,
                   body: dict | None = None, idem: str = "") -> dict:
    """Call the REST API as the same caller (same key), in-process."""
    import httpx
    if not path.startswith("/api/v1/") or ".." in path:
        return _text("path must start with /api/v1/", True)
    headers = {"authorization": request.headers.get("authorization") or "",
               "x-api-key": request.headers.get("x-api-key") or ""}
    if not headers["authorization"] and request.query_params.get("key"):
        headers["authorization"] = "Bearer " + request.query_params["key"]
    if idem:
        headers["idempotency-key"] = idem
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url="http://irris.internal",
                                 timeout=120) as client:
        r = await client.request(method, path, params=params or None, json=body if method != "GET" else None,
                                 headers={k: v for k, v in headers.items() if v})
    try:
        data = r.json()
    except ValueError:
        data = r.text[:4000]
    text = dumps({"status": r.status_code, "body": data})
    if len(text) > 60_000:
        text = text[:60_000] + "... (truncated; narrow the request)"
    return _text(text, r.status_code >= 400)


async def call_tool(c: auth.Ctx, name: str, args: dict, request: Request | None = None) -> dict:
    if name == "irf_validate":
        return irf_validate(args)
    if name == "status":
        from irris.api_platform import status_of
        try:
            return _text(status_of(c, str(args.get("kind") or ""), str(args.get("id") or "")))
        except Exception as e:  # noqa: BLE001 - HTTPException for unknown kinds / ids
            return _text(str(getattr(e, "detail", e)), True)
    if name in ("api_list", "api_describe", "api_read", "api_change"):
        if request is None:
            return _text("not available here", True)
        spec = request.app.openapi()
        if name == "api_list":
            words = set(str(args.get("query") or "").lower().split())
            found = [o for o in _ops(spec)
                     if not words or words & set((o["path"] + " " + o["summary"]).lower().replace("/", " ").split())]
            return _text({"endpoints": found[:200], "openapi": "/api/v1/openapi.json"})
        if name == "api_describe":
            op = spec.get("paths", {}).get(str(args.get("path")), {}).get(str(args.get("method", "")).lower())
            if not op:
                return _text("no such endpoint; see api_list", True)
            return _text({"method": str(args["method"]).upper(), "path": args["path"], "summary": op.get("summary"),
                          "description": op.get("description"), "parameters": op.get("parameters", []),
                          "requestBody": op.get("requestBody")})
        if name == "api_read":
            return await api_call(request, "GET", str(args.get("path") or ""),
                                  params=args.get("params") if isinstance(args.get("params"), dict) else None)
        if c.role not in ("owner", "admin"):
            return _text("api_change needs an admin key", True)
        method = str(args.get("method") or "").upper()
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            return _text("method must be POST, PUT, PATCH or DELETE", True)
        return await api_call(request, method, str(args.get("path") or ""),
                              body=args.get("body") if isinstance(args.get("body"), dict) else {},
                              idem=str(args.get("idempotency_key") or ""))
    if name == "ask":
        res = await agent.answer(c.db, c.workspace, str(args.get("message", "")),
                                 conversation_id=args.get("conversation_id") or None,
                                 camera_id=str(args.get("camera_id") or ""))
        lines = [res["reply"]]
        if res["sources"]:
            lines += ["", "Sources:"] + [f"[{s['n']}] {s['title']}" + (f", p. {s['page']}" if s.get("page") else "")
                                         for s in res["sources"]]
        lines += ["", f"conversation_id: {res['conversation_id']}"]
        return _text("\n".join(lines))
    if name == "ingest":
        try:
            recs = irf.normalize_many(args.get("records"), default_type=args.get("type") or "row",
                                      default_source=args.get("source") or "mcp", now=now())
        except irf.IRFError as e:
            return _text(f"Not ingested: {e}", True)
        return _text({"accepted": len(recs), **store.write(c.db, c.ws_id, recs)})
    if name == "run_status":
        try:
            return _text(runs.state(c.db, c.ws_id, str(args.get("run_id"))))
        except store.NotFound as e:
            return _text(str(e), True)
    if name == "check_step":
        rid = str(args.get("run_id"))
        try:
            if args.get("result"):
                r = args.get("reading") if isinstance(args.get("reading"), dict) else None
                reading = {"value": float(r["value"]), "unit": str(r.get("unit") or ""), "confidence": 1.0} \
                    if r and r.get("value") is not None else None
                return _text(runs.record(c.db, c.ws_id, rid, str(args["result"]), say=str(args.get("note") or ""),
                                         reading=reading, by="mcp"))
            s = runs.state(c.db, c.ws_id, rid)
            frame = await agent.camera_frame(c.db, c.ws_id, s["camera_id"])
            if frame is None:
                return _text("No live camera frame for this run.", True)
            return _text(await runs.vision_check(c.db, c.workspace, rid, frame))
        except (store.NotFound, runs.RunError, ValueError) as e:
            return _text(str(e), True)
        except Exception as e:  # noqa: BLE001 - model errors
            return _text(f"{type(e).__name__}: {e}", True)
    if name == "cameras":
        from irris.api import cameras_list
        return _text(cameras_list(c))
    if name in {t["name"] for t in agent.TOOLS}:
        result = await agent.Turn(c.db, c.workspace).call(name, args)
        return _text(result, isinstance(result, dict) and "error" in result)
    from irris import api_media
    if name in api_media.MCP_NAMES:
        return await api_media.mcp_call(c, name, args)
    return _text(f"Unknown tool {name}", True)


async def handle(c: auth.Ctx | None, msg: dict, request: Request | None = None) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if mid is None:                        # notifications (initialized, cancelled) get no reply
        return None
    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                   "serverInfo": {"name": "irris", "title": f"Irris · {c.workspace['name']}" if c else "Irris (no key)",
                                  "version": "1.0"},
                   "instructions": INSTRUCTIONS if c else ANON_INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS if c else ANON_TOOLS})
    if method == "tools/call":
        name = str(params.get("name", ""))
        if c is None and name not in ANON_NAMES:
            return ok(_text("This tool needs a workspace API key (Authorization: Bearer irr_...).", True))
        try:
            if c is None:
                return ok(irf_validate(params.get("arguments") or {}))
            return ok(await call_tool(c, name, params.get("arguments") or {}, request))
        except Exception as e:  # noqa: BLE001
            return ok(_text(f"{type(e).__name__}: {str(e)[:300]}", True))
    if method == "resources/list":
        if c is None:
            return ok({"resources": [{"uri": "irris://irf", "name": "Irris Record Format", "mimeType": "text/markdown"}]})
        return ok({"resources": [{"uri": "irris://irf", "name": "Irris Record Format", "mimeType": "text/markdown"},
                                 {"uri": "irris://workspace", "name": c.workspace["name"], "mimeType": "application/json"}]})
    if method == "resources/read":
        uri = params.get("uri")
        if uri == "irris://irf":
            return ok({"contents": [{"uri": uri, "mimeType": "text/markdown",
                                     "text": (ROOT / "docs" / "IRF.md").read_text(encoding="utf-8")}]})
        if uri == "irris://workspace" and c is not None:
            info = {"name": c.workspace["name"], "industry": c.workspace["industry"], "about": c.workspace["about"],
                    "stats": store.stats(c.db, c.ws_id)}
            return ok({"contents": [{"uri": uri, "mimeType": "application/json", "text": dumps(info)}]})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "resource not found"}}
    if method in ("prompts/list",):
        return ok({"prompts": []})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def _unauthorized(request: Request) -> JSONResponse:
    return JSONResponse({"error": "unauthorized", "detail": "send a workspace API key: Authorization: Bearer irr_..."},
                        status_code=401, headers={"WWW-Authenticate": 'Bearer realm="irris"'})


@router.post("/mcp")
async def mcp_post(request: Request):
    has_key = bool(auth.bearer(request) or request.query_params.get("key") or request.cookies.get(auth.COOKIE))
    try:
        c = auth.ctx_from(request) if has_key else None      # no key at all: the anonymous read-only tier
    except Exception:  # noqa: BLE001 - a wrong key is an error, not the anonymous tier
        return _unauthorized(request)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
    if isinstance(payload, list):
        replies = [r for r in [await handle(c, m, request) for m in payload if isinstance(m, dict)] if r]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await handle(c, payload, request) if isinstance(payload, dict) else None
    return JSONResponse(reply) if reply else Response(status_code=202)


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})
