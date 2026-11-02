"""Knowledge API (/api/v1): file ingest of any size, documents, study, jobs, memories, insights.

Loaded by app.py as a feature module: `router` is mounted, `startup()` starts the background job workers.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from irris import docs, extract, files, insights, irf, jobs, llm, memory, store, study, usage  # noqa: F401 (handlers)
from irris.auth import Ctx, ctx
from irris.db import loads, now

router = APIRouter(prefix="/api/v1")

INLINE_ROWS_BYTES = 8 * 1024 * 1024


_scheduler: asyncio.Task | None = None


async def _nightly() -> None:
    """Once an hour, queue the day's memory consolidation where memory changed (deduped per workspace and day)."""
    from irris.db import get_db
    while True:
        try:
            await asyncio.to_thread(memory.schedule_consolidation, get_db())
        except Exception:  # noqa: BLE001 - try again next hour
            pass
        await asyncio.sleep(3600)


async def startup() -> None:
    global _scheduler
    await jobs.start()
    if jobs.enabled() and _scheduler is None:
        _scheduler = asyncio.create_task(_nightly(), name="irris-nightly")


async def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.cancel()
        _scheduler = None
    await jobs.stop()


def _bool(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


# ================================================================ ingest files of any size
@router.post("/ingest/file")
async def ingest_file(file: UploadFile = File(...), source: str = "upload", teach: str = "",
                      c: Ctx = Depends(ctx)):
    """Store the file, then read it. Small files are read before this returns; big ones by a background job
    (the response has `job`). Documents are studied afterwards (always with teach=true)."""
    name = file.filename or "file"
    mime = file.content_type or "application/octet-stream"
    try:
        row = await files.save_upload(c.db, c.ws_id, file, name, mime)
    except files.TooBig as e:
        raise HTTPException(413, str(e))
    teach_flag = _bool(teach)
    path = files.path_of(row)
    try:
        if docs.is_document(name, mime):
            return await _ingest_document(c, row, path, source, teach_flag)
        return await _ingest_rows(c, row, path, source)
    except irf.IRFError as e:
        files.delete(c.db, c.ws_id, row["id"])
        raise HTTPException(422, str(e))


async def _ingest_document(c: Ctx, row: dict, path: Path, source: str, teach: bool) -> dict:
    try:
        pages = await asyncio.to_thread(docs.peek_pages, path, row["name"], row["mime"])
    except irf.IRFError:
        raise
    except Exception as e:  # noqa: BLE001 - corrupt files
        raise irf.IRFError(f"couldn't read {row['name']}: {type(e).__name__}")
    rid = docs.create_record(c.db, c.ws_id, file_row=row, source=source, teach=teach)
    base = {"file_id": row["id"], "name": row["name"], "record_id": rid, "kind": "document", "ids": [rid],
            "accepted": 1, "created": 1, "updated": 0, "pages": pages}
    if pages <= docs.INLINE_MAX_PAGES and row["size"] <= docs.INLINE_MAX_BYTES:
        try:
            status = await asyncio.to_thread(docs.read_now, c.db, c.ws_id, rid, row)
        except Exception as e:  # noqa: BLE001
            store.delete(c.db, c.ws_id, rid)
            raise irf.IRFError(f"couldn't read {row['name']}: {type(e).__name__}: {str(e)[:120]}")
        job = docs.queue_study(c.db, c.ws_id, rid, row["name"], status["pages"], teach=teach)
        return {**base, "pages": status["pages"], "status": "indexed", "job": job}
    job = jobs.enqueue(c.db, c.ws_id, "read", {"record_id": rid, "teach": teach}, title=f"Reading {row['name']}",
                       dedupe=f"read:{rid}")
    docs.patch_record(c.db, rid, {"status": "queued", "pages": pages})
    return {**base, "status": "queued", "job": job}


async def _ingest_rows(c: Ctx, row: dict, path: Path, source: str) -> dict:
    if row["size"] > INLINE_ROWS_BYTES:
        job = jobs.enqueue(c.db, c.ws_id, "rows", {"file_id": row["id"], "source": source},
                           title=f"Importing {row['name']}", dedupe=f"rows:{row['id']}")
        return {"file_id": row["id"], "name": row["name"], "accepted": 0, "created": 0, "updated": 0, "ids": [],
                "kind": "row", "status": "queued", "job": job}
    content = await asyncio.to_thread(path.read_bytes)
    try:
        items = await asyncio.to_thread(extract.extract, row["name"], row["mime"], content, file_id=row["id"],
                                        now=now(), source=source)
    except irf.IRFError:
        raise
    except Exception as e:  # noqa: BLE001 - corrupt files
        raise irf.IRFError(f"couldn't read {row['name']}: {type(e).__name__}")
    res = await asyncio.to_thread(store.write, c.db, c.ws_id, [it.record for it in items])
    return {"file_id": row["id"], "name": row["name"], "accepted": len(items), **res,
            "kind": items[0].record["type"] if items else None, "status": "indexed"}


@jobs.handler("rows")
async def rows_job(ctx: jobs.JobCtx) -> dict:
    """Big CSV / Excel / JSON files: parsed off the request, written in batches."""
    db, ws = ctx.db, ctx.workspace_id
    f = db.one("SELECT * FROM stored_files WHERE id = ? AND workspace_id = ?", [ctx.payload["file_id"], ws])
    if not f:
        return {"skipped": "the file is gone"}
    content = await asyncio.to_thread(files.path_of(f).read_bytes)
    items = await asyncio.to_thread(extract.extract, f["name"], f["mime"], content, file_id=f["id"], now=now(),
                                    source=ctx.payload.get("source") or "upload", max_rows=extract.MAX_ROWS_JOB)
    start = ctx.state.get("done", 0)
    batch = 1000
    for i in range(start, len(items), batch):
        await asyncio.to_thread(store.write, db, ws, [it.record for it in items[i:i + batch]])
        ctx.state["done"] = min(len(items), i + batch)
        await ctx.progress(ctx.state["done"] / max(1, len(items)), f"Imported {ctx.state['done']:,} of {len(items):,} rows")
    return {"rows": len(items)}


@router.get("/files/{file_id}")
def get_file(file_id: str, c: Ctx = Depends(ctx)):
    f = files.find(c.db, c.ws_id, file_id)
    if not f:
        raise HTTPException(404, "file not found")
    disposition = f'inline; filename="{f["name"]}"'
    if "path" in f:
        return FileResponse(f["path"], media_type=f["mime"], headers={"Content-Disposition": disposition})
    return Response(f["content"], media_type=f["mime"], headers={"Content-Disposition": disposition})


# ================================================================ documents
@router.get("/documents")
def list_documents(q: str = "", c: Ctx = Depends(ctx)):
    return {"documents": docs.documents(c.db, c.ws_id, q=q, limit=200)}


def _doc(c: Ctx, record_id: str) -> dict:
    try:
        row = docs.doc_row(c.db, c.ws_id, record_id)
    except store.NotFound as e:
        raise HTTPException(404, str(e))
    if row["type"] != "document":
        raise HTTPException(404, "not a document")
    return row


@router.get("/documents/{record_id}")
def get_document(record_id: str, c: Ctx = Depends(ctx)):
    row = _doc(c, record_id)
    out = docs.document_api(row)
    out["outline"] = loads(row["data"], {}).get("outline") or []
    if out["digest_id"]:
        d = c.db.one("SELECT text, data FROM records WHERE id = ? AND workspace_id = ?", [out["digest_id"], c.ws_id])
        if d:
            out["digest"] = {"text": d["text"], **{k: v for k, v in loads(d["data"], {}).items()
                                                   if k in ("key_points", "chapters", "sections", "facts")}}
    out["jobs"] = [j for j in jobs.listing(c.db, c.ws_id, status="", limit=50)
                   if j["payload"].get("record_id") == record_id][:5]
    out["knowledge"] = len(study.knowledge(c.db, c.ws_id, document_id=record_id, limit=5000))
    return out


@router.get("/documents/{record_id}/pages")
def document_pages(record_id: str, start: int = 1, end: int | None = None, c: Ctx = Depends(ctx)):
    _doc(c, record_id)
    return docs.read(c.db, c.ws_id, record_id, start, end, max_chars=60_000)


@router.get("/documents/{record_id}/search")
def document_search(record_id: str, q: str, limit: int = 20, c: Ctx = Depends(ctx)):
    _doc(c, record_id)
    return {"hits": store.search(c.db, c.ws_id, q, limit=min(limit, 50), record_id=record_id)}


@router.post("/documents/{record_id}/study")
def study_document(record_id: str, c: Ctx = Depends(ctx)):
    row = _doc(c, record_id)
    data = loads(row["data"], {})
    if not llm.available(c.workspace, "llm"):
        raise HTTPException(400, "studying needs a reasoning model (Settings → Models)")
    docs.patch_record(c.db, record_id, {"teach": True, "study": "queued"})
    job = jobs.enqueue(c.db, c.ws_id, "study", {"record_id": record_id}, title=f"Studying {row['title']}",
                       dedupe=f"study:{record_id}")
    return {"job": job, "pages": data.get("pages", 0)}


@router.get("/knowledge")
def knowledge(document_id: str = "", kind: str = "", limit: int = 200, c: Ctx = Depends(ctx)):
    return {"knowledge": study.knowledge(c.db, c.ws_id, document_id=document_id, kind=kind, limit=min(limit, 1000))}


# ================================================================ jobs
@router.get("/jobs")
def list_jobs(status: str = "", kind: str = "", limit: int = 50, c: Ctx = Depends(ctx)):
    return {"jobs": jobs.listing(c.db, c.ws_id, status=status, kind=kind, limit=limit)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, c: Ctx = Depends(ctx)):
    job = jobs.get(c.db, c.ws_id, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, c: Ctx = Depends(ctx)):
    job = jobs.cancel(c.db, c.ws_id, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, c: Ctx = Depends(ctx)):
    job = jobs.retry(c.db, c.ws_id, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


# ================================================================ memories
@router.get("/memories")
def list_memories(q: str = "", history: bool = False, c: Ctx = Depends(ctx)):
    return {"memories": memory.listing(c.db, c.ws_id, c.user_id, q=q, history=history)}


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        raise HTTPException(400, "the body isn't valid JSON")
    if not isinstance(b, dict):
        raise HTTPException(400, "send a JSON object")
    return b


def _actor(c: Ctx) -> str:
    return f"team/{c.user['email']}" if c.user else "api"


@router.post("/memories")
async def add_memory(request: Request, c: Ctx = Depends(ctx)):
    b = await _body(request)
    try:
        return memory.add(c.db, c.ws_id, str(b.get("text") or ""),
                          user_id=c.user_id if b.get("scope") == "personal" else "",
                          kind=str(b.get("kind") or "semantic"), importance=float(b.get("importance", 0.7)),
                          source=_actor(c), pinned=bool(b.get("pinned")), subject=str(b.get("subject") or ""),
                          predicate=str(b.get("predicate") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.patch("/memories/{memory_id}")
async def patch_memory(memory_id: str, request: Request, c: Ctx = Depends(ctx)):
    out = memory.update(c.db, c.ws_id, memory_id, await _body(request), actor=_actor(c))
    if not out:
        raise HTTPException(404, "memory not found")
    return out


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str, c: Ctx = Depends(ctx)):
    if not memory.forget(c.db, c.ws_id, memory_id, actor=_actor(c)):
        raise HTTPException(404, "memory not found")
    return {"deleted": memory_id}


@router.get("/memories/{memory_id}/history")
def memory_history(memory_id: str, c: Ctx = Depends(ctx)):
    """Every value this fact has had (superseded ones too), and the memory's event log."""
    return {"versions": memory.chain(c.db, c.ws_id, memory_id), "events": memory.events(c.db, c.ws_id, memory_id)}


@router.get("/memory/events")
def memory_events(limit: int = 200, c: Ctx = Depends(ctx)):
    return {"events": memory.events(c.db, c.ws_id, limit=min(limit, 1000))}


@router.post("/memory/consolidate")
async def run_consolidation(c: Ctx = Depends(ctx)):
    c.require_admin()
    return jobs.enqueue(c.db, c.ws_id, "consolidate", {}, title="Consolidating memory", dedupe=f"consolidate:{c.ws_id}")


# ================================================================ usage and budgets
@router.get("/usage")
def get_usage(days: int = 7, c: Ctx = Depends(ctx)):
    return usage.summary(c.db, c.ws_id, days=min(max(days, 1), 90))


@router.patch("/usage/limits")
async def set_usage_limits(request: Request, c: Ctx = Depends(ctx)):
    c.require_admin()
    b = await _body(request)

    def num(k):
        v = b.get(k)
        return int(v) if v not in (None, "") else None
    return usage.set_limits(c.db, c.ws_id, num("daily_tokens"), num("daily_calls"))


@router.get("/usage/calls")
def usage_calls(limit: int = 100, task: str = "", c: Ctx = Depends(ctx)):
    where, params = "workspace_id = ?", [c.ws_id]
    if task:
        where += " AND task = ?"
        params.append(task)
    return {"calls": c.db.execute(f"SELECT * FROM llm_calls WHERE {where} ORDER BY id DESC LIMIT ?",
                                  [*params, min(limit, 1000)])}


@router.get("/conversations/{conv_id}/summary")
def conversation_summary(conv_id: str, c: Ctx = Depends(ctx)):
    if not c.db.one("SELECT id FROM conversations WHERE id = ? AND workspace_id = ?", [conv_id, c.ws_id]):
        raise HTTPException(404, "conversation not found")
    s = memory.summary(c.db, conv_id)
    return {"summary": s["summary"] if s else "", "upto": s["upto"] if s else 0}


# ================================================================ numbers and insights
@router.get("/insights")
def list_insights(c: Ctx = Depends(ctx)):
    return {"insights": insights.insights(c.db, c.ws_id)}


@router.post("/insights/run")
async def run_insights(c: Ctx = Depends(ctx)):
    return await asyncio.to_thread(insights.run_insights, c.db, c.ws_id)


@router.get("/stats/series")
def series_stats(type: str = "reading", field: str = "value", subject: str = "", source: str = "",
                 hours: float | None = None, group_by: str = "subject", c: Ctx = Depends(ctx)):
    return insights.aggregate(c.db, c.ws_id, type=type, field=field, subject=subject, source=source, hours=hours,
                              group_by=group_by)


@router.get("/knowledge/status")
def knowledge_status(c: Ctx = Depends(ctx)):
    """How much Irris knows: documents, pages, study notes, memories, embeddings, work in progress."""
    db, ws = c.db, c.ws_id
    pages = db.one("SELECT COUNT(*) AS n FROM doc_pages WHERE workspace_id = ?", [ws])["n"]
    passages = db.one("SELECT COUNT(*) AS n FROM passages WHERE workspace_id = ?", [ws])["n"]
    vecs = db.one("SELECT COUNT(*) AS n FROM embeddings WHERE workspace_id = ?", [ws])["n"]
    know = db.one("SELECT COUNT(*) AS n FROM records WHERE workspace_id = ? AND type = 'knowledge'", [ws])["n"]
    mems = db.one("SELECT COUNT(*) AS n FROM memory_items WHERE workspace_id = ? AND status = 'active'", [ws])["n"]
    active = jobs.listing(db, ws, status="active", limit=50)
    return {"documents": len(docs.documents(db, ws, limit=10_000)), "pages": pages, "passages": passages,
            "embedded": vecs, "semantic": vecs > 0, "knowledge": know,
            "memories": mems, "active_jobs": active}
