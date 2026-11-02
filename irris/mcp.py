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


def _text(value: Any, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else dumps(value)}], "isError": error}


async def call_tool(c: auth.Ctx, name: str, args: dict) -> dict:
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


async def handle(c: auth.Ctx, msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if mid is None:                        # notifications (initialized, cancelled) get no reply
        return None
    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                   "serverInfo": {"name": "irris", "title": f"Irris · {c.workspace['name']}", "version": "1.0"},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            return ok(await call_tool(c, str(params.get("name", "")), params.get("arguments") or {}))
        except Exception as e:  # noqa: BLE001
            return ok(_text(f"{type(e).__name__}: {str(e)[:300]}", True))
    if method == "resources/list":
        return ok({"resources": [{"uri": "irris://irf", "name": "Irris Record Format", "mimeType": "text/markdown"},
                                 {"uri": "irris://workspace", "name": c.workspace["name"], "mimeType": "application/json"}]})
    if method == "resources/read":
        uri = params.get("uri")
        if uri == "irris://irf":
            return ok({"contents": [{"uri": uri, "mimeType": "text/markdown",
                                     "text": (ROOT / "docs" / "IRF.md").read_text(encoding="utf-8")}]})
        if uri == "irris://workspace":
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
    try:
        c = auth.ctx_from(request)
    except Exception:  # noqa: BLE001
        return _unauthorized(request)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
    if isinstance(payload, list):
        replies = [r for r in [await handle(c, m) for m in payload if isinstance(m, dict)] if r]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await handle(c, payload) if isinstance(payload, dict) else None
    return JSONResponse(reply) if reply else Response(status_code=202)


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})
