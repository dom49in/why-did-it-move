"""End-to-end in SAMPLE_MODE (offline-rules reasoning). Findings are checked against ground truth computed
independently from the CSV, not against stored answers."""
import json
import unittest

import run
from app import pipeline
from tests import helpers as h


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = h.tempdir()
        d = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.env = h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "mem"), LOG_DIR=str(d / "logs"))
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.dir = d
        run._memory = None if hasattr(run, "_memory") else None

    def go(self, query, request_id="t"):
        from app.memory import InvestigationMemory
        return pipeline.run_request({"request_id": request_id, "user_query": query}, memory=InvestigationMemory())


class TestGrossMarginSeptember(Base):
    def setUp(self):
        super().setUp()
        self.resp, self.status = self.go("Why did gross margin fall in September versus August?")
        self.res = self.resp["result"]
        self.rows = h.load_rows()

    def test_success_and_required_fields(self):
        self.assertEqual(self.status, 200)
        for k in ("request_id", "trace_id", "status", "agents_used", "result", "collaboration", "models_used", "log_path"):
            self.assertIn(k, self.resp)
        self.assertEqual(self.resp["agents_used"], ["PlannerAgent", "ExecutorAgent", "EvaluatorAgent"])
        self.assertEqual(self.resp["data_source"], "sample_data")
        self.assertEqual(self.resp["models_used"], ["offline-rules"])        # never presented as a real model

    def test_total_change_matches_csv(self):
        t = self.res["total_change"]
        self.assertAlmostEqual(t["base"], h.margin(self.rows, "2026-08"), delta=0.1)
        self.assertAlmostEqual(t["compare"], h.margin(self.rows, "2026-09"), delta=0.1)
        self.assertLess(t["delta"], 0)

    def test_top_driver_matches_independent_truth(self):
        truth = h.group_delta(self.rows, "product", "2026-08", "2026-09")
        worst = min(truth, key=truth.get)
        top = self.res["findings"][0]
        self.assertEqual((top["dimension"], top["member"]), ("product", worst))
        self.assertEqual(worst, "Rain Jacket")
        self.assertAlmostEqual(top["delta"], truth[worst], delta=0.1)

    def test_mechanism_names_the_cost_shock(self):
        mech = self.res["findings"][0]["mechanism"]
        self.assertEqual(max(mech["effects"], key=lambda k: abs(mech["effects"][k])), "unit_cost")
        self.assertAlmostEqual(mech["unit_cost_change_pct"], 20.0, delta=2.0)

    def test_every_claim_cites_a_finding_and_a_query(self):
        ids = {f["id"] for f in self.res["findings"]}
        qids = {e["query_id"] for e in self.res["evidence"]}
        self.assertTrue(self.res["claims"])
        for c in self.res["claims"]:
            self.assertIn(c["finding_id"], ids)
        for f in self.res["findings"]:
            self.assertTrue(f["sources"])
            self.assertTrue(set(f["sources"]) <= qids, f["sources"])

    def test_feedback_loop_is_real_and_checks_pass(self):
        c = self.resp["collaboration"]
        self.assertGreaterEqual(c["critiques_issued"], 1)
        self.assertGreaterEqual(c["revision_count"], 1)
        self.assertTrue(c["feedback_loop_completed"])
        for k, v in c["final_checks"].items():
            if isinstance(v, bool):
                self.assertTrue(v, k)

    def test_logs_written_to_file_with_required_fields(self):
        with open(self.resp["log_path"]) as fh:
            lines = [json.loads(l) for l in fh.read().splitlines()]
        self.assertGreater(len(lines), 15)
        for rec in lines:
            for k in ("timestamp", "trace_id", "agent_name", "action", "status", "model"):
                self.assertIn(k, rec)
        agents = {r["agent_name"] for r in lines}
        self.assertTrue({"PlannerAgent", "ExecutorAgent", "EvaluatorAgent"} <= agents)
        self.assertTrue(any(r["model"] == "offline-rules" for r in lines))
        self.assertTrue(any(r["action"] == "critique" or "critique" in r["action"] for r in lines))

    def test_markovian_is_load_bearing_and_bounded(self):
        m = self.resp["engines"]["markovian"]
        self.assertGreaterEqual(m["steps"], 4)
        self.assertEqual(m["token_label"], "estimated")
        self.assertLessEqual(m["max_prompt_chars"], 4000)
        self.assertIn("TS conformance not run", m["implementation"])


class TestMemoryLoop(Base):
    def test_second_run_recalls_and_gives_feedback_on_first(self):
        first, _ = self.go("Why did gross margin fall in September versus August?", "a")
        self.assertEqual(first["result"]["memory"]["recalled"], [])
        self.assertGreater(len(first["result"]["memory"]["stored"]), 0)
        second, _ = self.go("Why did gross margin fall in September compared to August?", "b")      # reworded: "versus" -> "compared to". Looser paraphrases ('profit', 'drop') score below the threshold; see README limits
        mem = second["result"]["memory"]
        self.assertGreater(len(mem["recalled"]), 0)
        self.assertTrue(any(f["signal"] == "helpful" for f in mem["feedback_applied"]))
        # recalled memories are hints: the numbers still come from queries
        self.assertEqual(second["result"]["findings"][0]["member"], "Rain Jacket")

    def test_memory_can_be_disabled(self):
        with h.env(SAMPLE_MODE="true", MEMORY_ENABLED="false", MEMORY_DIR=str(self.dir / "m2"), LOG_DIR=str(self.dir / "l2")):
            from app.memory import InvestigationMemory
            resp, status = pipeline.run_request({"user_query": "Why did gross margin fall in September versus August?"},
                                                memory=InvestigationMemory())
        self.assertEqual(status, 200)
        self.assertEqual(resp["result"]["memory"]["stored"], [])


class TestOtherQuestions(Base):
    def test_revenue_july_to_august(self):
        resp, status = self.go("What drove the change in revenue from July to August?")
        self.assertEqual(status, 200)
        rows = h.load_rows()
        t = resp["result"]["total_change"]
        self.assertEqual(resp["result"]["metric"], "revenue")
        self.assertEqual((resp["result"]["base_period"], resp["result"]["compare_period"]), ("2026-07", "2026-08"))
        self.assertAlmostEqual(t["delta"], h.sums(rows, "2026-08")["revenue"] - h.sums(rows, "2026-07")["revenue"], delta=0.1)

    def test_units_september_vs_august(self):
        resp, status = self.go("Explain why units sold changed in September compared to August.")
        self.assertEqual(status, 200)
        rows = h.load_rows()
        self.assertEqual(resp["result"]["metric"], "units")
        self.assertAlmostEqual(resp["result"]["total_change"]["delta"],
                               h.sums(rows, "2026-09")["units"] - h.sums(rows, "2026-08")["units"], delta=0.1)


class TestRefusesWhatItCannotAnswer(Base):
    def test_empty_and_wrong_types(self):
        for body in ({}, {"user_query": ""}, {"user_query": 5}, {"user_query": "x" * 2001}, [], "str"):
            resp, status = pipeline.run_request(body)
            self.assertEqual(status, 422, body)
            self.assertEqual(resp["status"], "error")
            self.assertEqual(resp["error"]["type"], "invalid_request")

    def test_period_outside_the_data_is_a_structured_error_not_a_made_up_answer(self):
        resp, status = self.go("Why did revenue fall in March 2019 versus February 2019?")
        self.assertNotEqual(status, 200)
        self.assertEqual(resp["status"], "error")
        self.assertIn("type", resp["error"])


if __name__ == "__main__":
    unittest.main()
