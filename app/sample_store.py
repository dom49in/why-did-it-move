"""SAMPLE_MODE data source.

Loads *input data* (data/sample/sales_facts.csv) into an in-memory SQLite table and runs the very same
SQL the Zetaris path would run. It never returns prewritten answers; results are computed per query.
"""
from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path

from . import config
from .errors import DataError
from .schema import load_schema
from .sqlguard import assert_read_only

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_loaded_from: str | None = None


def _load_table(conn: sqlite3.Connection, path: Path, table: str) -> None:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        columns = reader.fieldnames or []
    numeric = set()
    for col in columns:
        try:
            for r in rows:
                float(r[col])
            numeric.add(col)
        except (TypeError, ValueError):
            pass
    col_defs = ", ".join(f"{c} {'REAL' if c in numeric else 'TEXT'}" for c in columns)
    conn.execute(f"CREATE TABLE {table} ({col_defs})")
    conn.executemany(
        f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
        [[float(r[c]) if c in numeric else r[c] for c in columns] for r in rows],
    )


def _connect() -> sqlite3.Connection:
    """In-memory SQLite holding the sales facts plus, if present, each external table declared in the schema."""
    global _conn, _loaded_from
    path = Path(config.get("SAMPLE_DATA_PATH", str(config.ROOT / "data" / "sample" / "sales_facts.csv")))
    if _conn is not None and _loaded_from == str(path):
        return _conn
    if not path.exists():
        raise DataError(f"Sample data not found at {path}. Run scripts/make_sample_data.py", stage="sample_data")
    schema = load_schema()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    _load_table(conn, path, schema["table"])
    for spec in schema.get("external", {}).values():
        ext = path.parent / f"{spec['table']}.csv"
        if ext.exists():
            _load_table(conn, ext, spec["table"])
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    _conn, _loaded_from = conn, str(path)
    return conn


def run_sql(sql: str) -> list[dict]:
    assert_read_only(sql)
    with _lock:
        try:
            cur = _connect().execute(sql)
            return [{k.lower(): row[k] for k in row.keys()} for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise DataError(f"Sample data query failed: {exc}", stage="sample_data") from exc
