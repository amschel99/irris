"""Storage: Postgres in production (DATABASE_URL), SQLite on a laptop and in tests.

SQL is written once with `?` placeholders; for Postgres they become `%s`. The few dialect differences (ids,
full-text search) live here, so the rest of the code never checks which database it runs on.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from irris.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '', pw_hash TEXT NOT NULL,
  created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT NOT NULL DEFAULT '', about TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '', models TEXT NOT NULL DEFAULT '{}', created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS members (
  workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (workspace_id, user_id));
CREATE TABLE IF NOT EXISTS invites (
  token TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, email TEXT NOT NULL, role TEXT NOT NULL,
  created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
  expires DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL, prefix TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL, created DOUBLE PRECISION NOT NULL, last_used DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, type TEXT NOT NULL, source TEXT NOT NULL, ext_id TEXT,
  time DOUBLE PRECISION NOT NULL, subject TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}', units TEXT NOT NULL DEFAULT '{}',
  tags TEXT NOT NULL DEFAULT '[]', links TEXT NOT NULL DEFAULT '[]', created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS records_ext ON records (workspace_id, source, ext_id);
CREATE INDEX IF NOT EXISTS records_ws_time ON records (workspace_id, time);
CREATE INDEX IF NOT EXISTS records_ws_type ON records (workspace_id, type, time);
CREATE INDEX IF NOT EXISTS records_ws_subject ON records (workspace_id, subject);
CREATE TABLE IF NOT EXISTS passages (
  id {SERIAL}, workspace_id TEXT NOT NULL, record_id TEXT NOT NULL, ord INTEGER NOT NULL, page INTEGER,
  text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS passages_record ON passages (record_id);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}', secret TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
  last_sync DOUBLE PRECISION, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
  content {BLOB} NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS cameras (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '', token_hash TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, procedure_id TEXT NOT NULL, camera_id TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0,
  log TEXT NOT NULL DEFAULT '[]', label TEXT NOT NULL DEFAULT '', started DOUBLE PRECISION NOT NULL,
  ended DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS runs_ws ON runs (workspace_id, started);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id {SERIAL}, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}', created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conv ON messages (conversation_id, id);
-- ==== knowledge: jobs, embeddings, memory (irris/jobs.py, irris/embed.py, irris/memory.py) ====
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  progress DOUBLE PRECISION NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0, dedupe TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  run_after DOUBLE PRECISION NOT NULL DEFAULT 0, locked_until DOUBLE PRECISION, created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL, started DOUBLE PRECISION, finished DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs (status, run_after);
CREATE INDEX IF NOT EXISTS jobs_ws ON jobs (workspace_id, created);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs (dedupe, status);
CREATE TABLE IF NOT EXISTS stored_files (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, size BIGINT NOT NULL,
  path TEXT NOT NULL, sha256 TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS doc_pages (
  record_id TEXT NOT NULL, page INTEGER NOT NULL, workspace_id TEXT NOT NULL, text TEXT NOT NULL,
  PRIMARY KEY (record_id, page));
CREATE TABLE IF NOT EXISTS embeddings (
  passage_id BIGINT PRIMARY KEY, workspace_id TEXT NOT NULL, record_id TEXT NOT NULL, model TEXT NOT NULL,
  vec {BLOB} NOT NULL);
CREATE INDEX IF NOT EXISTS embeddings_ws ON embeddings (workspace_id);
CREATE INDEX IF NOT EXISTS embeddings_record ON embeddings (record_id);
CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
  text TEXT NOT NULL, norm TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', predicate TEXT NOT NULL DEFAULT '',
  importance DOUBLE PRECISION NOT NULL DEFAULT 0.6, confidence DOUBLE PRECISION NOT NULL DEFAULT 0.8,
  source_type TEXT NOT NULL DEFAULT 'agent_inferred', trust DOUBLE PRECISION NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
  evidence_ids TEXT NOT NULL DEFAULT '[]', superseded_by TEXT NOT NULL DEFAULT '', access_count INTEGER NOT NULL DEFAULT 0,
  valid_from DOUBLE PRECISION NOT NULL, valid_to DOUBLE PRECISION, last_used DOUBLE PRECISION,
  created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS memory_items_ws ON memory_items (workspace_id, status, kind);
CREATE INDEX IF NOT EXISTS memory_items_key ON memory_items (workspace_id, subject, predicate);
CREATE TABLE IF NOT EXISTS memory_events (
  id {SERIAL}, workspace_id TEXT NOT NULL, memory_id TEXT NOT NULL DEFAULT '', event TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}', created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS memory_events_ws ON memory_events (workspace_id, created);
CREATE TABLE IF NOT EXISTS llm_calls (
  id {SERIAL}, workspace_id TEXT NOT NULL, task TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, ms INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '',
  created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS llm_calls_ws ON llm_calls (workspace_id, created);
CREATE TABLE IF NOT EXISTS ws_limits (
  workspace_id TEXT PRIMARY KEY, daily_tokens BIGINT, daily_calls INTEGER, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS conv_summaries (
  conversation_id TEXT PRIMARY KEY, summary TEXT NOT NULL, upto BIGINT NOT NULL, updated DOUBLE PRECISION NOT NULL);
-- ==== media: voice, watch rules, video (irris/voice.py, irris/watch.py) ====
CREATE TABLE IF NOT EXISTS media_settings (
  workspace_id TEXT PRIMARY KEY, config TEXT NOT NULL DEFAULT '{}', updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS camera_state (
  camera_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
  coach_s INTEGER NOT NULL DEFAULT 0, notice INTEGER NOT NULL DEFAULT 0, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS watch_rules (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, camera_id TEXT NOT NULL, condition TEXT NOT NULL,
  interval_s INTEGER NOT NULL, severity TEXT NOT NULL, speak INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1, cooldown_s INTEGER NOT NULL DEFAULT 300, last_check DOUBLE PRECISION,
  last_match DOUBLE PRECISION, last_state INTEGER NOT NULL DEFAULT 0, last_say TEXT NOT NULL DEFAULT '',
  checks INTEGER NOT NULL DEFAULT 0, matches INTEGER NOT NULL DEFAULT 0, created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS watch_rules_ws ON watch_rules (workspace_id);
CREATE TABLE IF NOT EXISTS media_jobs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  progress DOUBLE PRECISION NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', params TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS media_jobs_ws ON media_jobs (workspace_id, created);
CREATE TABLE IF NOT EXISTS observations (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, camera_id TEXT NOT NULL DEFAULT '', t_start DOUBLE PRECISION NOT NULL,
  t_end DOUBLE PRECISION NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT '', conf DOUBLE PRECISION NOT NULL DEFAULT 0.5, evidence TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS observations_ws_t ON observations (workspace_id, t_start);
CREATE INDEX IF NOT EXISTS observations_cam ON observations (workspace_id, camera_id, subject, relation, t_end);
CREATE TABLE IF NOT EXISTS camera_rois (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, camera_id TEXT NOT NULL, key TEXT NOT NULL,
  box TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', updated DOUBLE PRECISION NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS camera_rois_key ON camera_rois (camera_id, key);
CREATE TABLE IF NOT EXISTS shift_summaries (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, camera_id TEXT NOT NULL DEFAULT '', level INTEGER NOT NULL,
  t_start DOUBLE PRECISION NOT NULL, t_end DOUBLE PRECISION NOT NULL, text TEXT NOT NULL,
  merged INTEGER NOT NULL DEFAULT 0, created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS shift_summaries_cam ON shift_summaries (workspace_id, camera_id, level, merged);
-- guidance v2 (irris/guidance.py, irris/instruments.py)
CREATE TABLE IF NOT EXISTS run_tracker (
  run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}',
  updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, camera_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
  type TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS instruments_ws ON instruments (workspace_id);
"""

