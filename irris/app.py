"""The Irris server: the website at /, the dashboard at /app, the API at /api/v1, MCP at /mcp."""
from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from irris import api, mcp
from irris.config import ROOT
from irris.db import get_db

SITE = ROOT / "site"
DASH = ROOT / "web" / "dist"


def _file(base: Path, rel: str) -> Path | None:
    path = (base / rel).resolve()
    if base.resolve() not in path.parents and path != base.resolve():
        return None
    return path if path.is_file() else None


# Feature modules: each has a `router`, and may have `async startup()` / `async shutdown()` (background workers).
FEATURES = ("api_knowledge", "api_media")


def _features() -> list:
    mods = []
    for name in FEATURES:
        try:
            mods.append(importlib.import_module(f"irris.{name}"))
        except ModuleNotFoundError as e:
            if e.name != f"irris.{name}":
                raise
    return mods


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
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

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
