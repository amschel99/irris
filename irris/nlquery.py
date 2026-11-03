"""Plain words -> a visible, structured records query.

"batches last month where temperature > 185" becomes
    {"type": "batch_record", "since": ..., "conditions": [{"field": "temperature", "op": ">", "value": 185}], ...}
which the person sees (as chips) before it runs. Parsing is deterministic and free: no model call, no execution.
Types, subjects and field names are matched against what this workspace actually has, so the query only uses
things that exist; anything not understood is reported in `unparsed` instead of being guessed.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from irris import store
from irris.db import Database, loads

UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400,
         "year": 365 * 86400}
OPS = [(r">=|at least|no less than", ">="), (r"<=|at most|no more than", "<="), (r"!=|not equal to|is not", "!="),
       (r">|above|over|more than|greater than|higher than|exceeds?", ">"),
       (r"<|below|under|less than|lower than", "<"), (r"==|=|equals?|is", "=")]
TYPE_WORDS = {"batch": "batch_record", "batches": "batch_record", "batch record": "batch_record",
              "batch records": "batch_record", "reading": "reading", "readings": "reading", "measurements": "reading",
              "event": "event", "events": "event", "alarm": "event", "alarms": "event", "document": "document",
              "documents": "document", "manuals": "document", "procedure": "procedure", "procedures": "procedure",
              "sops": "procedure", "lessons": "lesson", "insights": "insight", "rows": "row"}
FIELD_SYNONYMS = {"temp": ["temperature", "temp", "value"], "temperature": ["temperature", "temp", "value"],
                  "pressure": ["pressure", "value"], "reading": ["value"], "value": ["value"], "qty": ["qty", "quantity"],
                  "quantity": ["quantity", "qty"], "deviations": ["deviations"], "weight": ["weight", "kg", "value"]}
STOP = {"show", "me", "all", "the", "a", "an", "of", "in", "on", "for", "with", "where", "that", "which", "and",
        "list", "find", "get", "give", "what", "were", "was", "are", "from", "any", "had", "have", "please", "my", "our"}


def _day_start(t: float) -> float:
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def _num(s: str) -> float | int:
    v = float(s)
    return int(v) if v.is_integer() else v


def _field_names(db: Database, ws_id: str, type_: str) -> list[str]:
    where, params = "workspace_id = ?", [ws_id]
    if type_:
        where += " AND type = ?"
        params.append(type_)
    keys: dict[str, int] = {}
    for r in db.execute(f"SELECT data FROM records WHERE {where} ORDER BY time DESC LIMIT 50", params):
        for k, v in (loads(r["data"], {}) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                keys[k] = keys.get(k, 0) + 1
            elif isinstance(v, dict):                                  # one level of nesting: metrics.temp
                for k2, v2 in v.items():
                    if isinstance(v2, (int, float)) and not isinstance(v2, bool):
                        keys[f"{k}.{k2}"] = keys.get(f"{k}.{k2}", 0) + 1
    return sorted(keys, key=lambda k: -keys[k])


def _match_field(word: str, fields: list[str]) -> str | None:
    w = word.lower().strip().replace(" ", "_")
    cands = [w] + FIELD_SYNONYMS.get(w, [])
    for c in cands:
        for f in fields:
            if f.lower() == c or f.lower().split(".")[-1] == c:
                return f
    for f in fields:
        if w and (w in f.lower() or f.lower().split(".")[-1] in w):
            return f
    return None


def parse(db: Database, ws_id: str, text: str, now: float | None = None) -> dict:
    now = now or time.time()
    original = text
    s = " " + text.lower().strip() + " "
    q: dict[str, Any] = {"text": original, "type": "", "subject": "", "source": "", "since": None, "until": None,
                         "conditions": [], "terms": [], "unparsed": []}

    # ---- time window
    def cut(m: re.Match) -> None:
        nonlocal s
        s = s.replace(m.group(0), " ", 1)
    if (m := re.search(r"\b(?:in the |over the )?(?:last|past)\s+(\d+)\s+(minute|min|hour|hr|day|week|month|year)s?\b", s)):
        q["since"] = now - int(m.group(1)) * UNITS[m.group(2)]; cut(m)
    elif (m := re.search(r"\b(?:in the |over the )?(?:last|past)\s+(hour|day|week|month|year)\b", s)):
        q["since"] = now - UNITS[m.group(1)]; cut(m)
    elif (m := re.search(r"\btoday\b", s)):
        q["since"] = _day_start(now); cut(m)
    elif (m := re.search(r"\byesterday\b", s)):
        q["since"], q["until"] = _day_start(now) - 86400, _day_start(now); cut(m)
    elif (m := re.search(r"\bthis (week|month|year)\b", s)):
        d = datetime.fromtimestamp(now, tz=timezone.utc)
        start = {"week": _day_start(now) - d.weekday() * 86400,
                 "month": datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp(),
                 "year": datetime(d.year, 1, 1, tzinfo=timezone.utc).timestamp()}[m.group(1)]
        q["since"] = start; cut(m)
    for word, key in (("since|after|from", "since"), ("before|until", "until")):
        if (m := re.search(rf"\b(?:{word})\s+(\d{{4}}-\d{{2}}-\d{{2}})\b", s)):
            q[key] = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc).timestamp(); cut(m)

    # ---- type (from this workspace's types, plus common words)
    have = {r["type"] for r in db.execute("SELECT DISTINCT type FROM records WHERE workspace_id = ?", [ws_id])}
    for word, t in sorted(TYPE_WORDS.items(), key=lambda kv: -len(kv[0])):
        if (m := re.search(rf"\b{re.escape(word)}\b", s)) and (t in have or not have):
            q["type"] = t; cut(m); break
    if not q["type"]:
        for t in sorted(have, key=len, reverse=True):
            for form in (t.replace("_", " ") + "s", t.replace("_", " "), t + "s", t):
                if (m := re.search(rf"\b{re.escape(form)}\b", s)):
                    q["type"] = t; cut(m); break
            if q["type"]:
                break

    # ---- conditions: "<field> <op> <number>"
    fields = _field_names(db, ws_id, q["type"])
    op_alt = "|".join(p for p, _ in OPS)
    for m in list(re.finditer(rf"([a-z][a-z0-9_ .]*?)\s*({op_alt})\s*(-?\d+(?:\.\d+)?)\s*(?:°?\s*[a-z%°]{{0,3}})?\b", s)):
        lead = m.group(1)
        words = [w for w in re.findall(r"[a-z0-9_.]+", lead) if w not in STOP]
        op = next(o for p, o in OPS if re.fullmatch(p, m.group(2)))
        field, used = None, ""
        if words:
            field = _match_field(words[-1], fields)
            used = words[-1] if field else ""
            if not field and len(words) > 1:
                field = _match_field(" ".join(words[-2:]), fields)
                used = " ".join(words[-2:]) if field else ""
        tail = m.group(0)[len(lead):]                                     # "> 185 C"
        if not field and "value" in fields:
            field = "value"                                              # "press 7 above 180": the reading's value
        if not field:
            q["unparsed"].append(m.group(0).strip())
            s = s.replace(m.group(0), " ", 1)
            continue
        at = lead.rfind(used) if used else len(lead)                     # cut only the field word and the comparison,
        cut_from = m.group(0)[at:]                                       # so "press 7" stays for the subject
        q["conditions"].append({"field": field, "op": op, "value": _num(m.group(3)),
                                "said": (used + " " if used else "") + tail.strip()})
        s = s.replace(cut_from, " ", 1)

    # ---- subject (one that exists here)
    subjects = [r["subject"] for r in db.execute(
        "SELECT DISTINCT subject FROM records WHERE workspace_id = ? AND subject != '' LIMIT 500", [ws_id])]
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
    words_left = norm(s)
    best = ""
    for sub in subjects:
        name = norm(sub.split(":", 1)[-1])
        if name and re.search(rf"\b{re.escape(name)}\b", words_left) and len(name) > len(norm(best.split(':', 1)[-1])):
            best = sub
    if best:
        q["subject"] = best
        words_left = re.sub(rf"\b{re.escape(norm(best.split(':', 1)[-1]))}\b", " ", words_left)

    q["terms"] = [w for w in words_left.split() if w not in STOP and len(w) > 2 and not w.isdigit()][:8]
    q["explain"] = explain(q)
    return q


def explain(q: dict) -> str:
    parts = [f"{q['type'].replace('_', ' ')}s" if q.get("type") else "records"]
    if q.get("subject"):
        parts.append(f"for {q['subject']}")
    for c in q.get("conditions") or []:
        parts.append(f"where {c['field']} {c['op']} {c['value']}")
    if q.get("since"):
        parts.append("since " + datetime.fromtimestamp(q["since"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))
    if q.get("until"):
        parts.append("until " + datetime.fromtimestamp(q["until"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))
    if q.get("terms"):
        parts.append("mentioning " + ", ".join(q["terms"]))
    return " ".join(parts)


def _get(data: dict, field: str):
    cur: Any = data
    for part in field.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _ok(v, op: str, target) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    return {">": v > target, "<": v < target, ">=": v >= target, "<=": v <= target, "=": v == target,
            "!=": v != target}[op]


def run(db: Database, ws_id: str, q: dict, limit: int = 50) -> dict:
    """Run a parsed (or hand-edited) query. Conditions and terms filter in code over the matching records."""
    pool = store.query(db, ws_id, type=q.get("type") or "", subject=q.get("subject") or "", source=q.get("source") or "",
                       since=q.get("since"), until=q.get("until"), limit=500 if (q.get("conditions") or q.get("terms")) else limit,
                       full=True)
    out = []
    for r in pool["records"]:
        if not all(_ok(_get(r["data"], c["field"]), c["op"], c["value"]) for c in q.get("conditions") or []):
            continue
        hay = (r["title"] + " " + r["text"] + " " + r["subject"] + " " + str(r["data"])).lower()
        if q.get("terms") and not any(t in hay for t in q["terms"]):
            continue
        out.append(r)
    exact = not (q.get("conditions") or q.get("terms")) or pool["total"] <= 500
    return {"query": q, "explain": explain(q), "total": len(out) if exact else None,
            "scanned": len(pool["records"]), "records": out[:limit], "complete": exact}
