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
    """Store normalised records. Returns counts and the ids, in input order.

    `pages` maps a record's position to its pages [(page number, text)]: documents keep them (doc_pages) so they
    can be read page by page and cited by page.
    """
    created = updated = 0
    ids = []
    types = set()
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
                tx.execute("DELETE FROM embeddings WHERE record_id = ?", [rid])
                tx.execute("DELETE FROM passages WHERE record_id = ?", [rid])
                updated += 1
            else:
                rid = new_id()
                tx.execute("INSERT INTO records (id, workspace_id, type, source, ext_id, time, subject, title, text, "
                           "data, units, tags, links, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [rid, workspace_id, *values, t, t])
                created += 1
            ids.append(rid)
            types.add(rec["type"])
            rec_pages = (pages or {}).get(i)
            if rec_pages:
                tx.execute("DELETE FROM doc_pages WHERE record_id = ?", [rid])
                _index_pages(tx, workspace_id, rid, rec.get("title") or "", rec_pages)
            else:
                _index(tx, workspace_id, rid, rec)
    if updated:
        _embed_changed(workspace_id)
    after_write(db, workspace_id, types)
    return {"created": created, "updated": updated, "ids": ids}


def _index(tx, workspace_id: str, rid: str, rec: dict) -> None:
    for ordinal, part in enumerate(irf.chunk(irf.body(rec))[:20_000]):
        tx.execute("INSERT INTO passages (workspace_id, record_id, ord, page, text) VALUES (?,?,?,?,?)",
                   [workspace_id, rid, ordinal, None, part])


def _index_pages(tx, workspace_id: str, rid: str, title: str, pages: list[tuple[int | None, str]]) -> None:
    """Store pages and their passages. Idempotent per page, so a resumed reader can index a page again."""
    for n, (page, text) in enumerate(pages):
        page = page if page is not None else n + 1
        tx.execute("DELETE FROM doc_pages WHERE record_id = ? AND page = ?", [rid, page])
        tx.execute("INSERT INTO doc_pages (record_id, page, workspace_id, text) VALUES (?,?,?,?)",
                   [rid, page, workspace_id, text])
        tx.execute("DELETE FROM embeddings WHERE passage_id IN (SELECT id FROM passages WHERE record_id = ? AND page = ?)",
                   [rid, page])
        tx.execute("DELETE FROM passages WHERE record_id = ? AND page = ?", [rid, page])
        for i, part in enumerate(irf.chunk(text)):
            tx.execute("INSERT INTO passages (workspace_id, record_id, ord, page, text) VALUES (?,?,?,?,?)",
                       [workspace_id, rid, page * 1000 + i, page, f"{title}\n{part}" if title else part])


def index_pages(db: Database, workspace_id: str, record_id: str, title: str, pages: list[tuple[int, str]]) -> None:
    """Add (or redo) some pages of a document that is being read."""
    with db.transaction() as tx:
        _index_pages(tx, workspace_id, record_id, title, pages)
    _embed_changed(workspace_id)
    after_write(db, workspace_id, {"document"})


def _embed_changed(workspace_id: str) -> None:
    from irris import embed
    embed.bump(workspace_id)


def after_write(db: Database, workspace_id: str, types: set[str]) -> None:
    """Queue the follow-up work new records need: embeddings, and insights when readings arrive."""
    from irris import jobs
    try:
        jobs.enqueue(db, workspace_id, "embed", {}, title="Understanding new data", dedupe=f"embed:{workspace_id}",
                     while_running=True)
        if "reading" in types:
            jobs.enqueue(db, workspace_id, "insights", {}, title="Looking for anomalies",
                         dedupe=f"insights:{workspace_id}", delay=30)
    except Exception:  # noqa: BLE001 - follow-up work must never fail a write
        pass


def get(db: Database, workspace_id: str, record_id: str) -> dict:
    row = db.one("SELECT * FROM records WHERE id = ? AND workspace_id = ?", [record_id, workspace_id])
    if not row:
        raise NotFound("record not found")
    return to_api(row)


def delete(db: Database, workspace_id: str, record_id: str) -> None:
    with db.transaction() as tx:
        if not tx.one("SELECT id FROM records WHERE id = ? AND workspace_id = ?", [record_id, workspace_id]):
            raise NotFound("record not found")
        tx.execute("DELETE FROM embeddings WHERE record_id = ?", [record_id])
        tx.execute("DELETE FROM passages WHERE record_id = ?", [record_id])
        tx.execute("DELETE FROM doc_pages WHERE record_id = ?", [record_id])
        tx.execute("DELETE FROM records WHERE id = ?", [record_id])
    _embed_changed(workspace_id)


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


def search(db: Database, workspace_id: str, q: str, limit: int = 8, types: list[str] | None = None,
           record_id: str | None = None) -> list[dict]:
    """Hybrid search: full-text and semantic (when embeddings are available), fused with reciprocal rank fusion."""
    from irris import embed
    from irris.db import PASSAGE_COLS
    fts = db.search_passages(workspace_id, q, limit=limit * 4, types=types, record_id=record_id)
    by_id = {h["passage_id"]: dict(h) for h in fts}
    ranked = [h["passage_id"] for h in fts]
    near = embed.nearest(db, workspace_id, q, k=limit * 6)
    order = ranked
    if near:
        missing = [pid for pid, _ in near if pid not in by_id]
        if missing:
            marks = ",".join("?" * len(missing))
            extra = db.execute(f"SELECT {PASSAGE_COLS}, 0 AS score FROM passages p JOIN records r ON r.id = p.record_id "
                               f"WHERE p.id IN ({marks}) AND p.workspace_id = ?", [*missing, workspace_id])
            for h in extra:
                if (types and h["type"] not in types) or (record_id and h["record_id"] != record_id):
                    continue
                by_id[h["passage_id"]] = dict(h)
        sims = dict(near)
        semantic = [pid for pid, sim in near if pid in by_id and sim > 0.3]
        order = embed.rrf(ranked, semantic)
        for pid in order:
            if pid in sims:
                by_id[pid]["similarity"] = round(sims[pid], 3)
    hits, seen = [], {}
    per_record = 8 if record_id else 2   # one big manual can't crowd out the rest
    for pid in order:
        h = by_id[pid]
        n = seen.get(h["record_id"], 0)
        if n >= per_record:
            continue
        seen[h["record_id"]] = n + 1
        hit = {"record_id": h["record_id"], "type": h["type"], "source": h["source"], "title": h["title"],
               "subject": h["subject"], "page": h["page"], "time": h["time"], "text": h["passage"],
               "score": round(float(h.get("score") or 0), 4)}
        if "similarity" in h:
            hit["similarity"] = h["similarity"]
        hits.append(hit)
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
