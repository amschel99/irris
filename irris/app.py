"""The Irris server: the website at /, the dashboard at /app, the API at /api/v1, MCP at /mcp."""
from __future__ import annotations

import importlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException, RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from irris import api, mcp
from irris.config import ROOT
from irris.db import get_db, now
from irris.security import sha256

SITE = ROOT / "site"
DASH = ROOT / "web" / "dist"


def _file(base: Path, rel: str) -> Path | None:
    path = (base / rel).resolve()
    if base.resolve() not in path.parents and path != base.resolve():
        return None
    return path if path.is_file() else None


# Feature modules: each has a `router`, and may have `async startup()` / `async shutdown()` (background workers).
FEATURES = ("api_knowledge", "api_media", "api_mcp_sources", "api_platform")


def _features() -> list:
    mods = []
    for name in FEATURES:
        try:
            mods.append(importlib.import_module(f"irris.{name}"))
        except ModuleNotFoundError as e:
            if e.name != f"irris.{name}":
                raise
    return mods


# ---------------------------------------------------------------- errors agents can act on
CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
         409: "conflict", 413: "too_large", 422: "invalid", 429: "rate_limited", 500: "internal", 502: "upstream_error",
         503: "unavailable", 504: "timeout"}
NEXT = {400: "Fix the request as the detail says and send it again.",
        401: "Send a workspace API key (Authorization: Bearer irr_...) or sign in.",
        403: "Ask a workspace admin to do it, or use an admin key.",
        404: "Check the id; list the collection to find the right one.",
        409: "Read the current state (GET it, or GET /api/v1/status/{kind}/{id}) and call an allowed action.",
        413: "Send less at once (split it into smaller requests).",
        422: "Fix the fields named in the detail and send it again.",
        429: "Wait a few minutes, then try again.",
        502: "A connected service or model failed; retry later or check it in Settings.",
        503: "Retry in a minute.", 504: "Retry; if it keeps timing out, ask for less."}


def error_response(status: int, detail, headers: dict | None = None) -> JSONResponse:
    body = {"detail": detail, "code": CODES.get(status, "error"),
            "next_action": NEXT.get(status, "Retry with backoff; if it keeps failing, POST /api/v1/feedback.")}
    return JSONResponse(body, status_code=status, headers=headers)


# ---------------------------------------------------------------- Idempotency-Key on creates
IDEMPOTENT = re.compile(r"^/api/v1/(ingest|runs|procedures|keys|sources|instruments|mcp-servers|webhooks|cameras|"
                        r"lessons|onboarding|feedback)/?$")
IDEM_TTL = 24 * 3600


async def idempotent(request: Request, call_next):
    """Same Idempotency-Key + same body within 24 h -> the first response again (header Idempotent-Replayed: true);
    same key with a different body -> 409. Keys are per caller (API key or session) and per path."""
    key = request.headers.get("idempotency-key", "").strip()
    if not key or request.method != "POST" or not IDEMPOTENT.match(request.url.path):
        return await call_next(request)
    body = await request.body()
    who = request.headers.get("authorization") or request.headers.get("x-api-key") or         request.cookies.get("irris_session") or request.query_params.get("key") or ""
    kh = sha256(f"{sha256(who)}|{request.headers.get('x-workspace', '')}|{request.url.path}|{key}")
    bh = sha256(body.decode("utf-8", "replace"))
    db = get_db()
    row = db.one("SELECT * FROM idempotency WHERE key_hash = ? AND created > ?", [kh, now() - IDEM_TTL])
    if row:
        if row["body_hash"] != bh:
            return error_response(409, "this Idempotency-Key was already used with a different body")
        return Response(row["response"], status_code=row["status_code"], media_type="application/json",
                        headers={"Idempotent-Replayed": "true"})
    resp = await call_next(request)
    data = b"".join([chunk async for chunk in resp.body_iterator])
    if resp.status_code < 500 and "json" in resp.headers.get("content-type", ""):
        db.execute("DELETE FROM idempotency WHERE created < ?", [now() - IDEM_TTL])
        db.execute("INSERT INTO idempotency (key_hash, body_hash, status_code, response, created) VALUES (?,?,?,?,?) "
                   "ON CONFLICT (key_hash) DO NOTHING", [kh, bh, resp.status_code, data.decode("utf-8", "replace"), now()])
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    return Response(data, status_code=resp.status_code, headers=headers)


def create_app() -> FastAPI:
    mods = _features()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for m in mods:
            if hasattr(m, "startup"):
                await m.startup()
        yield
        for m in mods:
            if hasattr(m, "shutdown"):
                await m.shutdown()

    app = FastAPI(title="Irris API", version="1.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan,
                  openapi_url="/api/v1/openapi.json",
                  description="The AI twin for manufacturing and engineering teams. See /developers.")
    app.include_router(api.router)
    app.include_router(mcp.router)
    for m in mods:
        app.include_router(m.router)
    get_db()   # create the schema before the first request

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        path = request.url.path
        if exc.status_code == 404 and not path.startswith(("/api/", "/mcp")) and request.method == "GET":
            page = SITE / "404.html"
            if page.exists():
                return HTMLResponse(page.read_text(encoding="utf-8"), status_code=404)
        return error_response(exc.status_code, exc.detail, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        problems = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:]) or 'body'}: {e.get('msg')}"
                             for e in exc.errors()[:5])
        return error_response(422, problems or "invalid request")

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        logging.getLogger("irris").exception("unhandled error on %s", request.url.path)
        return error_response(500, "something went wrong on our side")

    @app.middleware("http")
    async def _idempotency(request: Request, call_next):
        return await idempotent(request, call_next)

    @app.get("/llms.txt", include_in_schema=False)
    def llms():
        return PlainTextResponse((ROOT / "docs" / "llms.txt").read_text(encoding="utf-8"))

    @app.get("/developers", include_in_schema=False)
    def developers():
        return FileResponse(SITE / "developers.html")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{rest:path}", include_in_schema=False)
    def dashboard(rest: str = ""):
        found = _file(DASH, rest) if rest else None
        if found:
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if rest.startswith("assets/") else {}
            return FileResponse(found, headers=headers)
        index = DASH / "index.html"
        if not index.exists():
            return PlainTextResponse("The dashboard isn't built yet: cd web && npm install && npm run build", 503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(SITE / "index.html")

    @app.get("/{rest:path}", include_in_schema=False)
    def site(rest: str):
        found = _file(SITE, rest) or _file(SITE, rest + ".html")
        if not found:
            raise FastAPIHTTPException(404)
        return FileResponse(found)

    return app


app = create_app()
