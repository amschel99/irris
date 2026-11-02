"""Stored files on disk (FILES_DIR; on Railway a volume at /data/files), streamed in and out, any size.

Files uploaded before disk storage live as blobs in the `files` table; `open_file` reads either.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from irris.config import ROOT
from irris.db import Database, new_id, now

CHUNK = 1 << 20


def files_dir() -> Path:
    path = Path(os.environ.get("FILES_DIR") or ROOT / "data" / "files")
    path.mkdir(parents=True, exist_ok=True)
    return path


def max_upload_bytes() -> int:
    return int(os.environ.get("MAX_UPLOAD_MB", "2048") or 2048) * 1024 * 1024


class TooBig(Exception):
    pass


async def save_upload(db: Database, workspace_id: str, upload, name: str, mime: str) -> dict:
    """Stream an UploadFile to disk. Returns the stored_files row."""
    fid = new_id()
    ext = Path(name).suffix.lower()[:12]
    rel = Path(workspace_id) / f"{fid}{ext}"
    dest = files_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    limit, size, digest = max_upload_bytes(), 0, hashlib.sha256()
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise TooBig(f"the file is over {limit // (1024 * 1024)} MB")
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    db.execute("INSERT INTO stored_files (id, workspace_id, name, mime, size, path, sha256, created) "
               "VALUES (?,?,?,?,?,?,?,?)", [fid, workspace_id, name, mime, size, rel.as_posix(), digest.hexdigest(), now()])
    return db.one("SELECT * FROM stored_files WHERE id = ?", [fid])


def save_bytes(db: Database, workspace_id: str, name: str, mime: str, content: bytes) -> dict:
    fid = new_id()
    rel = Path(workspace_id) / f"{fid}{Path(name).suffix.lower()[:12]}"
    dest = files_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    db.execute("INSERT INTO stored_files (id, workspace_id, name, mime, size, path, sha256, created) "
               "VALUES (?,?,?,?,?,?,?,?)", [fid, workspace_id, name, mime, len(content), rel.as_posix(),
                                            hashlib.sha256(content).hexdigest(), now()])
    return db.one("SELECT * FROM stored_files WHERE id = ?", [fid])


def path_of(row: dict) -> Path:
    base = files_dir().resolve()
    p = (base / row["path"]).resolve()
    if base not in p.parents:
        raise FileNotFoundError("bad path")
    return p


def find(db: Database, workspace_id: str, file_id: str) -> dict | None:
    """{"name", "mime", "size", "path"} for a disk file, or {"name", "mime", "size", "content"} for an old blob."""
    row = db.one("SELECT * FROM stored_files WHERE id = ? AND workspace_id = ?", [file_id, workspace_id])
    if row:
        return {"name": row["name"], "mime": row["mime"], "size": row["size"], "path": path_of(row)}
    old = db.one("SELECT name, mime, size, content FROM files WHERE id = ? AND workspace_id = ?", [file_id, workspace_id])
    if old:
        return {"name": old["name"], "mime": old["mime"], "size": old["size"], "content": bytes(old["content"])}
    return None


def read_bytes(db: Database, workspace_id: str, file_id: str) -> bytes | None:
    f = find(db, workspace_id, file_id)
    if not f:
        return None
    return f["content"] if "content" in f else f["path"].read_bytes()


def delete(db: Database, workspace_id: str, file_id: str) -> None:
    row = db.one("SELECT * FROM stored_files WHERE id = ? AND workspace_id = ?", [file_id, workspace_id])
    if row:
        try:
            path_of(row).unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        db.execute("DELETE FROM stored_files WHERE id = ?", [file_id])
    db.execute("DELETE FROM files WHERE id = ? AND workspace_id = ?", [file_id, workspace_id])
