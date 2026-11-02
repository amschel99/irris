"""IRF, the Irris Record Format (docs/IRF.md): turns any input into records, and records into searchable text."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

VERSION = "1.0"
FIELDS = {"irf", "type", "source", "id", "time", "subject", "title", "text", "data", "units", "tags", "links"}
_TYPE = re.compile(r"[^a-z0-9_]+")


class IRFError(ValueError):
    pass


def parse_time(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e12 else value)    # milliseconds too
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return parse_time(float(s), default)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise IRFError(f"time {value!r} is not ISO 8601 or Unix seconds")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise IRFError(f"time {value!r} is not ISO 8601 or Unix seconds")


def is_irf(value: Any) -> bool:
    """An object counts as IRF when it has a type and only uses (mostly) IRF field names."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return False
    return "irf" in value or set(value) <= FIELDS


def _clean_type(value: str) -> str:
    t = _TYPE.sub("_", str(value).strip().lower()).strip("_")
    if not t:
        raise IRFError("type is empty")
    return t[:48]


def _str_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")][:50]
    raise IRFError("tags must be a list of strings")


def normalize(value: Any, *, default_type: str = "row", default_source: str = "api", now: float) -> dict:
    """One input value -> one normalised record (a dict with every IRF field)."""
    if not is_irf(value):
        value = {"type": default_type, "data": value if isinstance(value, dict) else {"value": value}}
    rec = dict(value)
    extra = {k: rec.pop(k) for k in list(rec) if k not in FIELDS}
    data = rec.get("data")
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        data = {"value": data}
    if extra:
        data = {**extra, **data}
    links = rec.get("links") or []
    if not isinstance(links, list) or not all(isinstance(l, dict) and l.get("to") for l in links):
        raise IRFError('links must be a list of {"rel", "to"}')
    units = rec.get("units") or {}
    if not isinstance(units, dict):
        raise IRFError('units must be an object like {"value": "C"}')
    out = {
        "type": _clean_type(rec["type"]),
        "source": str(rec.get("source") or default_source).strip()[:200],
        "id": None if rec.get("id") in (None, "") else str(rec["id"])[:300],
        "time": parse_time(rec.get("time"), now),
        "subject": str(rec.get("subject") or "").strip()[:200],
        "title": str(rec.get("title") or "").strip()[:500],
        "text": str(rec.get("text") or ""),
        "data": data,
        "units": {str(k): str(v) for k, v in units.items()},
        "tags": _str_list(rec.get("tags")),
        "links": [{"rel": str(l.get("rel") or "related"), "to": str(l["to"])} for l in links][:50],
    }
    if not (out["text"] or out["data"] or out["title"]):
        raise IRFError("a record needs text, data or a title")
    return out


def normalize_many(payload: Any, *, default_type: str = "row", default_source: str = "api", now: float) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("records"), list) and not is_irf(payload):
        payload = payload["records"]
    items = payload if isinstance(payload, list) else [payload]
    out, errors = [], []
    for i, item in enumerate(items):
        try:
            out.append(normalize(item, default_type=default_type, default_source=default_source, now=now))
        except IRFError as e:
            errors.append(f"record {i}: {e}")
    if errors:
        raise IRFError("; ".join(errors[:10]) + (f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""))
    return out


def _flatten(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return [f"{prefix}: {str(value)[:200]}"]
    if isinstance(value, dict):
        lines = []
        for k, v in list(value.items())[:200]:
            lines += _flatten(v, f"{prefix}.{k}" if prefix else str(k), depth + 1)
        return lines
    if isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            return [f"{prefix}: {', '.join(str(v) for v in value[:50])}"]
        lines = []
        for i, v in enumerate(value[:50]):
            lines += _flatten(v, f"{prefix}[{i}]", depth + 1)
        return lines
    return [f"{prefix}: {value}"] if prefix else [str(value)]


BOOKKEEPING = ("file_id", "file", "mime", "size", "status", "pages", "read", "unit", "outline", "teach", "study",
               "digest_id", "empty_pages", "read_at")


def body(rec: dict) -> str:
    """The searchable text of a record. Documents keep their text; structured records get `key: value` lines."""
    head = [p for p in (rec.get("title"), rec.get("subject") and f"subject: {rec['subject']}",
                        rec.get("tags") and f"tags: {', '.join(rec['tags'])}") if p]
    data = dict(rec.get("data") or {})
    if rec.get("type") in ("document", "image"):      # file and reading bookkeeping isn't content
        for key in BOOKKEEPING:
            data.pop(key, None)
    if rec.get("type") == "procedure" and isinstance(data.get("steps"), list):
        steps = data.pop("steps")
        head.append("\n".join(f"Step {i + 1}: {s.get('instruction', '')}"
                              + (f" (check: {s['check']})" if s.get("check") else "")
                              for i, s in enumerate(steps) if isinstance(s, dict)))
    units = rec.get("units") or {}
    lines = []
    for line in _flatten(data):
        key = line.split(":", 1)[0]
        lines.append(f"{line} {units[key]}" if key in units else line)
    parts = head + ([rec["text"]] if rec.get("text") else []) + lines
    return "\n".join(parts)


def chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split text into passages at paragraph or sentence boundaries."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end), text.rfind(". ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in out if p]
