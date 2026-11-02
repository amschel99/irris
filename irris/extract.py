"""Files -> IRF records. Documents keep their pages so answers can cite them."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from irris import irf

MAX_ROWS = 50_000
DOC_EXT = {".pdf", ".docx", ".txt", ".md", ".markdown", ".rst", ".log", ".html", ".htm", ".xml", ".yaml", ".yml"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


class Extracted:
    def __init__(self, record: dict, pages: list[tuple[int | None, str]] | None = None):
        self.record, self.pages = record, pages


def _pdf(content: bytes) -> list[tuple[int | None, str]]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # one broken page shouldn't lose the document
            text = ""
        if text.strip():
            pages.append((i, text))
    return pages


def _docx(content: bytes) -> str:
    import docx
    doc = docx.Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _rows_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [dict(r) for _, r in zip(range(MAX_ROWS), csv.DictReader(io.StringIO(text), dialect=dialect))]


def _rows_xlsx(content: bytes) -> list[tuple[str, dict]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            continue
        names = [str(h) if h not in (None, "") else f"col{i + 1}" for i, h in enumerate(header)]
        for row in rows:
            if len(out) >= MAX_ROWS:
                break
            if any(v not in (None, "") for v in row):
                out.append((ws.title, {n: (v.isoformat() if hasattr(v, "isoformat") else v) for n, v in zip(names, row)}))
    return out


def _cast(value):
    if not isinstance(value, str):
        return value
    s = value.strip()
    try:
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        return value


def extract(name: str, mime: str, content: bytes, *, file_id: str, now: float, source: str = "upload") -> list[Extracted]:
    ext = Path(name).suffix.lower()
    stem = Path(name).stem
    base = {"source": source, "title": name, "data": {"file_id": file_id, "file": name, "mime": mime}}

    if ext in IMAGE_EXT or mime.startswith("image/"):
        return [Extracted(irf.normalize({**base, "type": "image", "id": file_id}, now=now))]

    if ext == ".csv" or mime == "text/csv":
        return [Extracted(irf.normalize({"type": "row", "source": f"{source}/{stem}", "id": f"{file_id}:{i}",
                                         "data": {k: _cast(v) for k, v in row.items() if k}}, now=now))
                for i, row in enumerate(_rows_csv(content))]

    if ext in (".xlsx", ".xlsm"):
        return [Extracted(irf.normalize({"type": "row", "source": f"{source}/{stem}/{sheet}", "id": f"{file_id}:{i}",
                                         "data": row}, now=now))
                for i, (sheet, row) in enumerate(_rows_xlsx(content))]

    if ext in (".json", ".ndjson", ".jsonl") or mime in ("application/json", "application/x-ndjson"):
        text = content.decode("utf-8-sig", errors="replace")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [Extracted(r) for r in irf.normalize_many(payload, default_source=f"{source}/{stem}", now=now)]

    if ext == ".pdf" or mime == "application/pdf":
        pages = _pdf(content)
        text = "\n\n".join(t for _, t in pages)
        rec = irf.normalize({**base, "type": "document", "id": file_id, "text": text[:2_000_000],
                             "data": {**base["data"], "pages": len(pages)}}, now=now)
        return [Extracted(rec, pages)]

    if ext == ".docx":
        text = _docx(content)
    elif ext in DOC_EXT or mime.startswith("text/"):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        raise irf.IRFError(f"Irris can't read {ext or mime} files yet. Send PDF, Word, Excel, CSV, JSON, text or images.")
    return [Extracted(irf.normalize({**base, "type": "document", "id": file_id, "text": text[:2_000_000]}, now=now))]
