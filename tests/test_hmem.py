"""H-MEM tests: the formulas, deltas, thresholds and never-happens rules the spec fully determines.
(The TS conformance runner cannot import the Python port, so conformance is NOT claimed.)"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engines import hmem as hm
from app.schema import METRIC_ALIASES


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.mem = hm.HMem(Path(self._tmp.name), clock=self.clock)

    def add(self, content="Gross margin fell in September because of Rain Jacket costs", **kw):
        kw.setdefault("source", "test")
        return self.mem.add(content, **kw)


class TestConstants(unittest.TestCase):
    def test_spec_numbers(self):
        self.assertEqual(hm.WEIGHTS, {"semantic": .35, "keyword": .20, "tag": .10, "domain": .10, "entity": .15,
                                      "layer": .05, "recency": .05, "superseded": .20})
        self.assertAlmostEqual(sum(v for k, v in hm.WEIGHTS.items() if k != "superseded"), 1.0)
        self.assertEqual(hm.RETRIEVAL_THRESHOLD, 0.35)
        self.assertEqual(hm.FEEDBACK_DELTAS, {"helpful": 0.05, "not_helpful": -0.03, "wrong": -0.20})
        self.assertEqual(len(hm.LEDGER_ACTIONS), 17)


class TestStore(Base):
    def test_source_is_mandatory_and_short_content_rejected(self):
        with self.assertRaises(ValueError):
            self.mem.add("some valid content here", source="")
        with self.assertRaises(ValueError):
            self.mem.add("short", source="x")
        with self.assertRaises(ValueError):
            self.mem.add("some valid content here", source="x", layer="forever")
        self.assertEqual(self.mem.all(), [])

    def test_persistence_roundtrip(self):
        m = self.add(tags=["Margin"], entities=["Rain Jacket", "rain jacket"])
        again = hm.HMem(self.mem.dir)
        self.assertEqual(again.get(m.id).content, m.content)
        self.assertEqual(again.get(m.id).entities, ["Rain Jacket"])      # case-insensitive de-dup
        self.assertEqual(again.get(m.id).tags, ["margin"])

    def test_supersede_keeps_loser_and_links_both_ways(self):
        a = self.add(replace_key="k1")
        b = self.add("Gross margin fell in September because of updated Rain Jacket costs", replace_key="k1")
        self.assertEqual(b.supersedes, a.id)
        self.assertEqual(self.mem.get(a.id).superseded_by, b.id)
        self.assertIsNotNone(self.mem.get(a.id))                          # never deleted


class TestRetrieval(Base):
    def test_unrelated_memory_not_returned_related_is(self):
        good = self.add(domain="gross_margin", tags=["gross_margin"])
        self.mem.add("The office plants are watered on Tuesdays by the facilities team", source="t", domain="facilities")
        r = self.mem.retrieve("Why did gross margin fall in September?", threshold=0.2, domain_aliases=METRIC_ALIASES)
        ids = [m["id"] for m in r["memories"]]
        self.assertIn(good.id, ids)
        self.assertEqual(len(ids), 1)
        self.assertIn("returned", r["trace"])

    def test_score_is_clamped_and_confidence_weighted(self):
        self.add(confidence=1.0, domain="gross_margin")
        low = self.add("Gross margin fell in September because of Rain Jacket costs (copy)", confidence=0.2, domain="gross_margin")
        r = self.mem.retrieve("gross margin fell September Rain Jacket costs", threshold=0.0)
        scores = {m["id"]: m["score"] for m in r["memories"]}
        for s in scores.values():
            self.assertTrue(0.0 <= s <= 1.0)
        self.assertLess(scores[low.id], max(scores.values()))

    def test_superseded_is_penalised(self):
        old = self.add(replace_key="k")
        new = self.add(replace_key="k")
        r = self.mem.retrieve("gross margin September Rain Jacket costs", threshold=0.0)
        s = {m["id"]: m["score"] for m in r["memories"]}
        self.assertLess(s[old.id], s[new.id])

    def test_top_five_cap_and_read_is_ledgered(self):
        for i in range(8):
            self.add(f"Gross margin fell in September because of Rain Jacket costs variant {i}")
        r = self.mem.retrieve("gross margin fell September Rain Jacket costs", threshold=0.0)
        self.assertEqual(len(r["memories"]), 5)
        self.assertTrue(any(e["action"] == "read" for e in self.mem.ledger()))
        self.assertIn("[general]", r["context"])


class TestFeedback(Base):
    def test_deltas_exact(self):
        m = self.add(confidence=0.5)
        self.assertAlmostEqual(self.mem.feedback(m.id, "helpful").confidence, 0.55)
        self.assertEqual(self.mem.get(m.id).access_count, 1)
        self.assertAlmostEqual(self.mem.feedback(m.id, "not_helpful").confidence, 0.52)
        self.assertAlmostEqual(self.mem.feedback(m.id, "wrong").confidence, 0.32)
        self.assertIn("review", self.mem.get(m.id).tags)

    def test_clamped_and_validated(self):
        m = self.add(confidence=0.99)
        self.assertEqual(self.mem.feedback(m.id, "helpful").confidence, 1.0)
        with self.assertRaises(ValueError):
            self.mem.feedback(m.id, "great")
        with self.assertRaises(KeyError):
            self.mem.feedback("mem-nope", "helpful")


class TestLedger(Base):
    def test_actions_are_canonical_and_chain_verifies(self):
        m = self.add()
        self.mem.feedback(m.id, "helpful")
        self.mem.retrieve("gross margin", threshold=0.0)
        led = self.mem.ledger()
        self.assertTrue(all(e["action"] in hm.LEDGER_ACTIONS for e in led))
        self.assertTrue(self.mem.verify_ledger()["ok"])

    def test_tampering_is_detected(self):
        self.add()
        self.add("Another gross margin memory about costs")
        lines = self.mem.ledger_path.read_text().splitlines()
        row = json.loads(lines[0])
        row["actor"] = "mallory"
        lines[0] = json.dumps(row)
        self.mem.ledger_path.write_text("\n".join(lines) + "\n")
        self.assertFalse(self.mem.verify_ledger()["ok"])


class TestSleep(Base):
    def test_preview_changes_nothing_execute_backs_up_and_restore_works(self):
        old = self.add("Old unused memory about Widget returns last quarter")
        self.clock.t += timedelta(days=45)
        fresh = self.add("A fresh memory about the Rain Jacket cost shock")
        plan = self.mem.sleep_preview()
        self.assertEqual(plan["toArchive"], [old.id])
        self.assertEqual(len(self.mem.all()), 2)                          # preview is read-only
        done = self.mem.sleep_execute()
        self.assertEqual(done["archived"], 1)
        self.assertIsNone(self.mem.get(old.id))
        self.assertIsNotNone(self.mem.get(fresh.id))
        self.assertTrue((self.mem.backup_dir / f"{done['backup_id']}.json").exists())
        self.assertEqual(self.mem.restore(done["backup_id"]), 2)
        self.assertIsNotNone(self.mem.get(old.id))

    def test_superseding_memory_is_guarded_from_archive(self):
        a = self.add(replace_key="k")
        b = self.add("Gross margin fell because of Rain Jacket cost update", replace_key="k")
        self.clock.t += timedelta(days=60)
        archive = self.mem.sleep_preview()["toArchive"]
        self.assertNotIn(b.id, archive)       # supersedes link guard: the winner of a supersede is never archived
        self.assertNotIn(a.id, archive)       # referenced by the winner


if __name__ == "__main__":
    unittest.main()
