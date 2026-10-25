"""Live-mode code paths, exercised against local stand-ins (tests/helpers.py). These prove the HTTP contract,
headers, retries, row-shape tolerance, truncation handling and model logging. They do NOT prove that real
Zetaris or Nightshift behave this way: that needs credentials (see README, 'Verified vs unverified')."""
import json
import unittest

from app import data_access, llm, pipeline
from app.errors import DataError, LLMError
from app.memory import InvestigationMemory
from tests import helpers as h


def live_env(z, n, d, **extra):
    cfg = dict(ZETARIS_REST_URL=z.url, ZETARIS_API_KEY="z-key", ZETARIS_ORG_ID="org-1",
               METERLESS_BASE_URL=n.url if n else None, METERLESS_RUNTIME_TOKEN="test-runtime-token" if n else None,
               MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l"), LLM_BACKOFF_SECONDS="0", ZETARIS_BACKOFF_SECONDS="0")
    cfg.update(extra)
    return h.env(**cfg)


class TestZetarisContract(unittest.TestCase):
    def test_request_shape_and_headers(self):
        with h.tempdir() as d, h.FakeZetaris() as z, live_env(z, None, d):
            r = data_access.query("SELECT MIN(order_date) AS min_date, MAX(order_date) AS max_date FROM sales_facts",
                                  ["min_date", "max_date"])
        self.assertEqual(r.source, "zetaris")
        req = z.requests[0]
        self.assertEqual(req["path"], "/api/v1.0/sql-editor/sqls/run")
        self.assertEqual(req["headers"]["authorization"], "Bearer z-key")
        self.assertEqual(req["headers"]["x-org-id"], "org-1")
        self.assertTrue(req["headers"]["x-request-id"])
        self.assertEqual(set(req["body"]), {"queryId", "sql", "source", "limit"})
        self.assertEqual(req["body"]["source"], "SqlEditor")

    def test_row_shapes_are_normalised(self):
        for shape in ("dicts", "lists", "upper_dicts", "other_keys"):
            with h.tempdir() as d, h.FakeZetaris(shape=shape) as z, live_env(z, None, d):
                r = data_access.query("SELECT order_date AS min_date, order_date AS max_date FROM sales_facts",
                                      ["min_date", "max_date"])
                self.assertTrue(r.rows and set(r.rows[0]) == {"min_date", "max_date"}, shape)

    def test_retries_then_succeeds_and_then_gives_up(self):
        with h.tempdir() as d, h.FakeZetaris(fail_status=503, fail_count=1) as z, live_env(z, None, d):
            data_access.query("SELECT MIN(order_date) AS a FROM sales_facts", ["a"])
            self.assertEqual(len(z.requests), 2)
        with h.tempdir() as d, h.FakeZetaris(fail_status=503, fail_count=9) as z, live_env(z, None, d):
            with self.assertRaises(DataError) as cm:
                data_access.query("SELECT MIN(order_date) AS a FROM sales_facts", ["a"])
            self.assertEqual(cm.exception.stage, "zetaris")

    def test_client_error_is_not_retried(self):
        with h.tempdir() as d, h.FakeZetaris(fail_status=401, fail_count=9) as z, live_env(z, None, d):
            with self.assertRaises(DataError):
                data_access.query("SELECT 1 AS a", ["a"])
            self.assertEqual(len(z.requests), 1)

    def test_write_never_leaves_the_process(self):
        with h.tempdir() as d, h.FakeZetaris() as z, live_env(z, None, d):
            with self.assertRaises(DataError):
                data_access.query("DELETE FROM sales_facts", ["a"])
            self.assertEqual(z.requests, [])

    def test_row_cap_marks_truncated(self):
        with h.tempdir() as d, h.FakeZetaris(row_cap=3) as z, live_env(z, None, d, ZETARIS_ROW_LIMIT="3"):
            r = data_access.query("SELECT product AS member, order_date AS base_value, order_date AS compare_value "
                                  "FROM sales_facts", ["member", "base_value", "compare_value"])
            self.assertTrue(r.truncated)


class TestNightshiftContract(unittest.TestCase):
    def test_headers_model_and_logged_model_name(self):
        with h.tempdir() as d, h.FakeNightshift() as n, h.env(METERLESS_BASE_URL=n.url, METERLESS_RUNTIME_TOKEN="test-runtime-token"):
            out = llm.chat([{"role": "user", "content": "hi"}])
        req = n.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer test-runtime-token")
        self.assertEqual(req["headers"]["meterless-scope"], "user")
        self.assertTrue(req["headers"]["meterless-app"])
        self.assertEqual(req["body"]["model"], "meterless/auto")
        self.assertEqual(out["model"], "nvidia/test-model-1")            # response.model, not the requested alias

    def test_missing_credentials_fail_loudly(self):
        with h.env():
            with self.assertRaises(LLMError):
                llm.chat([{"role": "user", "content": "x"}])

    def test_offline_rules_refused_outside_sample_mode(self):
        with h.env(LLM_MODE="offline_rules"):
            with self.assertRaises(LLMError):
                llm.mode()
        with h.env():
            self.assertEqual(llm.mode(), "nightshift")                    # no silent fallback in live mode

    def test_retry_on_503_and_json_repair(self):
        with h.FakeNightshift(fail_first=1) as n, h.env(METERLESS_BASE_URL=n.url, METERLESS_RUNTIME_TOKEN="test-runtime-token", LLM_BACKOFF_SECONDS="0"):
            self.assertEqual(llm.chat([{"role": "user", "content": "x"}])["retries"], 1)
        with h.FakeNightshift(prose_first=1) as n, h.env(METERLESS_BASE_URL=n.url, METERLESS_RUNTIME_TOKEN="test-runtime-token"):
            obj, meta = llm.chat_json([{"role": "system", "content": "ROLE: PLANNER"}, {"role": "user", "content": "x"}])
            self.assertIsInstance(obj, dict)

    def test_extract_json_variants(self):
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(llm.extract_json('Here:\n```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(llm.extract_json('prefix {"a": 3} suffix'), {"a": 3})
        with self.assertRaises(ValueError):
            llm.extract_json("no json")

    def test_bad_token_is_a_structured_error(self):
        with h.FakeNightshift(token="right") as n, h.env(METERLESS_BASE_URL=n.url, METERLESS_RUNTIME_TOKEN="wrong"):
            with self.assertRaises(LLMError):
                llm.chat([{"role": "user", "content": "x"}])


class TestFullRunOverTheWire(unittest.TestCase):
    def test_zetaris_plus_nightshift_standins_end_to_end(self):
        with h.tempdir() as d, h.FakeZetaris() as z, h.FakeNightshift() as n, live_env(z, n, d):
            resp, status = pipeline.run_request({"user_query": "Why did gross margin fall in September versus August?"},
                                                memory=InvestigationMemory())
        self.assertEqual(status, 200, resp.get("error"))
        self.assertEqual(resp["data_source"], "zetaris")
        self.assertEqual(resp["reasoning_mode"], "nightshift")
        self.assertEqual(resp["models_used"], ["nvidia/test-model-1"])    # model that handled the calls is logged
        self.assertEqual(resp["result"]["findings"][0]["member"], "Rain Jacket")
        self.assertGreater(len(z.requests), 8)
        self.assertGreater(len(n.requests), 8)
        for req in z.requests:
            self.assertTrue(req["body"]["sql"].lstrip().upper().startswith("SELECT"))

    def test_no_silent_fallback_when_nightshift_is_down(self):
        with h.tempdir() as d, h.FakeZetaris() as z, live_env(z, None, d, METERLESS_BASE_URL="http://127.0.0.1:1/v1",
                                                               METERLESS_RUNTIME_TOKEN="t"):
            resp, status = pipeline.run_request({"user_query": "Why did gross margin fall in September versus August?"},
                                                memory=InvestigationMemory())
        self.assertEqual(status, 502)
        self.assertEqual(resp["error"]["stage"], "llm")

    def test_truncated_zetaris_results_are_disclosed_or_rejected(self):
        with h.tempdir() as d, h.FakeZetaris(row_cap=2) as z, h.FakeNightshift() as n, live_env(z, n, d, ZETARIS_ROW_LIMIT="2"):
            resp, status = pipeline.run_request({"user_query": "Why did gross margin fall in September versus August?"},
                                                memory=InvestigationMemory())
        self.assertEqual(status, 200)
        checks = resp["collaboration"]["final_checks"]
        self.assertFalse(checks["not_truncated"])
        self.assertFalse(checks["sums_reconcile"])                       # partial data cannot reconcile to the total
        self.assertFalse(resp["collaboration"]["feedback_loop_completed"])   # never reported as a clean pass
        self.assertTrue(any("row cap" in c.lower() for c in resp["result"]["caveats"]))
        self.assertTrue(resp["collaboration"]["unresolved_issues"])


if __name__ == "__main__":
    unittest.main()
