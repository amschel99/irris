"""Connected databases. Postgres today: test, read the schema, run read-only queries, sync tables into records.

Every query runs in a READ ONLY transaction with a statement timeout, and only single SELECT / WITH / EXPLAIN
statements are accepted, so a connected database can be read but never changed through Irris.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from irris import irf

ROW_LIMIT = 200
SYNC_LIMIT = 5000
_READ = re.compile(r"^\s*(select|with|explain|show|values|table)\b", re.I)
# A first filter only: the READ ONLY transaction is the real guard (and connect with a read-only database user).
_WRITE = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|vacuum|call|"
                    r"into|set_config|pg_terminate_backend|pg_cancel_backend|dblink\w*|lo_\w+)\b", re.I)


class ConnectorError(Exception):
    pass


def check_sql(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise ConnectorError("empty query")
    if ";" in re.sub(r"'[^']*'", "''", s):
        raise ConnectorError("one statement at a time")
    if not _READ.match(s) or _WRITE.search(re.sub(r"'[^']*'", "''", s)):
        raise ConnectorError("only read-only queries (SELECT, WITH, EXPLAIN) are allowed")
    return s


def _connect(url: str):
    import psycopg
    from psycopg.rows import dict_row
    try:
        conn = psycopg.connect(url, connect_timeout=8, row_factory=dict_row, autocommit=False)
    except Exception as e:  # noqa: BLE001 - surface any driver error to the user, without the URL
        raise ConnectorError(f"couldn't connect: {str(e).splitlines()[0][:200]}")
    with conn.cursor() as cur:
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cur.execute("SET statement_timeout = '15s'")
    conn.commit()
    return conn


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def test(url: str) -> dict:
    conn = _connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS db, version() AS version")
            row = cur.fetchone()
        return {"ok": True, "database": row["db"], "version": row["version"].split(",")[0]}
    finally:
        conn.close()


def schema(url: str) -> list[dict]:
    conn = _connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
              SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
                     COALESCE(s.n_live_tup, 0) AS rows_estimate
              FROM information_schema.columns c
              LEFT JOIN pg_stat_user_tables s ON s.schemaname = c.table_schema AND s.relname = c.table_name
              WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
              ORDER BY c.table_schema, c.table_name, c.ordinal_position""")
            rows = cur.fetchall()
        tables: dict[str, dict] = {}
        for r in rows:
            key = f"{r['table_schema']}.{r['table_name']}"
            t = tables.setdefault(key, {"table": key, "rows_estimate": r["rows_estimate"], "columns": []})
            t["columns"].append({"name": r["column_name"], "type": r["data_type"]})
        return list(tables.values())[:300]
    finally:
        conn.close()


def run(url: str, sql: str, limit: int = ROW_LIMIT) -> dict:
    s = check_sql(sql)
    conn = _connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(s)
            cols = [d.name for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        conn.rollback()
        return {"columns": cols, "rows": [_jsonable(dict(r)) for r in rows[:limit]], "truncated": len(rows) > limit}
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConnectorError(str(e).splitlines()[0][:300])
    finally:
        conn.close()


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


def sync_table(url: str, table: str, source: str, now: float, limit: int = SYNC_LIMIT) -> list[dict]:
    """Rows of one table as IRF `row` records. The primary key (if any) becomes the record id, so re-syncing updates."""
    if not _IDENT.match(table):
        raise ConnectorError(f"{table!r} is not a table name")
    schema_name, name = table.split(".") if "." in table else ("public", table)
    conn = _connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
              SELECT a.attname FROM pg_index i
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
              WHERE i.indrelid = %s::regclass AND i.indisprimary""", [f'"{schema_name}"."{name}"'])
            pk = [r["attname"] for r in cur.fetchall()]
            cur.execute(f'SELECT * FROM "{schema_name}"."{name}" LIMIT %s', [limit])
            rows = cur.fetchall()
        conn.rollback()
    except Exception as e:  # noqa: BLE001
        raise ConnectorError(str(e).splitlines()[0][:300])
    finally:
        conn.close()
    out = []
    for r in rows:
        data = _jsonable(dict(r))
        ext = ":".join(str(data.get(k)) for k in pk) if pk else hashlib.sha1(
            json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:20]
        out.append(irf.normalize({"type": "row", "source": f"{source}/{schema_name}.{name}", "id": ext,
                                  "title": f"{name} {ext}" if pk else name, "data": data}, now=now))
    return out
