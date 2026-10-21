"""Measurement contracts; no MySQL, Docker or model service required."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_benchmark_support import (
    percentile,
    distribution,
    check_cell,
    Observation,
    CursorObserver,
)
from benchmark_backend_reads import payload_hash


class BenchmarkMetricTests(unittest.TestCase):
    def test_nearest_rank_and_empty_samples(self):
        self.assertIsNone(percentile([], 0.95))
        self.assertEqual(percentile(list(range(1, 21)), 0.95), 19)
        self.assertEqual(distribution([3, 1, 2])["mean"], 2)
        self.assertEqual(distribution([])["n"], 0)
        with self.assertRaises(ValueError):
            percentile([1], 0)

    def test_failed_or_duplicate_tasks_cannot_be_counted_as_throughput(self):
        good = {
            "id": "a",
            "state": "completed",
            "generation": 1,
            "calls_reserved": 3,
            "candidates_reserved": 1,
            "result": {"agent": {"status": "passed"}},
        }
        check_cell([good], 1, 1)
        for bad in (
            {**good, "state": "error"},
            {**good, "generation": 2},
            {**good, "calls_reserved": 4},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_cell([bad], 1, 1)
        with self.assertRaises(ValueError):
            check_cell([good, good], 2, 2)
        with self.assertRaises(ValueError):
            check_cell([], 1, 1)

    def test_sql_observer_records_error_but_never_parameters(self):
        class Cursor:
            def execute(self, sql, args=None):
                raise ValueError("simulated")

        observer = Observation()
        with self.assertRaises(ValueError):
            CursorObserver(Cursor(), observer).execute(
                "SELECT secret FROM x", ("private",)
            )
        snapshot = observer.snapshot()
        self.assertEqual(snapshot["queries"], 1)
        self.assertEqual(snapshot["query_kinds"], {"SELECT": 1})
        self.assertNotIn("private", str(snapshot))

    def test_read_probe_hash_is_order_independent_for_fields_but_not_rows(self):
        self.assertEqual(payload_hash({"a": 1, "b": 2}), payload_hash({"b": 2, "a": 1}))
        self.assertNotEqual(payload_hash([1, 2]), payload_hash([2, 1]))
