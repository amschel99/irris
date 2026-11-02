"""Observation facts: everything the cameras saw, as time-annotated facts, answerable without a model call.

Every vision result is also stored as a fact (camera, t_start, t_end, subject, relation, value, confidence,
evidence). A fact identical to the camera's latest one just extends its t_end, so a steady scene is one row, not
thousands. Questions like "when did the guard open?", "what happened at 10:15?" or "last reading of the platen"
run fixed SQL templates, strict first, then relaxed (exact → wider window → partial match → drop the relation).

A rolling hierarchical summary keeps long shifts readable: summaries land at level 0; when a level holds 9 or more,
its oldest 8 are merged into one summary a level up.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from irris.db import Database, new_id, now

MERGE_GAP = 120          # a repeat within 2 minutes extends the previous fact
K = 8                    # summaries merged per level


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()[:200]


def record(db: Database, workspace_id: str, *, camera_id: str, subject: str, relation: str, value: Any = "",
           unit: str = "", conf: float = 0.5, evidence: str = "", source: str = "vision", t: float | None = None) -> str:
    """Store one fact, or extend the camera's matching latest fact. Returns the fact id."""
    t = t or now()
    subject, relation, value_s = _norm(subject), _norm(relation), _norm(value)
    if not subject or not relation:
        return ""
    last = db.one("SELECT * FROM observations WHERE workspace_id = ? AND camera_id = ? AND subject = ? AND relation = ? "
                  "ORDER BY t_end DESC LIMIT 1", [workspace_id, camera_id, subject, relation])
    if last and last["value"] == value_s and last["unit"] == unit and t - last["t_end"] <= MERGE_GAP:
        db.execute("UPDATE observations SET t_end = ?, n = n + 1, conf = ? WHERE id = ?",
                   [t, max(float(last["conf"]), float(conf)), last["id"]])
        return last["id"]
    fid = new_id()
    db.execute("INSERT INTO observations (id, workspace_id, camera_id, t_start, t_end, subject, relation, value, unit, "
               "conf, evidence, source, n) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               [fid, workspace_id, camera_id, t, t, subject, relation, value_s, unit, float(conf), evidence, source, 1])
    return fid


def _api(r: dict) -> dict:
    return {"id": r["id"], "camera_id": r["camera_id"], "t_start": r["t_start"], "t_end": r["t_end"],
            "subject": r["subject"], "relation": r["relation"], "value": r["value"], "unit": r["unit"],
            "conf": r["conf"], "evidence": r["evidence"], "source": r["source"], "n": r["n"]}


def between(db: Database, workspace_id: str, t0: float, t1: float, camera_id: str = "", limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM observations WHERE workspace_id = ? AND t_end >= ? AND t_start <= ?"
    params: list[Any] = [workspace_id, t0, t1]
    if camera_id:
        sql += " AND camera_id = ?"
        params.append(camera_id)
    return [_api(r) for r in db.execute(sql + " ORDER BY t_start LIMIT ?", [*params, limit])]


def _like(db: Database, workspace_id: str, words: list[str], relation: str = "", camera_id: str = "",
          order: str = "t_end DESC", limit: int = 20) -> list[dict]:
    if not words:
        return []
    sql = "SELECT * FROM observations WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    for w in words:
        sql += " AND (subject LIKE ? OR value LIKE ?)"
        params += [f"%{w}%", f"%{w}%"]
    if relation:
        sql += " AND relation = ?"
        params.append(relation)
    if camera_id:
        sql += " AND camera_id = ?"
        params.append(camera_id)
    return [_api(r) for r in db.execute(f"{sql} ORDER BY {order} LIMIT ?", [*params, limit])]


STOP = {"the", "a", "an", "of", "did", "when", "what", "was", "is", "last", "latest", "reading", "happen", "happened",
        "at", "on", "in", "to", "it", "its", "this", "that", "for", "show", "me", "time", "first", "value", "and"}


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", q.lower()) if w not in STOP][:4]


def _clock(q: str, tz_offset_min: int = 0) -> float | None:
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", q.lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    tz = timezone(timedelta(minutes=tz_offset_min))
    today = datetime.now(tz).replace(hour=h % 24, minute=mi, second=0, microsecond=0)
    if today.timestamp() > time.time() + 60:
        today -= timedelta(days=1)
    return today.timestamp()


def answer(db: Database, workspace_id: str, question: str, *, camera_id: str = "", tz_offset_min: int = 0) -> dict:
    """Answer a "when / what at / last reading" question from facts alone. {"kind", "facts", "relaxed"}."""
    q = question.lower()
    words = _words(q)
    at = _clock(q, tz_offset_min)
    if at is not None:                                       # what happened at HH:MM: widen the window step by step
        for i, half in enumerate((60, 300, 900)):
            facts = between(db, workspace_id, at - half, at + half, camera_id)
            if facts:
                return {"kind": "at", "facts": facts, "relaxed": i, "window_s": half * 2}
        return {"kind": "at", "facts": [], "relaxed": 3}
    if re.search(r"\b(last|latest|current)\b.*\breading\b|\breading of\b", q):
        for i, (ws, rel) in enumerate(((words, "reading"), (words[:1], "reading"), (words, ""))):
            facts = _like(db, workspace_id, ws, rel, camera_id, limit=5)
            if facts:
                return {"kind": "last_reading", "facts": facts, "relaxed": i}
        return {"kind": "last_reading", "facts": [], "relaxed": 3}
    if re.search(r"\bwhen\b|\bfirst\b|\bhow long\b", q):
        order = "t_start ASC" if "first" in q else "t_start DESC"
        for i, ws in enumerate((words, words[:2], words[:1])):
            facts = _like(db, workspace_id, ws, "", camera_id, order=order, limit=10)
            if facts:
                return {"kind": "when", "facts": facts, "relaxed": i}
        return {"kind": "when", "facts": [], "relaxed": 3}
    facts = _like(db, workspace_id, words, "", camera_id, limit=10) or _like(db, workspace_id, words[:1], "", camera_id, limit=10)
    return {"kind": "search", "facts": facts, "relaxed": 0 if facts else 3}


def timeline_text(facts: list[dict], t0: float | None = None) -> str:
    def clock(t: float) -> str:
        if t0 is None:
            return datetime.fromtimestamp(t).strftime("%H:%M:%S")
        s = max(0, t - t0)
        return f"{int(s // 60)}:{int(s % 60):02d}"
    out = []
    for f in facts:
        span = clock(f["t_start"]) + (f"–{clock(f['t_end'])}" if f["t_end"] - f["t_start"] >= 2 else "")
        val = f" = {f['value']}{' ' + f['unit'] if f['unit'] else ''}" if f["value"] else ""
        out.append(f"{span} {f['subject']} {f['relation']}{val} (conf {f['conf']:.2f})")
    return "\n".join(out)


# ================================================================ rolling hierarchical summaries
def add_summary(db: Database, workspace_id: str, camera_id: str, text: str, t_start: float, t_end: float) -> str:
    sid = new_id()
    db.execute("INSERT INTO shift_summaries (id, workspace_id, camera_id, level, t_start, t_end, text, merged, created) "
               "VALUES (?,?,?,?,?,?,?,?,?)", [sid, workspace_id, camera_id, 0, t_start, t_end, text[:4000], 0, now()])
    return sid


async def consolidate(db: Database, workspace_id: str, camera_id: str, merge) -> int:
    """While a level holds 9+ unmerged summaries, merge its oldest 8 into one a level up. `merge(texts) -> str`."""
    merges = 0
    for level in range(0, 8):
        while True:
            rows = db.execute("SELECT * FROM shift_summaries WHERE workspace_id = ? AND camera_id = ? AND level = ? "
                              "AND merged = 0 ORDER BY t_start", [workspace_id, camera_id, level])
            if len(rows) < K + 1:
                break
            batch = rows[:K]
            text = await merge([r["text"] for r in batch])
            db.execute("INSERT INTO shift_summaries (id, workspace_id, camera_id, level, t_start, t_end, text, merged, "
                       "created) VALUES (?,?,?,?,?,?,?,?,?)",
                       [new_id(), workspace_id, camera_id, level + 1, batch[0]["t_start"], batch[-1]["t_end"],
                        str(text)[:4000], 0, now()])
            for r in batch:
                db.execute("UPDATE shift_summaries SET merged = 1 WHERE id = ?", [r["id"]])
            merges += 1
    return merges


def summaries(db: Database, workspace_id: str, camera_id: str = "") -> list[dict]:
    sql = "SELECT * FROM shift_summaries WHERE workspace_id = ? AND merged = 0"
    params: list[Any] = [workspace_id]
    if camera_id:
        sql += " AND camera_id = ?"
        params.append(camera_id)
    return [{"id": r["id"], "camera_id": r["camera_id"], "level": r["level"], "t_start": r["t_start"],
             "t_end": r["t_end"], "text": r["text"]} for r in db.execute(sql + " ORDER BY t_start", params)]
