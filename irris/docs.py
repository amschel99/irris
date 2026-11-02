"""Documents of any length: read page by page into the index, then read back by page, outline or search.

A 4,000-page PDF is read by a background job in batches (pypdfium2: fast, a page at a time, low memory). Each
batch is indexed as soon as it's read, so search works on the first pages while the rest are still being read,
and a restarted job resumes at the page it reached. Word, text and Markdown files are split into parts of about a
page each, so they read and cite the same way.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from irris import files, irf, jobs, llm, store
from irris.db import Database, dumps, loads, now

PDF_EXT = {".pdf"}
TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".html", ".htm", ".xml", ".yaml", ".yml"}
DOCX_EXT = {".docx"}
BATCH_PAGES = 20
PART_CHARS = 3500
PREVIEW_CHARS = 20_000
MAX_OUTLINE = 3000
INLINE_MAX_PAGES = 60
INLINE_MAX_BYTES = 3 * 1024 * 1024


def auto_study_pages() -> int:
    return int(os.environ.get("IRRIS_AUTO_STUDY_PAGES", "300") or 300)


def is_document(name: str, mime: str) -> bool:
    ext = Path(name).suffix.lower()
    return ext in PDF_EXT | TEXT_EXT | DOCX_EXT or mime == "application/pdf" or (
        mime.startswith("text/") and ext not in (".csv", ".tsv"))


# ---------------------------------------------------------------- reading files
class Source:
    """A document file opened for reading by page (PDF) or by part (everything else)."""

    def __init__(self, path: Path, name: str, mime: str):
        self.path, self.name, self.mime = path, name, mime
        ext = Path(name).suffix.lower()
        self.kind = "pdf" if ext in PDF_EXT or mime == "application/pdf" else "docx" if ext in DOCX_EXT else "text"
        self._pdf = None
        self._parts: list[str] | None = None
        self._headings: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None

    def _open_pdf(self):
        if self._pdf is None:
            import pypdfium2 as pdfium
            try:
                self._pdf = pdfium.PdfDocument(str(self.path))
            except pdfium.PdfiumError as e:
                raise irf.IRFError(f"couldn't open the PDF: {e}")
        return self._pdf

    def _load_parts(self) -> list[str]:
        if self._parts is None:
            if self.kind == "docx":
                text, self._headings = _docx_text(self.path)
            else:
                text = self.path.read_text(encoding="utf-8-sig", errors="replace")
                if Path(self.name).suffix.lower() in (".html", ".htm"):
                    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
                    text = re.sub(r"(?s)<[^>]+>", " ", text)
                    text = re.sub(r"[ \t]+", " ", text)
            self._parts = split_parts(text)
            if not self._headings:
                self._headings = _md_headings(self._parts)
        return self._parts

    @property
    def unit(self) -> str:
        return "page" if self.kind == "pdf" else "part"

    def count(self) -> int:
        return len(self._open_pdf()) if self.kind == "pdf" else len(self._load_parts())

    def pages(self, start: int, end: int) -> list[tuple[int, str]]:
        """Pages start..end (1-based, inclusive)."""
        out = []
        if self.kind == "pdf":
            pdf = self._open_pdf()
            for i in range(start - 1, min(end, len(pdf))):
                page = pdf[i]
                try:
                    tp = page.get_textpage()
                    try:
                        text = tp.get_text_range() or ""
                    finally:
                        tp.close()
                except Exception:  # noqa: BLE001 - one broken page shouldn't lose the document
                    text = ""
                finally:
                    page.close()
                out.append((i + 1, _clean(text)))
        else:
            parts = self._load_parts()
            out = [(i + 1, parts[i]) for i in range(start - 1, min(end, len(parts)))]
        return out

    def outline(self) -> list[dict]:
        if self.kind != "pdf":
            self._load_parts()
            return self._headings[:MAX_OUTLINE]
        out = []
        try:
            for item in self._open_pdf().get_toc():
                title = item.get_title() if hasattr(item, "get_title") else getattr(item, "title", "")
                dest = item.get_dest() if hasattr(item, "get_dest") else None
                index = dest.get_index() if dest is not None and hasattr(dest, "get_index") else getattr(
                    item, "page_index", None)
                if title and index is not None:
                    out.append({"title": str(title).strip()[:200], "page": int(index) + 1,
                                "level": int(getattr(item, "level", 0))})
                if len(out) >= MAX_OUTLINE:
                    break
        except Exception:  # noqa: BLE001 - a broken outline isn't worth failing the read
            pass
        return out


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_parts(text: str, size: int = PART_CHARS) -> list[str]:
    """Split text into page-sized parts at paragraph boundaries."""
    text = _clean(text)
    if not text:
        return []
    parts, cur = [], []
    length = 0
    for para in re.split(r"\n\s*\n", text):
        if length + len(para) > size and cur:
            parts.append("\n\n".join(cur))
            cur, length = [], 0
        while len(para) > size * 1.5:          # one giant paragraph: cut at a sentence
            cut = para.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            parts.append(para[:cut].strip())
            para = para[cut:].strip()
        cur.append(para)
        length += len(para) + 2
    if cur:
        parts.append("\n\n".join(cur))
    return [p for p in parts if p.strip()]


def _md_headings(parts: list[str]) -> list[dict]:
    out = []
    for n, part in enumerate(parts, 1):
        for m in re.finditer(r"(?m)^(#{1,4})\s+(.+?)\s*#*\s*$", part):
            out.append({"title": m.group(2)[:200], "page": n, "level": len(m.group(1)) - 1})
            if len(out) >= MAX_OUTLINE:
                return out
    return out


def _docx_text(path: Path) -> tuple[str, list[dict]]:
    import docx
    doc = docx.Document(str(path))
    lines, heads = [], []
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading") or style == "title":
            level = int(re.sub(r"\D", "", style) or 1) - 1
            lines.append("#" * (level + 1) + " " + t)
        else:
            lines.append(t)
    for table in doc.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n\n".join(lines), heads


_HEADING = re.compile(r"^((chapter|section|part|appendix)\s+[ivxlc\d]+[a-z]?\b.{0,80}|\d{1,2}(\.\d{1,2}){0,2}\s+[A-Z][^.]{2,80})$",
                      re.I)


def detect_headings(pages: list[tuple[int, str]]) -> list[dict]:
    """For PDFs without bookmarks: short lines at the top of a page that look like chapter or section titles."""
    out = []
    for page, text in pages:
        for line in text.splitlines()[:6]:
            line = line.strip()
            if 3 < len(line) < 90 and _HEADING.match(line):
                level = 0 if line.lower().startswith(("chapter", "part")) else line.split()[0].count(".")
                out.append({"title": line, "page": page, "level": min(level, 3)})
                break
    return out


# ---------------------------------------------------------------- the document record
def create_record(db: Database, workspace_id: str, *, file_row: dict, source: str, teach: bool) -> str:
    rec = irf.normalize({"type": "document", "source": source, "id": file_row["id"], "title": file_row["name"],
                         "data": {"file_id": file_row["id"], "file": file_row["name"], "mime": file_row["mime"],
                                  "size": file_row["size"], "status": "queued", "pages": 0, "unit": "page",
                                  "teach": teach}}, now=now())
    return store.write(db, workspace_id, [rec])["ids"][0]


def patch_record(db: Database, record_id: str, data: dict, text: str | None = None) -> None:
    row = db.one("SELECT data FROM records WHERE id = ?", [record_id])
    if not row:
        return
    merged = {**loads(row["data"], {}), **data}
    if text is None:
        db.execute("UPDATE records SET data = ?, updated = ? WHERE id = ?", [dumps(merged), now(), record_id])
    else:
        db.execute("UPDATE records SET data = ?, text = ?, updated = ? WHERE id = ?",
                   [dumps(merged), text, now(), record_id])


def _open(db: Database, workspace_id: str, file_id: str, name: str, mime: str) -> Source:
    f = files.find(db, workspace_id, file_id)
    if not f or "path" not in f:
        raise irf.IRFError("the file is gone")
    return Source(f["path"], name, mime)


def read_batch(db: Database, workspace_id: str, record_id: str, title: str, src: Source, start: int,
               total: int, outline_found: bool) -> tuple[int, list[dict], int]:
    """Read and index pages start..start+BATCH_PAGES-1. Returns (next page, detected headings, empty pages)."""
    end = min(total, start + BATCH_PAGES - 1)
    pages = src.pages(start, end)
    empty = sum(1 for _, t in pages if not t.strip())
    store.index_pages(db, workspace_id, record_id, title, [(p, t) for p, t in pages if t.strip()])
    heads = [] if outline_found or src.kind != "pdf" else detect_headings(pages)
    return end + 1, heads, empty


def _finish(db: Database, workspace_id: str, record_id: str, src: Source, total: int, empty: int) -> dict:
    preview = []
    size = 0
    for page in db.execute("SELECT page, text FROM doc_pages WHERE record_id = ? ORDER BY page LIMIT 12", [record_id]):
        preview.append(page["text"])
        size += len(page["text"])
        if size > PREVIEW_CHARS:
            break
    status = {"status": "indexed", "pages": total, "unit": src.unit, "empty_pages": empty, "read_at": now()}
    patch_record(db, record_id, status, text="\n\n".join(preview)[:PREVIEW_CHARS])
    return status


def read_now(db: Database, workspace_id: str, record_id: str, file_row: dict) -> dict:
    """Read a small document in the request (so it's searchable as soon as the upload returns)."""
    rec = db.one("SELECT title, data FROM records WHERE id = ?", [record_id])
    with Source(files.path_of(file_row), file_row["name"], file_row["mime"]) as src:
        total = src.count()
        outline = src.outline()
        page, heads, empty = 1, [], 0
        while page <= total:
            page, found, e = read_batch(db, workspace_id, record_id, rec["title"], src, page, total, bool(outline))
            heads += found
            empty += e
        patch_record(db, record_id, {"outline": (outline or heads)[:MAX_OUTLINE]})
        return _finish(db, workspace_id, record_id, src, total, empty)


def peek_pages(path: Path, name: str, mime: str) -> int:
    with Source(path, name, mime) as src:
        return src.count() if src.kind == "pdf" else max(1, path.stat().st_size // PART_CHARS)


@jobs.handler("read")
async def read_job(ctx: jobs.JobCtx) -> dict:
    db, ws = ctx.db, ctx.workspace_id
    rid = ctx.payload["record_id"]
    rec = db.one("SELECT title, data FROM records WHERE id = ? AND workspace_id = ?", [rid, ws])
    if not rec:
        return {"skipped": "the document was deleted"}
    data = loads(rec["data"], {})
    src = await asyncio.to_thread(_open, db, ws, data["file_id"], data.get("file") or rec["title"],
                                  data.get("mime") or "")
    try:
        total = ctx.state.get("total") or await asyncio.to_thread(src.count)
        if "outline" not in ctx.state:
            outline = await asyncio.to_thread(src.outline)
            ctx.state.update(total=total, outline=bool(outline), next=1, empty=0)
            await asyncio.to_thread(patch_record, db, rid, {"status": "reading", "pages": total, "unit": src.unit,
                                                             "outline": outline})
        page = ctx.state["next"]
        while page <= total:
            page, heads, empty = await asyncio.to_thread(read_batch, db, ws, rid, rec["title"], src, page, total,
                                                         ctx.state["outline"])
            ctx.state["next"], ctx.state["empty"] = page, ctx.state["empty"] + empty
            if heads:
                cur = loads(db.one("SELECT data FROM records WHERE id = ?", [rid])["data"], {}).get("outline") or []
                await asyncio.to_thread(patch_record, db, rid, {"outline": (cur + heads)[:MAX_OUTLINE]})
            done = min(page - 1, total)
            await asyncio.to_thread(patch_record, db, rid, {"read": done})
            await ctx.progress(done / max(total, 1), f"Read {done:,} of {total:,} {src.unit}s")
        status = await asyncio.to_thread(_finish, db, ws, rid, src, total, ctx.state["empty"])
    finally:
        src.close()
    queue_study(db, ws, rid, rec["title"], total, teach=bool(ctx.payload.get("teach")))
    return {"record_id": rid, **status}


def queue_study(db: Database, workspace_id: str, record_id: str, title: str, pages: int, *, teach: bool) -> dict | None:
    """Study a document after reading it: always when teaching, automatically when it isn't huge."""
    if not (teach or pages <= auto_study_pages()):
        patch_record(db, record_id, {"study": "available"})
        return None
    ws = db.one("SELECT * FROM workspaces WHERE id = ?", [workspace_id])
    if not llm.available(ws, "llm"):
        patch_record(db, record_id, {"study": "needs a reasoning model"})
        return None
    patch_record(db, record_id, {"study": "queued"})
    return jobs.enqueue(db, workspace_id, "study", {"record_id": record_id}, title=f"Studying {title}",
                        dedupe=f"study:{record_id}")


# ---------------------------------------------------------------- reading back
def document_api(row: dict) -> dict:
    data = loads(row["data"], {})
    return {"id": row["id"], "title": row["title"], "source": row["source"], "created": row["created"],
            "updated": row["updated"], "file_id": data.get("file_id"), "mime": data.get("mime"),
            "size": data.get("size"), "pages": data.get("pages", 0), "read": data.get("read", data.get("pages", 0)),
            "unit": data.get("unit", "page"), "status": data.get("status", "indexed"),
            "study": data.get("study", ""), "digest_id": data.get("digest_id"),
            "empty_pages": data.get("empty_pages", 0), "outline_size": len(data.get("outline") or [])}


def documents(db: Database, workspace_id: str, q: str = "", limit: int = 100) -> list[dict]:
    rows = db.execute("SELECT * FROM records WHERE workspace_id = ? AND type = 'document' ORDER BY updated DESC LIMIT ?",
                      [workspace_id, 1000])
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["title"].lower()]
    return [document_api(r) for r in rows[:limit]]


def doc_row(db: Database, workspace_id: str, record_id: str) -> dict:
    row = db.one("SELECT * FROM records WHERE id = ? AND workspace_id = ?", [record_id, workspace_id])
    if not row:
        raise store.NotFound("document not found")
    return row


def outline(db: Database, workspace_id: str, record_id: str) -> dict:
    row = doc_row(db, workspace_id, record_id)
    data = loads(row["data"], {})
    out = data.get("outline") or []
    if not out and row["type"] != "document":
        out = []
    return {"id": row["id"], "title": row["title"], "pages": data.get("pages") or _page_count(db, record_id),
            "unit": data.get("unit", "page"), "outline": out}


def _page_count(db: Database, record_id: str) -> int:
    r = db.one("SELECT MAX(page) AS n FROM doc_pages WHERE record_id = ?", [record_id])
    return (r or {}).get("n") or 0


def read(db: Database, workspace_id: str, record_id: str, start: int = 1, end: int | None = None,
         max_chars: int = 14_000) -> dict:
    """Pages start..end, stopping early at max_chars. `next` says where to continue, so a whole book reads in turns."""
    row = doc_row(db, workspace_id, record_id)
    total = loads(row["data"], {}).get("pages") or _page_count(db, record_id)
    if not total:     # not a paged document: read its text in slices
        text = row["text"] or ""
        a = max(0, (start - 1) * max_chars)
        return {"id": row["id"], "title": row["title"], "from": start, "to": start, "pages": 1,
                "text": text[a:a + max_chars], "next": start + 1 if a + max_chars < len(text) else None}
    start = max(1, start)
    end = min(end or start + 9, total)
    rows = db.execute("SELECT page, text FROM doc_pages WHERE record_id = ? AND page >= ? AND page <= ? ORDER BY page",
                      [record_id, start, end])
    out, size, last = [], 0, start - 1
    for r in rows:
        if out and size + len(r["text"]) > max_chars:
            break
        out.append({"page": r["page"], "text": r["text"][:max_chars]})
        size += len(r["text"])
        last = r["page"]
    if not out and start <= end:
        last = end            # blank (scanned) pages: move on
    return {"id": row["id"], "title": row["title"], "from": start, "to": last, "pages": total,
            "unit": loads(row["data"], {}).get("unit", "page"), "content": out,
            "next": last + 1 if last < total else None}
