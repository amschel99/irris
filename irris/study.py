"""Studying a document: Irris reads EVERY page with the reasoning model and keeps what matters.

Map: the pages are cut into consecutive sections (about 14,000 characters each; coarser for huge books so the
call budget still covers every page). Each section becomes a `knowledge` record of notes, plus `knowledge`
records for its key facts, limits, procedures and warnings, each with its page.
Reduce: the section notes are summarised in rounds into one digest of the whole document.

The job checkpoints after every group of sections and writes with stable ids, so a restart resumes and never
duplicates. Without a reasoning model it's skipped (the document is still fully indexed and searchable).
"""
from __future__ import annotations

import asyncio
import os

from irris import irf, jobs, llm, store, usage
from irris.db import Database, loads, now
from irris.docs import doc_row, patch_record

SECTION_CHARS = 14_000
PARALLEL = 4
REDUCE_CHARS = 24_000
FACT_KINDS = {"fact", "limit", "procedure", "definition", "warning", "spec"}

MAP_PROMPT = """You are Irris, studying a document for {team} so you can answer their questions from it later.
Document: "{doc}". These are its {unit}s {a}-{b} of {total}.

Read them carefully and return JSON:
{{"title": "a short title for this section",
  "notes": "dense notes of everything useful here (at most 250 words), citing pages like (p. 12)",
  "facts": [{{"text": "one self-contained fact, with exact numbers and units", "page": number,
             "kind": "fact|limit|procedure|definition|warning|spec", "subject": "what it's about, or empty"}}]}}
At most 12 facts: the ones a technician or engineer would need (limits, settings, tolerances, steps, warnings).
If the pages are front matter, an index or blank, return short notes and no facts.

{pages}"""

REDUCE_PROMPT = """You are Irris, condensing your notes on "{doc}" for {team}. Below are notes on consecutive parts
of the document (with page references). Merge them into one overview that keeps every important point, limit and
page reference. Return JSON: {{"summary": "the merged notes, at most {words} words, keep (p. N) references",
"chapters": [{{"title": str, "from": number, "to": number}}]}}

{notes}"""

DIGEST_PROMPT = """You are Irris. Write the digest of "{doc}" for {team} from your notes below: what the document
is, how it's organised, and the points, limits and procedures they'll need most, with page references.
Return JSON: {{"digest": "markdown, at most 600 words", "key_points": ["at most 12 short points with (p. N)"],
"chapters": [{{"title": str, "from": number, "to": number}}]}}

{notes}"""


def max_calls() -> int:
    return int(os.environ.get("IRRIS_STUDY_MAX_CALLS", "3000") or 3000)


