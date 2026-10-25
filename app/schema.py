"""Schema description and SQL construction.

SQL is built from a whitelisted schema (data/schema.json): dimension and metric names can only be
ones declared there, periods must be YYYY-MM, and member values are quoted literals. The model
chooses *what to investigate*; it never writes raw SQL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from .errors import RequestError

PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def load_schema() -> dict:
    path = Path(config.get("SCHEMA_PATH", str(config.ROOT / "data" / "schema.json")))
    schema = json.loads(path.read_text())
    for name in [schema["table"], schema["date_column"], *schema["dimensions"]]:
        if not IDENT.match(name):
            raise RequestError(f"Invalid identifier in schema: {name!r}", stage="schema")
    for child, parent in schema.get("hierarchies", {}).items():
        if child not in schema["dimensions"] or parent not in schema["dimensions"]:
            raise RequestError(f"Hierarchy {child!r} -> {parent!r} names an unknown dimension.", stage="schema")
    return schema


def table_name(schema: dict) -> str:
    if config.sample_mode():
        return schema["table"]
    table = config.get("ZETARIS_TABLE", schema["table"])
    if not IDENT.match(table):
        raise RequestError(f"Invalid ZETARIS_TABLE: {table!r}", stage="schema")
    return table


def period_bounds(period: str) -> tuple[str, str]:
    if not isinstance(period, str) or not PERIOD.match(period):
        raise RequestError(f"Period must look like YYYY-MM, got {period!r}", stage="schema")
    year, month = int(period[:4]), int(period[5:])
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01", f"{ny:04d}-{nm:02d}-01"


def quote_literal(value) -> str:
    text = str(value)
    if len(text) > 200 or any(ord(c) < 32 for c in text):
        raise RequestError("Member value is too long or contains control characters.", stage="schema")
    return "'" + text.replace("'", "''") + "'"


def _metric_expr(schema: dict, metric: str) -> str:
    if metric not in schema["metrics"]:
        raise RequestError(f"Unknown metric {metric!r}. Supported: {sorted(schema['metrics'])}", stage="schema")
    return schema["metrics"][metric]


def _dimension(schema: dict, dimension: str) -> str:
    if dimension not in schema["dimensions"]:
        raise RequestError(f"Unknown dimension {dimension!r}. Supported: {sorted(schema['dimensions'])}",
                           stage="schema")
    return dimension


def _period_pred(date_col: str, period: str) -> str:
    start, end = period_bounds(period)
    return f"({date_col} >= '{start}' AND {date_col} < '{end}')"


def _scope_pred(schema: dict, filters: list[dict]) -> str:
    return "".join(f" AND {_dimension(schema, f['dimension'])} = {quote_literal(f['member'])}" for f in filters)


def total_sql(schema: dict, metric: str, base: str, compare: str, filters: list[dict] | None = None) -> str:
    d, expr = schema["date_column"], _metric_expr(schema, metric)
    b, c = _period_pred(d, base), _period_pred(d, compare)
    return (f"SELECT SUM(CASE WHEN {b} THEN {expr} ELSE 0 END) AS base_value, "
            f"SUM(CASE WHEN {c} THEN {expr} ELSE 0 END) AS compare_value "
            f"FROM {table_name(schema)} WHERE ({b} OR {c}){_scope_pred(schema, filters or [])}")


def breakdown_sql(schema: dict, dimension: str, metric: str, base: str, compare: str,
                  filters: list[dict] | None = None) -> str:
    d, expr, dim = schema["date_column"], _metric_expr(schema, metric), _dimension(schema, dimension)
    b, c = _period_pred(d, base), _period_pred(d, compare)
    return (f"SELECT {dim} AS member, "
            f"SUM(CASE WHEN {b} THEN {expr} ELSE 0 END) AS base_value, "
            f"SUM(CASE WHEN {c} THEN {expr} ELSE 0 END) AS compare_value "
            f"FROM {table_name(schema)} WHERE ({b} OR {c}){_scope_pred(schema, filters or [])} "
            f"GROUP BY {dim}")


def range_sql(schema: dict) -> str:
    d = schema["date_column"]
    return f"SELECT MIN({d}) AS min_date, MAX({d}) AS max_date FROM {table_name(schema)}"


COMPONENTS = ("revenue", "cost", "units")

# Words people use for each metric. Shared by the offline planner and by H-MEM's domain matching, so that
# "margin", "profit" and "gross margin" all reach the gross_margin domain. Order matters: first match wins.
METRIC_ALIASES = {
    "gross_margin": ("margin", "margins", "profit", "profits", "profitability"),
    "revenue": ("revenue", "revenues", "sales", "turnover", "income"),
    "units": ("units", "unit", "volume", "quantity", "quantities"),
}


def mechanism_sql(schema: dict, base: str, compare: str, filters: list[dict] | None = None) -> str:
    """One row with revenue, cost and units for both periods inside a scope (used to explain HOW a segment moved)."""
    d, comps = schema["date_column"], schema["components"]
    missing = [c for c in COMPONENTS if c not in comps]
    if missing:
        raise RequestError(f"Schema has no components {missing}", stage="schema")
    b, c = _period_pred(d, base), _period_pred(d, compare)
    cols = ", ".join(f"SUM(CASE WHEN {b} THEN {comps[k]} ELSE 0 END) AS {k}_base, "
                     f"SUM(CASE WHEN {c} THEN {comps[k]} ELSE 0 END) AS {k}_compare" for k in COMPONENTS)
    return f"SELECT {cols} FROM {table_name(schema)} WHERE ({b} OR {c}){_scope_pred(schema, filters or [])}"


MECHANISM_COLUMNS = [f"{k}_{p}" for k in COMPONENTS for p in ("base", "compare")]


# -- external context source (a public web API registered in Zetaris as a REST table) -----------------------
def external_spec(schema: dict, name: str = "fx_rates") -> dict | None:
    return (schema.get("external") or {}).get(name)


def external_table_name(schema: dict, name: str = "fx_rates") -> str | None:
    """Sample mode reads the bundled synthetic table. Live mode needs ZETARIS_FX_TABLE (the Zetaris view over the
    registered REST source); without it the external check is skipped and the response says so."""
    spec = external_spec(schema, name)
    if spec is None:
        return None
    if config.sample_mode():
        return spec["table"]
    table = config.get("ZETARIS_FX_TABLE")
    if not table:
        return None
    if not IDENT.match(table):
        raise RequestError(f"Invalid ZETARIS_FX_TABLE: {table!r}", stage="schema")
    return table


EXTERNAL_COLUMNS = ["base_value", "compare_value", "base_n", "compare_n"]


def external_sql(schema: dict, base: str, compare: str, name: str = "fx_rates") -> str:
    spec, table = external_spec(schema, name), external_table_name(schema, name)
    if spec is None or table is None:
        raise RequestError("External source is not configured.", stage="schema")
    for ident in (spec["date_column"], spec["base_column"], spec["quote_column"], spec["value_column"]):
        if not IDENT.match(ident):
            raise RequestError(f"Invalid identifier in schema: {ident!r}", stage="schema")
    d, v = spec["date_column"], spec["value_column"]
    b, c = _period_pred(d, base), _period_pred(d, compare)
    return (f"SELECT AVG(CASE WHEN {b} THEN {v} END) AS base_value, AVG(CASE WHEN {c} THEN {v} END) AS compare_value, "
            f"SUM(CASE WHEN {b} THEN 1 ELSE 0 END) AS base_n, SUM(CASE WHEN {c} THEN 1 ELSE 0 END) AS compare_n "
            f"FROM {table} WHERE ({b} OR {c}) AND {spec['base_column']} = {quote_literal(spec['base'])} "
            f"AND {spec['quote_column']} = {quote_literal(spec['quote'])}")
