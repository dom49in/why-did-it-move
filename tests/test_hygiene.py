"""Submission hygiene: no secrets, no hardcoded outputs, required files, examples are genuine runs."""
import json
import re
import unittest

from tests.helpers import ROOT

SKIP_DIRS = {".git", "__pycache__", ".memory", "logs", ".venv", "node_modules"}


def files(*suffixes):
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and (not suffixes or p.suffix in suffixes):
            yield p


class TestHygiene(unittest.TestCase):
    def test_required_files_exist(self):
        for name in ("Dockerfile", "README.md", ".env.example", "requirements.txt", "run.py", "SUBMISSION.md", ".gitignore"):
            self.assertTrue((ROOT / name).exists(), name)
        self.assertGreaterEqual(len(list((ROOT / "input_examples").glob("*.json"))), 3)
        self.assertGreaterEqual(len(list((ROOT / "output_examples").glob("*.json"))), 3)

    def test_no_secret_shaped_strings(self):
        pats = [re.compile(r"nvapi-[A-Za-z0-9_\-]{10,}"), re.compile(r"sk-[A-Za-z0-9]{20,}"),
                re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"ghp_[A-Za-z0-9]{20,}"),
                re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]
        for p in files():
            if p.suffix in {".pyc", ".docx", ".pptx", ".png", ".mp4"}:
                continue
            text = p.read_text(errors="ignore")
            for pat in pats:
                self.assertIsNone(pat.search(text), f"{p}: {pat.pattern}")

    def test_env_example_has_no_values_for_secrets_and_no_real_env_file(self):
        self.assertFalse((ROOT / ".env").exists())
        text = (ROOT / ".env.example").read_text()
        for key in ("METERLESS_RUNTIME_TOKEN", "ZETARIS_API_KEY"):
            m = re.search(rf"^{key}=(.*)$", text, re.M)
            self.assertIsNotNone(m, key)
            self.assertEqual(m.group(1).strip(), "", key)
        self.assertNotRegex(text, r"(?m)^\s*NVIDIA", "the NVIDIA key lives only in Nightshift's Providers tab")
        self.assertIn(".env", (ROOT / ".gitignore").read_text())

    def test_nvidia_key_is_never_read_by_the_app(self):
        for p in files(".py"):
            if "tests" in p.parts:
                continue
            self.assertNotIn("NVIDIA_API_KEY", p.read_text(), p)

    def test_tls_and_proxy_untouched(self):
        for p in files(".py"):
            text = p.read_text()
            self.assertNotRegex(text, r"verify\s*=\s*False", str(p))
            self.assertNotIn("HTTPS_PROXY", text if "tests" not in p.parts else "", str(p))

    def test_application_code_has_no_embedded_answers(self):
        """Business facts from the sample data must not appear in app/ source: answers come from queries."""
        for p in (ROOT / "app").rglob("*.py"):
            text = p.read_text()
            for needle in ("Rain Jacket", "56693", "676566", "619872"):
                self.assertNotIn(needle, text, f"{p} embeds {needle!r}")

    def test_example_files_are_never_read_at_runtime(self):
        for p in (ROOT / "app").rglob("*.py"):
            self.assertNotIn("output_examples", p.read_text(), p)
        self.assertNotIn("output_examples", (ROOT / "run.py").read_text().replace("--output", ""), "run.py")

    def test_output_examples_are_complete_real_run_records(self):
        for p in sorted((ROOT / "output_examples").glob("*.json")):
            d = json.loads(p.read_text())
            self.assertEqual(d["status"], "success", p.name)
            self.assertEqual(d["data_source"], "sample_data", p.name)         # labelled honestly as a sample-mode run
            self.assertEqual(d["models_used"], ["offline-rules"], p.name)
            self.assertTrue(d["collaboration"]["feedback_loop_completed"], p.name)
            self.assertTrue(d["result"]["findings"], p.name)
            log = ROOT / "output_examples" / "logs" / (d["trace_id"] + ".jsonl")
            self.assertTrue(log.exists(), f"missing log for {p.name}")


if __name__ == "__main__":
    unittest.main()
