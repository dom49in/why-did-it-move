"""Zetaris data-layer adapter: one SQL statement per call via the Zetaris REST API.

POST {ZETARIS_REST_URL}/sql-editor/sqls/run with a bearer token, X-Org-ID and a fresh X-Request-ID.
Retries once on timeouts, connection errors and 5xx/429, then raises a structured DataError.
"""
from __future__ import annotations

import os
import time
import uuid

import requests

from .. import config
from ..errors import DataError
from ..sqlguard import assert_read_only

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def row_limit() -> int:
    try:
        return max(1, int(config.get("ZETARIS_ROW_LIMIT", "1000")))
    except ValueError:
        return 1000


def _extract_rows(payload) -> list:
    if isinstance(payload, dict):
        data = payload.get("data", payload.get("rows"))
        if isinstance(data, dict):
            data = data.get("rows", data.get("data"))
    else:
        data = None
    if not isinstance(data, list):
        raise DataError("Zetaris returned a response without a list of rows (expected key 'data').",
                        stage="zetaris", details={"keys": sorted(payload) if isinstance(payload, dict) else None})
    return data


def run_sql(sql: str, limit: int | None = None) -> list:
    assert_read_only(sql)
    limit = limit or row_limit()
    try:
        base = os.environ["ZETARIS_REST_URL"].rstrip("/")
        api_key = os.environ["ZETARIS_API_KEY"]
        org_id = os.environ["ZETARIS_ORG_ID"]
    except KeyError as exc:
        raise DataError(f"Missing Zetaris setting: {exc.args[0]}", stage="zetaris") from exc
    timeout = config.get_float("ZETARIS_TIMEOUT", 60.0)
    attempts, last = 2, ""
    for attempt in range(attempts):
        try:
            response = requests.post(
                base + "/sql-editor/sqls/run",
                headers={
                    "Authorization": "Bearer " + api_key,
                    "X-Org-ID": org_id,
                    "X-Request-ID": str(uuid.uuid4()),
                },
                json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = f"unreachable ({type(exc).__name__})"
        else:
            if response.status_code in RETRYABLE_STATUS:
                last = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise DataError(f"Zetaris returned HTTP {response.status_code}: {response.text[:300]}",
                                stage="zetaris", details={"http_status": response.status_code})
            else:
                try:
                    return _extract_rows(response.json())
                except ValueError as exc:
                    raise DataError("Zetaris returned a non-JSON response.", stage="zetaris") from exc
        if attempt + 1 < attempts:
            time.sleep(config.get_float("ZETARIS_BACKOFF_SECONDS", 1.0) * (attempt + 1))
    raise DataError(f"Zetaris failed after {attempts} attempts: {last}", stage="zetaris",
                    details={"attempts": attempts})
