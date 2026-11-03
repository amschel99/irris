"""Background jobs: reading huge files, embedding passages, studying documents, distilling memories, insights.

A job is a row in `jobs`. Workers claim one atomically (Postgres: FOR UPDATE SKIP LOCKED), run its handler, and
save progress and a checkpoint (`state`) as they go. A job whose worker died is reclaimed when its lock expires and
resumes from its checkpoint, so handlers must be idempotent. Failures retry with backoff, up to MAX_ATTEMPTS.
"""
from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import Awaitable, Callable

from irris.db import Database, dumps, get_db, loads, new_id, now

log = logging.getLogger("irris.jobs")

MAX_ATTEMPTS = 5
LOCK_SECONDS = 180
WORKERS = int(os.environ.get("IRRIS_JOB_WORKERS", "3") or 3)
ACTIVE = ("queued", "running")


class Cancelled(Exception):
    pass


class Retry(Exception):
    """Raise to try again later without counting it as a crash (e.g. a model is down)."""

    def __init__(self, message: str, after: float = 60):
        super().__init__(message)
        self.after = after


def job_api(row: dict) -> dict:
    out = {"id": row["id"], "kind": row["kind"], "status": row["status"], "title": row["title"],
            "progress": round(float(row["progress"] or 0), 4), "message": row["message"], "error": row["error"],
            "result": loads(row["result"], {}), "payload": loads(row["payload"], {}), "attempts": row["attempts"],
            "created": row["created"], "updated": row["updated"], "started": row["started"], "finished": row["finished"]}
    from irris import ops
    out["status_info"] = ops.job_status(out)
    return out