PG_EXTRA = """
CREATE INDEX IF NOT EXISTS passages_fts ON passages USING GIN (to_tsvector('english', text));
"""

SQLITE_EXTRA = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(text, content='passages', content_rowid='id',
  tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
  INSERT INTO passages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
  INSERT INTO passages_fts(passages_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
"""


def new_id() -> str:
    return uuid.uuid4().hex[:20]


def now() -> float:
    return time.time()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


PASSAGE_COLS = ("p.id AS passage_id, p.record_id, p.ord, p.page, p.text AS passage, r.type, r.source, r.title, "
                "r.subject, r.time")


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            self._psycopg = psycopg
            self._dict_row = dict_row
            self._pool: queue.LifoQueue = queue.LifoQueue()
        else:
            path = url.split("sqlite:///", 1)[-1] if url.startswith("sqlite:") else url
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=OFF")
        self.migrate()

    # ---------------------------------------------------------------- connections
    def _pg_conn(self):
        try:
            conn = self._pool.get_nowait()
            if conn.closed or conn.broken:
                raise queue.Empty
            return conn
        except queue.Empty:
            return self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row)

    def _pg_release(self, conn) -> None:
        if not conn.closed and not conn.broken and self._pool.qsize() < 10:
            self._pool.put(conn)
        else:
            conn.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    # ---------------------------------------------------------------- queries
    def execute(self, sql: str, params: tuple | list = ()) -> list[dict]:
        if self.pg:
            conn = self._pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(self._sql(sql), params)
                    rows = cur.fetchall() if cur.description else []
                return [dict(r) for r in rows]
            finally:
                self._pg_release(conn)
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def insert_id(self, sql: str, params: tuple | list = ()) -> int:
        """INSERT into a table with a serial `id` and return it."""
        if self.pg:
            return self.one(sql + " RETURNING id", params)["id"]
        with self._lock:
            return self._conn.execute(sql, params).lastrowid

    @contextmanager
    def transaction(self) -> Iterator["Tx"]:
        if self.pg:
            conn = self._pg_conn()
            try:
                with conn.transaction():
                    yield Tx(self, conn)
            finally:
                self._pg_release(conn)
        else:
            with self._lock:
                self._conn.execute("BEGIN")
                try:
                    yield Tx(self, self._conn)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise

    def migrate(self) -> None:
        serial = "BIGSERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        blob = "BYTEA" if self.pg else "BLOB"
        ddl = SCHEMA.replace("{SERIAL}", serial).replace("{BLOB}", blob) + (PG_EXTRA if self.pg else SQLITE_EXTRA)
        if self.pg:
            self.execute(ddl)
        else:
            with self._lock:
                self._conn.executescript(ddl)

    # ---------------------------------------------------------------- full-text search
    def search_passages(self, workspace_id: str, query: str, limit: int = 8, types: list[str] | None = None,
                        record_id: str | None = None) -> list[dict]:
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query.lower()).split() if len(t) > 1][:24]
        if not terms:
            return []
        type_sql, type_params = "", []
        if types:
            type_sql = f" AND r.type IN ({','.join('?' * len(types))})"
            type_params = list(types)
        if record_id:
            type_sql += " AND p.record_id = ?"
            type_params.append(record_id)
        cols = PASSAGE_COLS
        if self.pg:
            q = " | ".join(terms)
            return self.execute(
                f"SELECT {cols}, ts_rank(to_tsvector('english', p.text), to_tsquery('english', ?)) AS score "
                f"FROM passages p JOIN records r ON r.id = p.record_id "
                f"WHERE p.workspace_id = ? AND to_tsvector('english', p.text) @@ to_tsquery('english', ?){type_sql} "
                f"ORDER BY score DESC LIMIT ?", [q, workspace_id, q, *type_params, limit])
        q = " OR ".join(f'"{t}"' for t in terms)
        return self.execute(
            f"SELECT {cols}, -bm25(passages_fts) AS score FROM passages_fts "
            f"JOIN passages p ON p.id = passages_fts.rowid JOIN records r ON r.id = p.record_id "
            f"WHERE passages_fts MATCH ? AND p.workspace_id = ?{type_sql} ORDER BY bm25(passages_fts) LIMIT ?",
            [q, workspace_id, *type_params, limit])


class Tx:
    """The same execute/one API, inside one transaction."""

    def __init__(self, db: Database, conn):
        self.db, self.conn = db, conn

    def execute(self, sql: str, params: tuple | list = ()) -> list[dict]:
        if self.db.pg:
            with self.conn.cursor() as cur:
                cur.execute(self.db._sql(sql), params)
                return [dict(r) for r in cur.fetchall()] if cur.description else []
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.execute(sql, params)
        return rows[0] if rows else None


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database(settings.database_url)
        return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db
