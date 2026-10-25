import asyncio
import json
import unittest

import run
from tests import helpers as h


class TestHandle(unittest.TestCase):
    def setUp(self):
        t = h.tempdir()
        d = t.__enter__()
        self.addCleanup(t.__exit__, None, None, None)
        e = h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l"))
        e.__enter__()
        self.addCleanup(e.__exit__, None, None, None)
        run._memory = None

    def post(self, path, payload):
        return run.handle("POST", path, json.dumps(payload).encode())

    def test_health(self):
        status, body = run.handle("GET", "/health", b"")
        self.assertEqual((status, body["status"], body["sample_mode"], body["reasoning_mode"]), (200, "ok", True, "offline_rules"))

    def test_run_endpoint_matches_cli_contract(self):
        status, body = self.post("/run", json.load(open(h.ROOT / "input_examples" / "example_1.json")))
        self.assertEqual(status, 200)
        self.assertEqual(body["request_id"], "example-001")
        self.assertEqual(body["status"], "success")

    def test_bad_json_and_bad_input_and_unknown_route(self):
        self.assertEqual(run.handle("POST", "/run", b"{nope")[0], 400)
        self.assertEqual(self.post("/run", {"user_query": ""})[0], 422)
        self.assertEqual(run.handle("GET", "/nope", b"")[0], 404)
        self.assertEqual(run.handle("DELETE", "/run", b"")[0], 404)

    def test_memory_endpoints_feedback_and_sleep(self):
        self.post("/run", {"user_query": "Why did gross margin fall in September versus August?"})
        status, mem = run.handle("GET", "/memory", b"")
        self.assertTrue(mem["enabled"] and mem["memories"] and mem["ledger"]["ok"])
        mid = mem["memories"][0]["id"]
        before = mem["memories"][0]["confidence"]
        status, fb = self.post("/memory/feedback", {"memory_id": mid, "signal": "wrong"})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(fb["memory"]["confidence"], max(0.0, before - 0.2), places=6)
        self.assertEqual(self.post("/memory/feedback", {"memory_id": "mem-none", "signal": "helpful"})[0], 404)
        self.assertEqual(self.post("/memory/feedback", {"memory_id": mid, "signal": "meh"})[0], 422)
        status, prev = self.post("/memory/sleep", {})
        self.assertIn("toArchive", prev["preview"])
        status, done = self.post("/memory/sleep", {"apply": True})
        self.assertIn("backup_id", done["executed"])

    def test_engine_stats(self):
        self.post("/run", {"user_query": "Why did gross margin fall in September versus August?"})
        status, s = run.handle("GET", "/engine/markovian", b"")
        self.assertEqual((status, s["totalRuns"]), (200, 1))

    def test_asgi_roundtrip(self):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b'{"user_query": "Why did gross margin fall in September versus August?"}', "more_body": False}

        async def send(msg):
            sent.append(msg)

        asyncio.run(run.app({"type": "http", "method": "POST", "path": "/run"}, receive, send))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(json.loads(sent[1]["body"])["status"], "success")


class TestCli(unittest.TestCase):
    def test_cli_writes_response_file_and_stdout_logs(self):
        import subprocess, sys
        with h.tempdir() as d:
            envv = {"PATH": "/usr/bin:/bin:/usr/local/bin", "SAMPLE_MODE": "true", "MEMORY_DIR": str(d / "m"), "LOG_DIR": str(d / "l")}
            p = subprocess.run([sys.executable, "run.py", "--input", "input_examples/example_3.json", "--output", str(d / "o.json")],
                               cwd=h.ROOT, env=envv, capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.load(open(d / "o.json"))
            self.assertEqual(out["status"], "success")
            jsonl = [l for l in p.stdout.splitlines() if l.startswith('{"timestamp"')]
            self.assertGreater(len(jsonl), 15)                           # logs reach stdout as well as logs/
            self.assertEqual(len(list((d / "l").glob("*.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()


class TestUi(unittest.TestCase):
    def test_root_serves_the_page_and_it_uses_only_same_origin_calls(self):
        import asyncio, run
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        asyncio.run(run.app({"type": "http", "method": "GET", "path": "/"}, receive, send))
        self.assertEqual(sent[0]["status"], 200)
        html = sent[1]["body"].decode()
        self.assertIn("Why did it move?", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", html)
