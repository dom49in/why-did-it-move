import unittest

from app import config, sample_store, schema
from app.errors import AppError, DataError, RequestError
from app.sqlguard import assert_read_only
from tests import helpers as h


class TestGuard(unittest.TestCase):
    def test_select_allowed(self):
        assert_read_only("SELECT a FROM t WHERE b = 'drop table x'")      # keyword inside a literal is fine
        assert_read_only("select 1;")

    def test_everything_else_refused(self):
        for sql in ("DROP TABLE sales_facts", "SELECT 1; DELETE FROM t", "SELECT 1 -- x", "SELECT 1 /* x */",
                    "UPDATE t SET a=1", "WITH x AS (SELECT 1) DELETE FROM t", "PRAGMA table_info(t)",
                    "SELECT * FROM t; SELECT 2"):
            with self.assertRaises(DataError, msg=sql):
                assert_read_only(sql)


class TestSqlBuilders(unittest.TestCase):
    def setUp(self):
        self.sch = schema.load_schema()

    def test_unknown_metric_and_dimension_rejected(self):
        with self.assertRaises(AppError):
            schema.breakdown_sql(self.sch, "region; DROP TABLE x", "gross_margin", "2026-08", "2026-09")
        with self.assertRaises(AppError):
            schema.total_sql(self.sch, "bogus_metric", "2026-08", "2026-09")

    def test_filter_values_are_escaped(self):
        self.assertEqual(schema.quote_literal("O'Brien"), "'O''Brien'")
        sql = schema.total_sql(self.sch, "revenue", "2026-08", "2026-09", [{"dimension": "region", "member": "x' OR '1'='1"}])
        assert_read_only(sql)
        self.assertIn("x'' OR ''1''=''1", sql)

    def test_bad_period_rejected(self):
        for p in ("2026-13", "September", "2026-9", "'; DROP"):
            with self.assertRaises(AppError, msg=p):
                schema.period_bounds(p)

    def test_every_builder_output_passes_the_guard(self):
        for sql in (schema.range_sql(self.sch), schema.total_sql(self.sch, "units", "2026-08", "2026-09"),
                    schema.breakdown_sql(self.sch, "region", "gross_margin", "2026-08", "2026-09"),
                    schema.mechanism_sql(self.sch, "2026-08", "2026-09", [{"dimension": "product", "member": "Rain Jacket"}])):
            assert_read_only(sql)


class TestExternalSql(unittest.TestCase):
    def test_sample_query_matches_csv_truth_and_passes_guard(self):
        with h.env(SAMPLE_MODE="true"):
            sch = schema.load_schema()
            sql = schema.external_sql(sch, "2026-08", "2026-09")
            assert_read_only(sql)
            r = sample_store.run_sql(sql)[0]
            import csv
            rows = list(csv.DictReader(open(h.ROOT / "data" / "sample" / "fx_rates.csv")))
            for month, key in (("2026-08", "base"), ("2026-09", "compare")):
                vals = [float(x["rate"]) for x in rows if x["rate_date"].startswith(month)]
                self.assertAlmostEqual(float(r[f"{key}_value"]), sum(vals) / len(vals), places=9)
                self.assertEqual(int(r[f"{key}_n"]), len(vals))

    def test_live_mode_needs_the_zetaris_view_name(self):
        with h.env(SAMPLE_MODE=None):
            self.assertIsNone(schema.external_table_name(schema.load_schema()))
        with h.env(ZETARIS_FX_TABLE="fx.fx_rates_table"):
            self.assertIn("FROM fx.fx_rates_table", schema.external_sql(schema.load_schema(), "2026-08", "2026-09"))
        with h.env(ZETARIS_FX_TABLE="fx; DROP TABLE x"):
            with self.assertRaises(AppError):
                schema.external_table_name(schema.load_schema())


class TestAgainstIndependentGroundTruth(unittest.TestCase):
    """The SQL run through the sample store must equal sums computed with the csv module alone."""

    def setUp(self):
        self._env = h.env(SAMPLE_MODE="true")
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self.sch = schema.load_schema()
        self.rows = h.load_rows()

    def test_breakdown_equals_csv_truth(self):
        sql = schema.breakdown_sql(self.sch, "region", "gross_margin", "2026-08", "2026-09")
        got = {r["member"]: r for r in sample_store.run_sql(sql)}
        want = h.group_delta(self.rows, "region", "2026-08", "2026-09")
        self.assertEqual(set(got), set(want))
        for m, d in want.items():
            self.assertAlmostEqual(float(got[m]["compare_value"]) - float(got[m]["base_value"]), d, delta=0.05)

    def test_total_equals_csv_truth(self):
        r = sample_store.run_sql(schema.total_sql(self.sch, "gross_margin", "2026-08", "2026-09"))[0]
        self.assertAlmostEqual(float(r["base_value"]), h.margin(self.rows, "2026-08"), delta=0.05)
        self.assertAlmostEqual(float(r["compare_value"]), h.margin(self.rows, "2026-09"), delta=0.05)

    def test_sample_store_refuses_writes(self):
        with self.assertRaises(DataError):
            sample_store.run_sql("DELETE FROM sales_facts")


if __name__ == "__main__":
    unittest.main()
