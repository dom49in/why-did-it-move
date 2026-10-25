"""Read-only guard applied to every SQL statement before it leaves the app."""
from __future__ import annotations

import re

from .errors import DataError

_LITERAL = re.compile(r"'(?:[^']|'')*'")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|truncate|merge|grant|revoke|replace|exec|call)\b")


def assert_read_only(sql: str) -> None:
    stripped = _LITERAL.sub("''", sql).strip().rstrip(";").strip()
    lowered = stripped.lower()
    if not lowered.startswith("select"):
        raise DataError("Only SELECT statements are allowed.", stage="sql_guard")
    if ";" in stripped or "--" in stripped or "/*" in stripped:
        raise DataError("Multiple statements and comments are not allowed.", stage="sql_guard")
    if _FORBIDDEN.search(lowered):
        raise DataError("Statement contains a forbidden keyword.", stage="sql_guard")