def plan_sections(db: Database, record_id: str) -> list[list[int]]:
    """Consecutive page ranges of about SECTION_CHARS, coarser if the call budget needs it. Every page is covered."""
    sizes = db.execute("SELECT page, LENGTH(text) AS n FROM doc_pages WHERE record_id = ? ORDER BY page", [record_id])
    total_chars = sum(r["n"] for r in sizes)
    target = max(SECTION_CHARS, total_chars // max(1, max_calls() - 50) + 1)
    out, start, acc = [], None, 0
    for r in sizes:
        if start is None:
            start = r["page"]
        acc += r["n"]
        if acc >= target:
            out.append([start, r["page"]])
            start, acc = None, 0
    if start is not None:
        out.append([start, sizes[-1]["page"]])
    return out


def _team(ws: dict) -> str:
    return f"{ws['name']} ({ws.get('industry') or 'a manufacturing and engineering team'})"


async def _map(models, db: Database, ws: dict, doc: dict, a: int, b: int, total: int, unit: str) -> dict:
    rows = await asyncio.to_thread(
        db.execute, "SELECT page, text FROM doc_pages WHERE record_id = ? AND page >= ? AND page <= ? ORDER BY page",
        [doc["id"], a, b])
    pages = "\n\n".join(f"--- {unit} {r['page']} ---\n{r['text']}" for r in rows)
    prompt = MAP_PROMPT.format(team=_team(ws), doc=doc["title"], unit=unit, a=a, b=b, total=total, pages=pages)
    msg = await usage.chat(db, ws["id"], "study", models, [{"role": "user", "content": prompt}], json_mode=True,
                           max_tokens=1800, timeout=180, ref=f"{doc['id']}:{a}-{b}")
    return llm.parse_json(msg.get("content") or "")


def _save_section(db: Database, ws_id: str, doc: dict, a: int, b: int, out: dict) -> int:
    src = f"study/{doc['id']}"
    link = [{"rel": "summarizes", "to": f"record:{doc['id']}"}]
    title = str(out.get("title") or f"{doc['title']}, pp. {a}-{b}")[:160]
    recs = [irf.normalize({"type": "knowledge", "source": src, "id": f"notes:{a}-{b}",
                           "title": f"{doc['title']}: {title} (pp. {a}-{b})",
                           "text": str(out.get("notes") or "").strip() or f"(pages {a}-{b})",
                           "data": {"kind": "notes", "document_id": doc["id"], "from": a, "to": b},
                           "links": link}, now=now())]
    facts = out.get("facts") if isinstance(out.get("facts"), list) else []
    for i, f in enumerate(facts[:16]):
        if not isinstance(f, dict) or not str(f.get("text") or "").strip():
            continue
        try:
            page = int(f.get("page") or a)
        except (TypeError, ValueError):
            page = a
        page = page if a <= page <= b else a
        kind = str(f.get("kind") or "fact").lower()
        recs.append(irf.normalize({"type": "knowledge", "source": src, "id": f"fact:{a}-{b}:{i}",
                                   "title": f"{doc['title']}, p. {page}", "text": str(f["text"]).strip()[:1000],
                                   "subject": str(f.get("subject") or "")[:200],
                                   "data": {"kind": kind if kind in FACT_KINDS else "fact", "document_id": doc["id"],
                                            "page": page},
                                   "links": link}, now=now()))
    store.write(db, ws_id, recs)
    return len(recs) - 1


async def _reduce(models, db: Database, ws: dict, doc: dict, notes: list[str]) -> dict:
    """Summarise notes in rounds until they fit one call, then write the digest."""
    team = _team(ws)
    rounds = 0
    while sum(len(n) for n in notes) > REDUCE_CHARS and rounds < 6:
        groups, cur, size = [], [], 0
        for n in notes:
            if cur and size + len(n) > REDUCE_CHARS:
                groups.append(cur)
                cur, size = [], 0
            cur.append(n)
            size += len(n)
        if cur:
            groups.append(cur)
        words = max(250, 900 // max(1, len(groups) // 8 + 1))

        async def one(g: list[str]) -> str:
            msg = await usage.chat(db, ws["id"], "study", models, [{"role": "user", "content": REDUCE_PROMPT.format(
                doc=doc["title"], team=team, words=words, notes="\n\n".join(g))}], json_mode=True, max_tokens=1800,
                timeout=180)
            out = llm.parse_json(msg.get("content") or "")
            return str(out.get("summary") or msg.get("content") or "")[:8000]

        merged: list[str] = []
        for i in range(0, len(groups), PARALLEL):
            merged += await asyncio.gather(*(one(g) for g in groups[i:i + PARALLEL]))
        notes = merged
        rounds += 1
    msg = await usage.chat(db, ws["id"], "study", models, [{"role": "user", "content": DIGEST_PROMPT.format(
        doc=doc["title"], team=team, notes="\n\n".join(notes)[:REDUCE_CHARS * 2])}], json_mode=True, max_tokens=2500,
        timeout=240)
    return llm.parse_json(msg.get("content") or "")


@jobs.handler("study")
async def study_job(ctx: jobs.JobCtx) -> dict:
    db, ws_id = ctx.db, ctx.workspace_id
    rid = ctx.payload["record_id"]
    try:
        row = doc_row(db, ws_id, rid)
    except store.NotFound:
        return {"skipped": "the document was deleted"}
    ws = ctx.workspace()
    models = llm.chain(ws, "llm")
    if not models:
        await asyncio.to_thread(patch_record, db, rid, {"study": "needs a reasoning model"})
        return {"skipped": "no reasoning model is set up"}
    data = loads(row["data"], {})
    if data.get("status") in ("queued", "reading"):
        raise jobs.Retry("waiting for the document to finish reading", after=30)
    doc = {"id": rid, "title": row["title"]}
    unit = data.get("unit", "page")
    if "sections" not in ctx.state:
        ctx.state.update(sections=await asyncio.to_thread(plan_sections, db, rid), done=0, facts=0)
        await asyncio.to_thread(patch_record, db, rid, {"study": "studying"})
    sections = ctx.state["sections"]
    total = data.get("pages") or (sections[-1][1] if sections else 0)
    n = len(sections)
    while ctx.state["done"] < n:
        group = sections[ctx.state["done"]:ctx.state["done"] + PARALLEL]
        try:
            outs = await asyncio.gather(*(_map(models, db, ws, doc, a, b, total, unit) for a, b in group))
        except usage.BudgetExhausted:
            raise jobs.Retry("the daily model budget is used up; studying continues tomorrow", after=usage.seconds_to_reset())
        except llm.ModelError as e:
            raise jobs.Retry(f"the reasoning model is unavailable ({str(e)[:120]}); retrying", after=120)
        for (a, b), out in zip(group, outs):
            ctx.state["facts"] += await asyncio.to_thread(_save_section, db, ws_id, doc, a, b, out)
        ctx.state["done"] += len(group)
        last = group[-1][1]
        await asyncio.to_thread(patch_record, db, rid, {"studied": last})
        await ctx.progress(0.92 * ctx.state["done"] / max(n, 1), f"Studied {last:,} of {total:,} {unit}s")
    await ctx.progress(0.94, "Writing the digest")
    notes_rows = await asyncio.to_thread(
        db.execute, "SELECT text, data FROM records WHERE workspace_id = ? AND source = ? AND type = 'knowledge'",
        [ws_id, f"study/{rid}"])
    notes = sorted(((loads(r["data"], {}), r["text"]) for r in notes_rows if loads(r["data"], {}).get("kind") == "notes"),
                   key=lambda x: x[0].get("from", 0))
    try:
        out = await _reduce(models, db, ws, doc, [f"(pp. {d.get('from')}-{d.get('to')}) {t}" for d, t in notes])
    except usage.BudgetExhausted:
        raise jobs.Retry("the daily model budget is used up; studying continues tomorrow", after=usage.seconds_to_reset())
    except llm.ModelError as e:
        raise jobs.Retry(f"the reasoning model is unavailable ({str(e)[:120]}); retrying", after=120)
    digest = str(out.get("digest") or "").strip() or "\n\n".join(t for _, t in notes)[:4000]
    points = [str(p) for p in (out.get("key_points") or []) if p][:12]
    chapters = [c for c in (out.get("chapters") or []) if isinstance(c, dict) and c.get("title")][:200]
    rec = irf.normalize({"type": "knowledge", "source": f"study/{rid}", "id": "digest",
                         "title": f"Digest: {row['title']}",
                         "text": digest + ("\n\nKey points:\n" + "\n".join(f"- {p}" for p in points) if points else ""),
                         "data": {"kind": "digest", "document_id": rid, "key_points": points, "chapters": chapters,
                                  "sections": n, "facts": ctx.state["facts"]},
                         "links": [{"rel": "summarizes", "to": f"record:{rid}"}]}, now=now())
    digest_id = (await asyncio.to_thread(store.write, db, ws_id, [rec]))["ids"][0]
    patch = {"study": "done", "digest_id": digest_id, "studied": total}
    if chapters and not data.get("outline"):
        patch["outline"] = [{"title": str(c["title"])[:200], "page": int(c.get("from") or 1), "level": 0}
                            for c in chapters if str(c.get("from") or "1").isdigit()]
    await asyncio.to_thread(patch_record, db, rid, patch)
    return {"record_id": rid, "sections": n, "facts": ctx.state["facts"], "digest_id": digest_id}


def knowledge(db: Database, workspace_id: str, document_id: str = "", kind: str = "", limit: int = 200) -> list[dict]:
    where, params = ["workspace_id = ?", "type = 'knowledge'"], [workspace_id]
    if document_id:
        where.append("source = ?")
        params.append(f"study/{document_id}")
    rows = db.execute(f"SELECT * FROM records WHERE {' AND '.join(where)} ORDER BY updated DESC LIMIT ?",
                      [*params, 5000 if kind else limit])
    out = []
    for r in rows:
        d = loads(r["data"], {})
        if kind and d.get("kind") != kind:
            continue
        out.append({"id": r["id"], "title": r["title"], "text": r["text"], "subject": r["subject"],
                    "kind": d.get("kind", "fact"), "document_id": d.get("document_id"), "page": d.get("page"),
                    "from": d.get("from"), "to": d.get("to"), "key_points": d.get("key_points"),
                    "chapters": d.get("chapters"), "updated": r["updated"]})
        if len(out) >= limit:
            break
    return out
