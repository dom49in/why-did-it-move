import unittest

from app import analysis as an
from tests import helpers as h


class TestSummariseAndLift(unittest.TestCase):
    rows = [{"member": "A", "base_value": 100, "compare_value": 40},
            {"member": "B", "base_value": 100, "compare_value": 95},
            {"member": "C", "base_value": 800, "compare_value": 780}]

    def test_shares_sum_to_one_and_deltas_are_exact(self):
        s = an.summarise(self.rows)
        self.assertAlmostEqual(s["scope_delta"], -85)
        self.assertAlmostEqual(sum(m["share"] for m in s["members"]), 1.0)
        self.assertEqual(s["members"][0]["member"], "A")          # sorted by |delta|

    def test_lift_separates_big_from_fast(self):
        s = {m["member"]: m for m in an.summarise(self.rows)["members"]}
        self.assertGreater(s["A"]["lift"], 5)                     # 71% of the change from 10% of the base
        self.assertLess(s["C"]["lift"], 0.5)                      # large but proportionally slow: not a driver
        picked = [m["member"] for m in an.contributors(an.summarise(self.rows))]
        self.assertEqual(picked[0], "A")
        self.assertNotIn("C", picked)

    def test_zero_change_has_no_shares(self):
        s = an.summarise([{"member": "A", "base_value": 5, "compare_value": 5}])
        self.assertIsNone(s["members"][0]["share"])


class TestMechanism(unittest.TestCase):
    def test_identity_matches_independent_computation_on_real_data(self):
        rows = h.load_rows()
        want = h.pvm(rows, "2026-08", "2026-09", product="Rain Jacket")
        a, b = h.sums(rows, "2026-08", product="Rain Jacket"), h.sums(rows, "2026-09", product="Rain Jacket")
        got = an.mechanism({"revenue_base": a["revenue"], "revenue_compare": b["revenue"], "cost_base": a["cost"],
                            "cost_compare": b["cost"], "units_base": a["units"], "units_compare": b["units"]},
                           "gross_margin")
        for k in want:
            self.assertAlmostEqual(got["effects"][k], want[k], delta=0.02)
        self.assertAlmostEqual(sum(got["effects"].values()), (b["revenue"] - b["cost"]) - (a["revenue"] - a["cost"]),
                               delta=0.05)                        # exact identity, not an estimate
        self.assertEqual(an.dominant_effect(got), "unit_cost")    # the seeded cost shock

    def test_zero_units_does_not_divide_by_zero(self):
        m = an.mechanism({"revenue_base": 0, "revenue_compare": 10, "cost_base": 0, "cost_compare": 5,
                          "units_base": 0, "units_compare": 2}, "gross_margin")
        self.assertEqual(m["effects"], {})


class TestGrounding(unittest.TestCase):
    payload = {"delta": -56693.3, "share": 0.583, "pct": -8.38, "lift": 3.03}

    def test_roundings_of_real_numbers_pass(self):
        for text in ("fell by 56,693.30", "fell about 56.7k", "58% of the change", "down 8.4%", "lift 3.0x"):
            self.assertEqual(an.ungrounded_numbers(text, self.payload), [], text)

    def test_invented_numbers_are_caught(self):
        bad = an.ungrounded_numbers("fell by 61,200 which is 72% of the change", self.payload)
        self.assertEqual(sorted(bad), ["61,200", "72%"])

    def test_dates_and_small_counts_ignored(self):
        self.assertEqual(an.numbers_in_text("In 2026-09 the top 3 segments"), [])


class TestDirection(unittest.TestCase):
    findings = [{"id": "f1", "delta": -10.0}, {"id": "f2", "delta": 5.0}]

    def test_wrong_direction_flagged(self):
        issues = an.direction_issues([{"finding_id": "f1", "statement": "Rain Jacket rose sharply"},
                                      {"finding_id": "f2", "statement": "Boots fell"}], self.findings)
        self.assertEqual(len(issues), 2)

    def test_right_direction_and_mixed_language_pass(self):
        self.assertEqual(an.direction_issues([{"finding_id": "f1", "statement": "fell, while prices were up"},
                                              {"finding_id": "f2", "statement": "rose"}], self.findings), [])


class TestExternalAssessment(unittest.TestCase):
    raw = {"base_value": 1.30, "compare_value": 1.40, "base_n": 20, "compare_n": 21}
    concentrated = [{"id": "f1", "level": 0, "dimension": "product", "member": "Rain Jacket",
                     "share_of_total": 0.58, "lift": 3.03}]
    spread = [{"id": "f1", "level": 0, "dimension": "region", "member": "Ontario", "share_of_total": 0.4, "lift": 1.1}]

    def test_market_wide_move_vs_concentrated_change_is_not_primary(self):
        a = an.external_assessment(self.raw, self.concentrated, "USD/CAD")
        self.assertEqual((a["verdict"], a["change_pct"]), ("concentrated", 7.69))
        self.assertIn("not the primary explanation", a["statement"])

    def test_broad_change_is_only_correlation(self):
        a = an.external_assessment(self.raw, self.spread, "USD/CAD")
        self.assertEqual(a["verdict"], "broad")
        self.assertIn("does not prove a cause", a["statement"])

    def test_flat_series_and_missing_data(self):
        self.assertEqual(an.external_assessment({**self.raw, "compare_value": 1.305}, self.concentrated, "x")["verdict"], "flat")
        self.assertEqual(an.external_assessment({"base_value": None, "compare_value": None, "base_n": 0, "compare_n": 0},
                                                self.concentrated, "x")["verdict"], "no_data")

    def test_statement_numbers_are_grounded_in_the_assessment_itself(self):
        a = an.external_assessment(self.raw, self.concentrated, "USD/CAD")
        self.assertEqual(an.ungrounded_numbers(a["statement"], a), [])


if __name__ == "__main__":
    unittest.main()
