"""Synthetic tests for evaluation and opt-in observability."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chatbot.config import Config, load_config
from qq_chatbot.eval.runner import evaluate, demo_retriever, load_cases, compare, evaluate_reviews
from qq_chatbot.observability import TraceLog
from qq_chatbot.schema import Answer
from qq_chatbot.serve import create_handler


class EvalTests(unittest.TestCase):
    def test_demo_and_metrics(self):
        retriever = demo_retriever(Config())
        cases = load_cases(Path(__file__).parent / "fixtures/retrieval_cases.json")
        report = evaluate(retriever, cases, k=1)
        self.assertEqual(report["metrics"], {"hit_at_k": 1., "recall_at_k": 1., "mrr": 1.})
        self.assertNotIn("query", json.dumps(report))
        self.assertEqual(compare(report, report)["mrr"], 0)
        with self.assertRaises(ValueError):
            evaluate(retriever, [], k=1)

    def test_partial_and_rank(self):
        retriever = demo_retriever(Config())
        report = evaluate(retriever, [{"query": "项目会议", "gold_turn_ids": ["demo-meeting", "missing"]}], k=1)
        self.assertEqual(report["metrics"]["recall_at_k"], .5)
        self.assertEqual(report["metrics"]["mrr"], 1)

    def test_boolean_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"observability": {"enabled": "false", "include_content": "false"},
                                        "embedding": {"fallback_to_hash": "false"}}), encoding="utf-8")
            cfg = load_config(path)
            self.assertFalse(cfg.observability.enabled)
            self.assertFalse(cfg.observability.include_content)
            self.assertFalse(cfg.embedding.fallback_to_hash)
            path.write_text('{"memory": {"enabled": "maybe"}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_human_reviews(self):
        report = evaluate_reviews([{"case": "synthetic-a", "style": 1, "factuality": .5, "timeline": 0}])
        self.assertEqual(report["means"]["factuality"], .5)
        self.assertEqual(report["source"], "human_annotations")
        for value in (float("nan"), True, "1", 2):
            with self.assertRaises(ValueError):
                evaluate_reviews([{"case": "a", "style": value, "factuality": 1, "timeline": 1}])

    def test_trace_redaction_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TraceLog(Path(tmp) / "trace.jsonl", max_bytes=1024)
            found = demo_retriever(Config()).retrieve("项目会议")
            for _ in range(8):
                log.record("PRIVATE_QUERY", "qa", found, Answer("PRIVATE_ANSWER"), 1.)
            text = log.path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn("group:100", text)
            self.assertNotIn("项目会议", text)
            self.assertTrue(log.path.with_suffix(".previous.jsonl").exists())
            self.assertTrue(log.recent())

    def test_trace_corrupt_tail_and_multiple_writers(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text('{"id":"valid"}\n{"id":', encoding="utf-8")
            log = TraceLog(path)
            self.assertEqual(log.recent(), [{"id": "valid"}])
            new_id = log.record("q", "qa", [], Answer("a"), 1)
            self.assertEqual(log.recent()[-1]["id"], new_id)
            path.write_text('', encoding="utf-8")
            def write(i):
                TraceLog(path).record("query", "qa", [], Answer("answer"), i)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(20)))
            self.assertEqual(len(log.recent()), 20)
            self.assertEqual(len({r["id"] for r in log.recent()}), 20)

    def test_admin_auth_and_validation(self):
        cfg = Config()
        cfg.observability.admin_token = "synthetic-test-token"
        bot = SimpleNamespace(config=cfg, retriever=demo_retriever(cfg), traces=None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(bot))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/admin/retrieval"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            def request(payload):
                return urllib.request.urlopen(urllib.request.Request(url, json.dumps(payload).encode(),
                    {"Authorization": "Bearer synthetic-test-token"}))
            with request({"top_k": 3}) as response:
                self.assertFalse(json.load(response)["persisted"])
            self.assertEqual(bot.retriever.config.top_k, 3)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request({"vector_weight": 2})
            error.exception.close()
            self.assertEqual(bot.retriever.config.vector_weight, .7)
            from concurrent.futures import ThreadPoolExecutor
            def change(payload):
                with request(payload) as response:
                    return json.load(response)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(change, [{"top_k": 9}, {"vector_weight": .2}]))
            self.assertEqual(bot.config.retrieval.top_k, 9)
            self.assertEqual(bot.config.retrieval.vector_weight, .2)
            self.assertIs(bot.config.retrieval, bot.retriever.config)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
