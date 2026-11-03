"""Semantic search: passage embeddings (fastembed, ONNX on CPU) fused with full-text search.

Vectors are stored in `embeddings` as float32 bytes, normalised, so a dot product is the cosine. Each workspace's
matrix is cached in memory and rebuilt when its version changes (writes bump it). If fastembed or its model isn't
available, everything still works on full-text search alone.

Env: IRRIS_EMBEDDINGS=off disables it; EMBED_MODEL (default BAAI/bge-small-en-v1.5); FASTEMBED_CACHE (model dir).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Protocol

import numpy as np

from irris.db import Database

log = logging.getLogger("irris.embed")

MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
BATCH = 64


class Embedder(Protocol):
    model: str

    def passages(self, texts: list[str]) -> np.ndarray: ...

    def query(self, text: str) -> np.ndarray: ...


class FastEmbedder:
    def __init__(self, model: str = MODEL):
        from fastembed import TextEmbedding
        kwargs = {"model_name": model}
        if os.environ.get("FASTEMBED_CACHE"):
            kwargs["cache_dir"] = os.environ["FASTEMBED_CACHE"]
        kwargs["threads"] = int(os.environ.get("EMBED_THREADS", "2"))   # leave the cores to the vision model
        self.model = model
        self._m = TextEmbedding(**kwargs)

    def passages(self, texts: list[str]) -> np.ndarray:
        return _norm(np.array(list(self._m.passage_embed(texts, batch_size=BATCH)), dtype=np.float32))

    def query(self, text: str) -> np.ndarray:
        return _norm(np.array(list(self._m.query_embed([text])), dtype=np.float32))[0]


def _norm(m: np.ndarray) -> np.ndarray:
    if m.ndim == 1:
        n = np.linalg.norm(m)
        return m / n if n else m
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return (m / n).astype(np.float32)


_embedder: Embedder | None = None
_failed_until = 0.0
_lock = threading.Lock()


def get() -> Embedder | None:
    """The embedder, loaded once; None if embeddings are off or the model can't load (retried every 10 min)."""
    global _embedder, _failed_until
    if _embedder is not None:
        return _embedder
    if os.environ.get("IRRIS_EMBEDDINGS", "on").lower() in ("off", "0", "false"):
        return None
    if time.time() < _failed_until:
        return None
    with _lock:
        if _embedder is None and time.time() >= _failed_until:
            try:
                _embedder = FastEmbedder()
            except Exception as e:  # noqa: BLE001 - not installed, or the model can't be fetched
                log.warning("embeddings unavailable, using full-text search only: %s", e)
                _failed_until = time.time() + 600
    return _embedder


def set_embedder(e: Embedder | None) -> None:
    """Tests use a fake embedder."""
    global _embedder, _failed_until
    _embedder, _failed_until = e, 0.0
    _cache.clear()


# ---------------------------------------------------------------- per-workspace matrix cache
_cache: dict[str, tuple[int, np.ndarray, np.ndarray]] = {}       # ws -> (version, passage ids, matrix)
_versions: dict[str, int] = {}


def bump(workspace_id: str) -> None:
    _versions[workspace_id] = _versions.get(workspace_id, 0) + 1


def _matrix(db: Database, workspace_id: str, model: str) -> tuple[np.ndarray, np.ndarray]:
    version = _versions.get(workspace_id, 0)
    hit = _cache.get(workspace_id)
    if hit and hit[0] == version:
        return hit[1], hit[2]
    rows = db.execute("SELECT passage_id, vec FROM embeddings WHERE workspace_id = ? AND model = ?", [workspace_id, model])
    if rows:
        ids = np.array([r["passage_id"] for r in rows], dtype=np.int64)
        mat = np.vstack([np.frombuffer(bytes(r["vec"]), dtype=np.float32) for r in rows])
    else:
        ids, mat = np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32)
    _cache[workspace_id] = (version, ids, mat)
    return ids, mat


def nearest(db: Database, workspace_id: str, query: str, k: int = 30) -> list[tuple[int, float]]:
    """(passage_id, cosine) of the k most similar passages, or [] without embeddings."""
    e = get()
    if e is None or not query.strip():
        return []
    ids, mat = _matrix(db, workspace_id, e.model)
    if not len(ids):
        return []
    try:
        q = e.query(query)
    except Exception as ex:  # noqa: BLE001
        log.warning("query embedding failed: %s", ex)
        return []
    if q.shape[0] != mat.shape[1]:
        return []
    scores = mat @ q
    k = min(k, len(ids))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(int(ids[i]), float(scores[i])) for i in top]


def embed_missing(db: Database, workspace_id: str, limit: int = 512) -> int:
    """Embed up to `limit` passages that have no vector yet. Returns how many were embedded."""
    e = get()
    if e is None:
        return 0
    rows = db.execute("SELECT p.id, p.record_id, p.text FROM passages p LEFT JOIN embeddings x ON x.passage_id = p.id "
                      "WHERE p.workspace_id = ? AND x.passage_id IS NULL ORDER BY p.id LIMIT ?", [workspace_id, limit])
    if not rows:
        return 0
    vecs = e.passages([r["text"][:2000] for r in rows])
    with db.transaction() as tx:
        for r, v in zip(rows, vecs):
            if db.pg:
                tx.execute("INSERT INTO embeddings (passage_id, workspace_id, record_id, model, vec) VALUES (?,?,?,?,?) "
                           "ON CONFLICT (passage_id) DO NOTHING", [r["id"], workspace_id, r["record_id"], e.model,
                                                                   v.astype(np.float32).tobytes()])
            else:
                tx.execute("INSERT OR IGNORE INTO embeddings (passage_id, workspace_id, record_id, model, vec) "
                           "VALUES (?,?,?,?,?)", [r["id"], workspace_id, r["record_id"], e.model,
                                                  v.astype(np.float32).tobytes()])
    bump(workspace_id)
    return len(rows)


def pending(db: Database, workspace_id: str) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM passages p LEFT JOIN embeddings x ON x.passage_id = p.id "
                 "WHERE p.workspace_id = ? AND x.passage_id IS NULL", [workspace_id])
    return row["n"] if row else 0


def rrf(*rankings: list, k: int = 60) -> list:
    """Reciprocal rank fusion of several ranked lists of ids."""
    score: dict = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda key: -score[key])