def enqueue(db: Database, workspace_id: str, kind: str, payload: dict | None = None, *, title: str = "",
            dedupe: str = "", delay: float = 0, while_running: bool = False) -> dict:
    """Queue a job. With `dedupe`, an identical job that is still queued is reused instead of adding another
    (`while_running`: also one that is running, for jobs that sweep up all pending work, like embedding)."""
    if dedupe:
        states = "('queued', 'running')" if while_running else "('queued')"
        same = db.one(f"SELECT * FROM jobs WHERE dedupe = ? AND status IN {states}", [dedupe])
        if same:
            return job_api(same)
    jid = new_id()
    t = now()
    db.execute("INSERT INTO jobs (id, workspace_id, kind, status, payload, dedupe, title, run_after, created, updated) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               [jid, workspace_id, kind, "queued", dumps(payload or {}), dedupe, title[:200], t + delay, t, t])
    wake()
    return job_api(db.one("SELECT * FROM jobs WHERE id = ?", [jid]))


def get(db: Database, workspace_id: str, job_id: str) -> dict | None:
    row = db.one("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", [job_id, workspace_id])
    return job_api(row) if row else None


def listing(db: Database, workspace_id: str, *, status: str = "", kind: str = "", limit: int = 50) -> list[dict]:
    where, params = ["workspace_id = ?"], [workspace_id]
    if status == "active":
        where.append("status IN ('queued', 'running')")
    elif status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    rows = db.execute(f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY created DESC LIMIT ?",
                      [*params, min(limit, 200)])
    return [job_api(r) for r in rows]


def cancel(db: Database, workspace_id: str, job_id: str) -> dict | None:
    db.execute("UPDATE jobs SET status = 'cancelled', finished = ?, updated = ? "
               "WHERE id = ? AND workspace_id = ? AND status IN ('queued', 'running')", [now(), now(), job_id, workspace_id])
    return get(db, workspace_id, job_id)


def retry(db: Database, workspace_id: str, job_id: str) -> dict | None:
    db.execute("UPDATE jobs SET status = 'queued', attempts = 0, error = '', run_after = ?, finished = NULL, updated = ? "
               "WHERE id = ? AND workspace_id = ? AND status IN ('error', 'cancelled')",
               [now(), now(), job_id, workspace_id])
    wake()
    return get(db, workspace_id, job_id)


def claim(db: Database) -> dict | None:
    """Take the oldest runnable job: queued and due, or running with an expired lock (its worker died)."""
    t = now()
    pick = ("SELECT id FROM jobs WHERE (status = 'queued' AND run_after <= ?) OR (status = 'running' AND locked_until < ?) "
            "ORDER BY run_after, created LIMIT 1")
    if db.pg:
        pick += " FOR UPDATE SKIP LOCKED"
    rows = db.execute(f"UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_until = ?, "
                      f"started = COALESCE(started, ?), updated = ? WHERE id = ({pick}) RETURNING *",
                      [t + LOCK_SECONDS, t, t, t, t])
    return rows[0] if rows else None


class JobCtx:
    """What a handler gets: the job, its payload, its checkpoint, and a way to report progress."""

    def __init__(self, db: Database, row: dict):
        self.db, self.row = db, row
        self.id, self.workspace_id, self.kind = row["id"], row["workspace_id"], row["kind"]
        self.payload: dict = loads(row["payload"], {})
        self.state: dict = loads(row["state"], {})
        self.attempts = row["attempts"]

    def workspace(self) -> dict | None:
        return self.db.one("SELECT * FROM workspaces WHERE id = ?", [self.workspace_id])

    async def progress(self, fraction: float | None = None, message: str | None = None, *, save: bool = True) -> None:
        """Save progress and the checkpoint, extend the lock, and stop if the job was cancelled."""
        sets, params = ["locked_until = ?", "updated = ?"], [now() + LOCK_SECONDS, now()]
        if fraction is not None:
            sets.append("progress = ?")
            params.append(max(0.0, min(1.0, float(fraction))))
        if message is not None:
            sets.append("message = ?")
            params.append(message[:300])
        if save:
            sets.append("state = ?")
            params.append(dumps(self.state))
        rows = await asyncio.to_thread(
            self.db.execute, f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? AND status = 'running' RETURNING id",
            [*params, self.id])
        if not rows:
            raise Cancelled()


Handler = Callable[[JobCtx], Awaitable[dict | None]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return register


async def run_one(db: Database, row: dict) -> None:
    ctx = JobCtx(db, row)
    fn = HANDLERS.get(row["kind"])
    try:
        if fn is None:
            raise RuntimeError(f"no handler for {row['kind']}")
        result = await fn(ctx) or {}
        await asyncio.to_thread(db.execute,
                                "UPDATE jobs SET status = 'done', progress = 1, result = ?, state = ?, finished = ?, "
                                "updated = ?, locked_until = NULL WHERE id = ? AND status = 'running'",
                                [dumps(result), dumps(ctx.state), now(), now(), ctx.id])
    except Cancelled:
        pass
    except Retry as e:
        await asyncio.to_thread(db.execute,
                                "UPDATE jobs SET status = 'queued', attempts = attempts - 1, message = ?, run_after = ?, "
                                "state = ?, updated = ?, locked_until = NULL WHERE id = ? AND status = 'running'",
                                [str(e)[:300], now() + e.after, dumps(ctx.state), now(), ctx.id])
    except Exception as e:  # noqa: BLE001 - a failing job is recorded, never takes the worker down
        log.warning("job %s (%s) failed: %s", ctx.id, ctx.kind, e)
        final = ctx.attempts >= MAX_ATTEMPTS
        await asyncio.to_thread(db.execute,
                                "UPDATE jobs SET status = ?, error = ?, run_after = ?, state = ?, updated = ?, finished = ?, "
                                "locked_until = NULL WHERE id = ? AND status = 'running'",
                                ["error" if final else "queued", f"{type(e).__name__}: {e}"[:500] + (
                                    "" if final else f" (attempt {ctx.attempts} of {MAX_ATTEMPTS})"),
                                 now() + min(600, 15 * 2 ** ctx.attempts), dumps(ctx.state), now(),
                                 now() if final else None, ctx.id])
        if final:
            log.debug(traceback.format_exc())


async def run_pending(db: Database | None = None, limit: int = 100) -> int:
    """Run queued jobs until none are due (tests and scripts). Returns how many ran."""
    db = db or get_db()
    n = 0
    while n < limit:
        row = await asyncio.to_thread(claim, db)
        if not row:
            break
        await run_one(db, row)
        n += 1
    return n


# ---------------------------------------------------------------- the worker pool
_wake = asyncio.Event()
_tasks: list[asyncio.Task] = []
_loop: asyncio.AbstractEventLoop | None = None


def wake() -> None:
    """Tell idle workers there's work (safe from any thread)."""
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(_wake.set)


async def _worker(n: int) -> None:
    db = get_db()
    while True:
        try:
            row = await asyncio.to_thread(claim, db)
        except Exception as e:  # noqa: BLE001 - database hiccup: back off
            log.warning("job claim failed: %s", e)
            row = None
            await asyncio.sleep(5)
        if row:
            await run_one(db, row)
            continue
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=2 + n)
        except asyncio.TimeoutError:
            pass


def enabled() -> bool:
    return os.environ.get("IRRIS_WORKER", "1") != "0"


async def start() -> None:
    global _wake, _loop
    if _tasks or not enabled():
        return
    _wake = asyncio.Event()
    _loop = asyncio.get_running_loop()
    for i in range(max(1, WORKERS)):
        _tasks.append(asyncio.create_task(_worker(i), name=f"irris-job-worker-{i}"))


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
    global _loop
    _loop = None
