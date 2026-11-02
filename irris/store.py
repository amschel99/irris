"""Records: write (with upsert on source + id), read, search, and the record kinds Irris treats specially."""
from __future__ import annotations

from typing import Any

from irris import irf
from irris.db import Database, dumps, loads, new_id, now


class NotFound(LookupError):
    pass


def to_api(row: dict, full: bool = True) -> dict:
    out = {
        "id": row["id"], "type": row["type"], "source": row["source"], "external_id": row.get("ext_id"),
        "time": row["time"], "subject": row["subject"], "title": row["title"],
        "tags": loads(row.get("tags"), []), "created": row["created"], "updated": row["updated"],
    }
    if full:
        out.update(text=row["text"], data=loads(row.get("data"), {}), units=loads(row.get("units"), {}),
                   links=loads(row.get("links"), []))
    else:
        out["preview"] = (row["text"] or "")[:240] if row.get("text") else dumps(loads(row.get("data"), {}))[:240]
    return out


def write(db: Database, workspace_id: str, records: list[dict],
          pages: dict[int, list[tuple[int | None, str]]] | None = None) -> dict:
    """Store normalised records. Returns counts and the ids, in input order."""
    created = updated = 0
    ids = []
    t = now()
    with db.transaction() as tx:
        for i, rec in enumerate(records):
            existing = None
            if rec.get("id"):
                existing = tx.one("SELECT id FROM records WHERE workspace_id = ? AND source = ? AND ext_id = ?",
                                  [workspace_id, rec["source"], rec["id"]])
            values = [rec["type"], rec["source"], rec.get("id"), rec["time"], rec["subject"], rec["title"],
                      rec["text"], dumps(rec["data"]), dumps(rec["units"]), dumps(rec["tags"]), dumps(rec["links"])]
            if existing:
                rid = existing["id"]
                tx.execute("UPDATE records SET type=?, source=?, ext_id=?, time=?, subject=?, title=?, text=?, data=?, "
                           "units=?, tags=?, links=?, updated=? WHERE id = ?", [*values, t, rid])
                tx.execute("DELETE FROM passages WHERE record_id = ?", [rid])
                updated += 1
            else:
                rid = new_id()
                tx.execute("INSERT INTO records (id, workspace_id, type, source, ext_id, time, subject, title, text, "
                           "data, units, tags, links, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [rid, workspace_id, *values, t, t])
                created += 1
            ids.append(rid)
            _index(tx, workspace_id, rid, rec, (pages or {}).get(i))
    return {"created": created, "updated": updated, "ids": ids}


def _index(tx, workspace_id: str, rid: str, rec: dict, pages: list[tuple[int | None, str]] | None) -> None:
    head = "\n".join(p for p in (rec.get("title"), rec.get("subject")) if p)
    if pages:
        chunks = [(page, part) for page, text in pages for part in irf.chunk(text)]
    else:
        chunks = [(None, part) for part in irf.chunk(irf.body(rec))]
    for ordinal, (page, part) in enumerate(chunks[:20_000]):
        text = f"{head}\n{part}" if head and pages else part
        tx.execute("INSERT INTO passages (workspace_id, record_id, ord, page, text) VALUES (?,?,?,?,?)",
                   [workspace_id, rid, ordinal, page, text])


def get(db: Database, workspace_id: str, record_id: str) -> dict:
    row = db.one("SELECT * FROM records WHERE id = ? AND workspace_id = ?", [record_id, workspace_id])
    if not row:
        raise NotFound("record not found")
    return to_api(row)


def delete(db: Database, workspace_id: str, record_id: str) -> None:
    with db.transaction() as tx:
        if not tx.one("SELECT id FROM records WHERE id = ? AND workspace_id = ?", [record_id, workspace_id]):
            raise NotFound("record not found")
        tx.execute("DELETE FROM passages WHERE record_id = ?", [record_id])
        tx.execute("DELETE FROM records WHERE id = ?", [record_id])


def query(db: Database, workspace_id: str, *, type: str = "", subject: str = "", source: str = "",
          since: float | None = None, until: float | None = None, limit: int = 50, offset: int = 0,
          full: bool = False) -> dict:
    where, params = ["workspace_id = ?"], [workspace_id]
    for col, val in (("type", type), ("subject", subject)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if source:
        where.append("(source = ? OR source LIKE ?)")
        params += [source, source.rstrip("/") + "/%"]
    if since is not None:
        where.append("time >= ?")
        params.append(since)
    if until is not None:
        where.append("time <= ?")
        params.append(until)
    w = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) AS n FROM records WHERE {w}", params)["n"]
    rows = db.execute(f"SELECT * FROM records WHERE {w} ORDER BY time DESC, created DESC LIMIT ? OFFSET ?",
                      [*params, min(limit, 500), offset])
    return {"total": total, "records": [to_api(r, full) for r in rows]}


def search(db: Database, workspace_id: str, q: str, limit: int = 8, types: list[str] | None = None) -> list[dict]:
    hits, seen = [], {}
    for h in db.search_passages(workspace_id, q, limit=limit * 3, types=types):
        n = seen.get(h["record_id"], 0)
        if n >= 2:          # at most two passages from one record, so one big manual can't crowd out the rest
            continue
        seen[h["record_id"]] = n + 1
        hits.append({"record_id": h["record_id"], "type": h["type"], "source": h["source"], "title": h["title"],
                     "subject": h["subject"], "page": h["page"], "time": h["time"], "text": h["passage"],
                     "score": round(float(h["score"] or 0), 4)})
        if len(hits) >= limit:
            break
    return hits


def stats(db: Database, workspace_id: str) -> dict:
    by_type = db.execute("SELECT type, COUNT(*) AS n FROM records WHERE workspace_id = ? GROUP BY type ORDER BY n DESC",
                         [workspace_id])
    total = sum(r["n"] for r in by_type)
    day_ago = now() - 86400
    recent = db.one("SELECT COUNT(*) AS n FROM records WHERE workspace_id = ? AND created >= ?",
                    [workspace_id, day_ago])["n"]
    sources = db.execute("SELECT source, COUNT(*) AS n, MAX(time) AS last FROM records WHERE workspace_id = ? "
                         "GROUP BY source ORDER BY last DESC LIMIT 20", [workspace_id])
    return {"records": total, "last_24h": recent, "by_type": by_type, "sources": sources}


# ---------------------------------------------------------------- procedures (records of type "procedure")

def clean_steps(steps: Any) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise irf.IRFError("a procedure needs at least one step")
    out = []
    for i, s in enumerate(steps):
        if isinstance(s, str):
            s = {"instruction": s}
        if not isinstance(s, dict) or not str(s.get("instruction") or "").strip():
            raise irf.IRFError(f"step {i + 1} needs an instruction")
        step = {"instruction": str(s["instruction"]).strip()}
        for key in ("check", "caution"):
            if str(s.get(key) or "").strip():
                step[key] = str(s[key]).strip()
        r = s.get("reading")
        if isinstance(r, dict) and (r.get("min") is not None or r.get("max") is not None):
            step["reading"] = {k: r[k] for k in ("min", "max", "unit", "label") if r.get(k) not in (None, "")}
        out.append(step)
    return out


def save_procedure(db: Database, workspace_id: str, body: dict, record_id: str | None = None) -> dict:
    name = str(body.get("name") or body.get("title") or "").strip()
    if not name:
        raise irf.IRFError("a procedure needs a name")
    rec = irf.normalize({"type": "procedure", "source": "procedures", "title": name,
                         "id": body.get("external_id") or (record_id or new_id()),
                         "subject": body.get("subject") or "",
                         "text": str(body.get("description") or ""),
                         "tags": body.get("tags") or [],
                         "data": {"steps": clean_steps(body.get("steps"))}}, now=now())
    if record_id:
        row = db.one("SELECT ext_id, source FROM records WHERE id = ? AND workspace_id = ? AND type = 'procedure'",
                     [record_id, workspace_id])
        if not row:
            raise NotFound("procedure not found")
        rec["id"], rec["source"] = row["ext_id"] or record_id, row["source"]
        if not row["ext_id"]:
            db.execute("UPDATE records SET ext_id = ? WHERE id = ?", [record_id, record_id])
    rid = write(db, workspace_id, [rec])["ids"][0]
    return procedure(db, workspace_id, rid)


def procedure(db: Database, workspace_id: str, record_id: str) -> dict:
    row = db.one("SELECT * FROM records WHERE id = ? AND workspace_id = ? AND type = 'procedure'",
                 [record_id, workspace_id])
    if not row:
        raise NotFound("procedure not found")
    return procedure_api(row)


def procedure_api(row: dict) -> dict:
    data = loads(row["data"], {})
    return {"id": row["id"], "name": row["title"], "description": row["text"], "subject": row["subject"],
            "tags": loads(row["tags"], []), "steps": data.get("steps") or [], "updated": row["updated"],
            "source": row["source"]}


def procedures(db: Database, workspace_id: str) -> list[dict]:
    rows = db.execute("SELECT * FROM records WHERE workspace_id = ? AND type = 'procedure' ORDER BY title",
                      [workspace_id])
    return [procedure_api(r) for r in rows]


def lessons(db: Database, workspace_id: str, limit: int = 200) -> list[dict]:
    rows = db.execute("SELECT * FROM records WHERE workspace_id = ? AND type = 'lesson' ORDER BY created DESC LIMIT ?",
                      [workspace_id, limit])
    return [{"id": r["id"], "text": r["text"], "subject": r["subject"], "created": r["created"],
             "source": r["source"]} for r in rows]


def add_lesson(db: Database, workspace_id: str, text: str, subject: str = "", source: str = "team") -> dict:
    text = text.strip()
    if not text:
        raise irf.IRFError("a lesson needs text")
    rec = irf.normalize({"type": "lesson", "source": source, "text": text, "subject": subject}, now=now())
    rid = write(db, workspace_id, [rec])["ids"][0]
    return {"id": rid, "text": text, "subject": subject}
