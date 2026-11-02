"""Every model and tool call, logged (`llm_calls`), and hard daily budgets per workspace.

The log feeds evals, budgets and future fine-tuning: task label, tokens in/out, latency, success. Budgets cap model
tokens and calls per UTC day (IRRIS_DAILY_TOKENS / IRRIS_DAILY_CALLS, overridable per workspace in `ws_limits`).
When a budget is spent, model calls raise BudgetExhausted: the assistant stops with a clear message and background
jobs wait for the next day. Nothing overspends silently.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from irris import llm
from irris.db import Database, dumps, now

CHARS_PER_TOKEN = 4


class BudgetExhausted(Exception):
    def __init__(self, info: dict):
        super().__init__(f"daily model budget used: {info['tokens']:,} of {info['daily_tokens']:,} tokens, "
                         f"{info['calls']:,} of {info['daily_calls']:,} calls")
        self.info = info


def day_start(t: float | None = None) -> float:
    d = datetime.fromtimestamp(t or time.time(), timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def seconds_to_reset() -> float:
    return day_start() + 86400 - time.time() + 5


def limits(db: Database, workspace_id: str) -> dict:
    row = db.one("SELECT daily_tokens, daily_calls FROM ws_limits WHERE workspace_id = ?", [workspace_id]) or {}
    return {"daily_tokens": int(row.get("daily_tokens") or os.environ.get("IRRIS_DAILY_TOKENS", "3000000")),
            "daily_calls": int(row.get("daily_calls") or os.environ.get("IRRIS_DAILY_CALLS", "3000"))}


def set_limits(db: Database, workspace_id: str, daily_tokens: int | None, daily_calls: int | None) -> dict:
    if db.one("SELECT workspace_id FROM ws_limits WHERE workspace_id = ?", [workspace_id]):
        db.execute("UPDATE ws_limits SET daily_tokens = ?, daily_calls = ?, updated = ? WHERE workspace_id = ?",
                   [daily_tokens, daily_calls, now(), workspace_id])
    else:
        db.execute("INSERT INTO ws_limits (workspace_id, daily_tokens, daily_calls, updated) VALUES (?,?,?,?)",
                   [workspace_id, daily_tokens, daily_calls, now()])
    return limits(db, workspace_id)


def used_today(db: Database, workspace_id: str) -> dict:
    row = db.one("SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens FROM llm_calls "
                 "WHERE workspace_id = ? AND kind = 'llm' AND created >= ?", [workspace_id, day_start()])
    return {"calls": int(row["calls"] or 0), "tokens": int(row["tokens"] or 0)}


def status(db: Database, workspace_id: str) -> dict:
    lim, used = limits(db, workspace_id), used_today(db, workspace_id)
    return {**lim, **used, "exhausted": used["tokens"] >= lim["daily_tokens"] or used["calls"] >= lim["daily_calls"],
            "resets_in": int(seconds_to_reset())}


def check(db: Database, workspace_id: str) -> None:
    s = status(db, workspace_id)
    if s["exhausted"]:
        raise BudgetExhausted(s)


def log(db: Database, workspace_id: str, *, task: str, kind: str, name: str, tokens_in: int = 0, tokens_out: int = 0,
        ms: int = 0, ok: bool = True, error: str = "", ref: str = "") -> None:
    try:
        db.execute("INSERT INTO llm_calls (workspace_id, task, kind, name, tokens_in, tokens_out, ms, ok, error, ref, "
                   "created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   [workspace_id, task[:40], kind, name[:80], int(tokens_in), int(tokens_out), int(ms), 1 if ok else 0,
                    error[:300], ref[:120], now()])
    except Exception:  # noqa: BLE001 - logging must never break the work
        pass


def estimate(value) -> int:
    return max(1, len(value if isinstance(value, str) else dumps(value)) // CHARS_PER_TOKEN)


async def chat(db: Database, workspace_id: str, task: str, models: list, messages: list[dict],
               tools: list[dict] | None = None, ref: str = "", **kw) -> dict:
    """llm.chat_chain with a budget check before and a log line after."""
    check(db, workspace_id)
    t0 = time.time()
    tin = estimate(messages) + (estimate(tools) if tools else 0)
    try:
        msg = await llm.chat_chain(models, messages, tools, **kw)
    except llm.ModelError as e:
        log(db, workspace_id, task=task, kind="llm", name=models[0].model if models else "", tokens_in=tin,
            ms=int((time.time() - t0) * 1000), ok=False, error=str(e), ref=ref)
        raise
    tout = estimate((msg.get("content") or "") + dumps(msg.get("tool_calls") or ""))
    log(db, workspace_id, task=task, kind="llm", name=str(msg.get("_model") or (models[0].model if models else "")),
        tokens_in=tin, tokens_out=tout, ms=int((time.time() - t0) * 1000), ref=ref)
    return msg


def summary(db: Database, workspace_id: str, days: int = 7) -> dict:
    since = now() - days * 86400
    rows = db.execute("SELECT task, kind, COUNT(*) AS n, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens, "
                      "COALESCE(AVG(ms), 0) AS avg_ms, SUM(CASE WHEN ok = 1 THEN 0 ELSE 1 END) AS errors "
                      "FROM llm_calls WHERE workspace_id = ? AND created >= ? GROUP BY task, kind ORDER BY tokens DESC",
                      [workspace_id, since])
    return {"days": days, "by_task": [{**r, "avg_ms": int(r["avg_ms"] or 0)} for r in rows],
            "today": status(db, workspace_id)}
