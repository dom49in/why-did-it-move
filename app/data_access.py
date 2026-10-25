"""Single entry point for data. Zetaris is the data layer; SAMPLE_MODE swaps only the source of rows."""
from __future__ import annotations

from typing import NamedTuple

from . import config, sample_store
from .adapters import zetaris
from .errors import DataError


class Result(NamedTuple):
    rows: list
    source: str
    truncated: bool   # True when the row cap was hit, so the result may be incomplete


def _normalise(rows: list, columns: list[str]) -> list[dict]:
    """Rows may arrive as lists (positional, in SELECT order) or dicts (keyed by column name or alias)."""
    out = []
    for row in rows:
        if isinstance(row, dict):
            lowered = {str(k).lower().split(".")[-1]: v for k, v in row.items()}
            if all(c in lowered for c in columns):
                out.append({c: lowered[c] for c in columns})
            elif len(row) >= len(columns):          # keys are not our aliases: fall back to SELECT order
                out.append(dict(zip(columns, list(row.values()))))
            else:
                raise DataError(f"Row keys {sorted(lowered)} do not cover columns {columns}", stage="data_access")
        elif isinstance(row, (list, tuple)):
            if len(row) < len(columns):
                raise DataError(f"Row has {len(row)} values, expected {len(columns)}: {columns}", stage="data_access")
            out.append(dict(zip(columns, row)))
        else:
            raise DataError(f"Unexpected row type {type(row).__name__}", stage="data_access")
    return out


def source_name() -> str:
    return "sample_data" if config.sample_mode() else "zetaris"


def query(sql: str, columns: list[str]) -> Result:
    """Run SQL and return rows as dicts keyed by `columns`. Column order must match the SELECT."""
    if config.sample_mode():
        raw, truncated = sample_store.run_sql(sql), False
    else:
        raw = zetaris.run_sql(sql)
        truncated = len(raw) >= zetaris.row_limit()
    return Result(_normalise(raw, columns), source_name(), truncated)
