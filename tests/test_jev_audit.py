"""Concurrent, metadata-only JSONL with original-vs-new accounting."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from ctn.jev import JevAuditLog, EvaluationLimits
from ctn.telemetry import JsonlWriter
from tests.test_jev_evaluator import evaluator, content


class AuditTests(unittest.TestCase):
    def test_post_logs_no_text_identifiers_key_or_raw(self):
        worker, _ = evaluator()
        data = content("with_context")
        flow = worker.evaluate(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                audit.record(flow)
            text = path.read_text()
            record = json.loads(text)
        for forbidden in (data["post"]["text"], data["post"]["post_id"], "synthetic-test-key", "raw_response", "legend", "probabilities"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(record["new_attempt_count"], 2)
        self.assertEqual(len(record["stages"]), 2)
        self.assertEqual(record["stages"][1]["original_usage"]["input_tokens"], 12)

    def test_cache_is_not_logged_as_new_usage_or_attempts(self):
        worker, _ = evaluator(limits=EvaluationLimits(cache_ttl_s=30))
        worker.evaluate(content())
        cached = worker.evaluate(content())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                audit.record(cached)
            record = json.loads(path.read_text())
        self.assertEqual(record["new_attempt_count"], 0)
        self.assertTrue(all(s["source"] == "cache" for s in record["stages"]))
        self.assertIsNotNone(record["stages"][0]["original_usage"])

    def test_deferred_failed_and_comparison_events(self):
        worker, _ = evaluator()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.jsonl"
            with JevAuditLog(path) as audit:
                audit.record(worker.evaluate({}))
                audit.record_comparison(worker.compare(content(post_id="a"), content(post_id="b")))
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[0]["status"], "deferred")
        self.assertEqual(len(records[1]["stage"]["targets"]), 2)

    def test_concurrent_writes_do_not_mutate_records_or_interleave_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            writer = JsonlWriter(path)
            inputs = [{"i": i, "payload": "汉字" * 1000} for i in range(100)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(writer.write, inputs))
            writer.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(records), 100)
        self.assertEqual({r["i"] for r in records}, set(range(100)))
        self.assertTrue(all("ts" not in item for item in inputs))

    def test_nonfinite_and_closed_writer_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = JsonlWriter(Path(directory) / "events.jsonl")
            with self.assertRaises(ValueError):
                writer.write({"value": float("nan")})
            writer.close()
            with self.assertRaises(ValueError):
                writer.write({"value": 1})
