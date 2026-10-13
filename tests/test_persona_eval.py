"""Small integration tests; run: python -B tests/test_persona_eval.py."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import persona_eval as ev


class PersonaEvaluationTests(unittest.TestCase):
    def test_splits_do_not_open_each_other(self):
        actual = ev.read_json
        with patch.object(ev, "read_json", wraps=actual) as reader:
            dev = ev.load_suite()
            self.assertEqual(len(dev["cases"]), 16)
            self.assertTrue(all("holdout" not in str(c.args[0]) for c in reader.call_args_list))
        holdout = ev.load_suite("holdout")
        self.assertEqual(len(holdout["cases"]), 8)
        self.assertNotEqual(dev["profile"]["speaker"], holdout["profile"]["speaker"])
        self.assertFalse({c["query"] for c in dev["cases"]} & {c["query"] for c in holdout["cases"]})

    def test_real_orchestration_threshold_cache_history_and_parameters(self):
        suite = ev.load_suite()
        for case in (suite["cases"][6], suite["cases"][13], suite["cases"][15]):
            for inject in (False, True):
                class Delegate:
                    def chat(self, messages, **kwargs):
                        self.kwargs = kwargs
                        return "测试回复"
                delegate = Delegate()
                row = ev.evaluate_case(suite, case, inject, delegate, 0.17, 213)
                self.assertTrue(all(row["checks"].values()), row["checks"])
                self.assertEqual(delegate.kwargs, {"temperature": 0.17, "max_tokens": 213})
                self.assertTrue(all(v is None for v in row["human_scores"].values()))
                if case["id"] == "dev-14":
                    self.assertEqual(len(row["citations"]), 1 if inject else 0)
                    self.assertNotIn("凌晨一点", row["messages"][-1]["content"])

    def test_blind_scoring_partial_coverage_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="persona-eval-test-") as tmp:
            base = Path(tmp)
            ev.main(["run", "--output", str(base / "run")])
            ev.main(["blind", "--runs", str(base / "run" / "run.json"), "--output", str(base / "blind")])
            sheet = ev.read_json(base / "blind" / "blind.json")
            self.assertNotIn("policy", sheet["cards"][0])
            sheet["cards"][0]["scores"]["贴题"] = 2
            ev.save(base, "filled.json", sheet)
            argv = ["score", "--sheet", str(base / "filled.json"), "--key", str(base / "blind" / "private-key.json"), "--output", str(base / "scored")]
            self.assertEqual(ev.main(argv), 0)
            summary = ev.read_json(base / "scored" / "scores.json")["summary"]
            self.assertEqual(sum(s["scored"] for s in summary), 1)
            self.assertTrue(all(s["mean"] is None for s in summary if s["dimension"] != "贴题"))
            sheet["cards"][0]["reply"] = "tampered"
            ev.save(base, "filled.json", sheet)
            args = argparse.Namespace(sheet=str(base / "filled.json"), key=str(base / "blind" / "private-key.json"), output=str(base / "bad"))
            with self.assertRaisesRegex(ValueError, "content/order"):
                ev.score(args)
            self.assertFalse((base / "bad").exists())

    def test_cli_generation_parameters_match_metadata(self):
        class Delegate:
            def __init__(self):
                self.parameters = []

            def chat(self, messages, **kwargs):
                self.parameters.append(kwargs)
                return "本地测试，不联网"

        delegate = Delegate()
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="persona-eval-cli-") as tmp:
            out = Path(tmp) / "run"
            with patch.object(ev, "make_llm", return_value=delegate):
                status = ev.main(["run", "--mode", "llm", "--provider", "openai", "--model", "fake",
                                  "--base-url", "http://invalid.local/v1", "--temperature", "0.17",
                                  "--max-tokens", "213", "--output", str(out)])
            self.assertEqual(status, 0)
            self.assertEqual(len(delegate.parameters), 32)
            self.assertTrue(all(p == {"temperature": 0.17, "max_tokens": 213} for p in delegate.parameters))
            metadata = ev.read_json(out / "run.json")["generation"]
            self.assertEqual(metadata["temperature"], 0.17)
            self.assertEqual(metadata["max_tokens"], 213)

    def test_output_guard(self):
        for path in (ROOT / ".chatbot" / "eval", ROOT.parent / "outside-persona-eval", ROOT / "tools"):
            with self.assertRaises(ValueError):
                ev.output_dir(path)


if __name__ == "__main__":
    unittest.main()
