"""Performance acceptance boundaries with real snapshots and controlled measurements."""
import copy
import hashlib
import json
import statistics
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repair_workbench.candidate import create_candidate
from repair_workbench.performance import verify_performance_candidate
from repair_workbench.snapshot import create_snapshot, snapshot_id, verify_snapshot


IMAGE = "sha256:" + "a" * 64
ENVIRONMENT = {
    "python_version": "3.13.0", "python_implementation": "CPython",
    "platform_system": "Linux", "machine": "aarch64", "clock": "perf_counter_ns",
    "isolation": {"cpus": 1, "memory_bytes": 268435456},
}


class PerformanceVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        (self.repo / "exporter.py").write_text("def export():\n    return 1\n")
        (self.repo / "tests").mkdir()
        self.original_test = "from exporter import export\ndef test_export():\n    assert export() == 1\n"
        (self.repo / "tests/test_export.py").write_text(self.original_test)
        for arguments in (("init", "-q"), ("add", "--all"), ("commit", "-qm", "fixture")):
            subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-C", str(self.repo), *arguments], check=True, capture_output=True, timeout=30,
            )
        self.baseline, self.candidate, self.destination = (self.root / name for name in ("baseline", "candidate", "verified"))
        create_snapshot(self.repo, self.baseline)
        self._write_tests(self.baseline)
        create_candidate(self.baseline, self.candidate,
                         {"exporter.py": "def export():\n    value = 1\n    return value\n"}, {"exporter.py"})
        self.probe_calls = []
        self.test_mutator = None
        self.probe_mutator = None

    def _write_tests(self, snapshot, name="test_export", status="tests_passed"):
        reports = snapshot / "reports"
        reports.mkdir()
        failure = "<failure/>" if status == "tests_failed" else ""
        (reports / "pytest.xml").write_text(
            f'<testsuite><testcase classname="tests.test_export" name="{name}">{failure}</testcase></testsuite>'
        )
        result = {
            "status": status, "cleanup_ok": True, "image_id": IMAGE,
            "snapshot_id": snapshot_id(verify_snapshot(snapshot)),
            "counts": {"total": 1, "passed": int(not failure), "failed": int(bool(failure)), "errors": 0, "skipped": 0},
        }
        (reports / "result.json").write_text(json.dumps(result))
        return result

    def _tests(self, snapshot, image, timeout, **kwargs):
        self.assertEqual(image, IMAGE)
        # Submitted reports are never reused, and original test bytes survive rebuild.
        self.assertEqual((snapshot / "workspace/tests/test_export.py").read_text(), self.original_test)
        self.assertFalse((snapshot / "reports").exists())
        result = self._write_tests(snapshot)
        if self.test_mutator:
            self.test_mutator(result, snapshot)
        return result

    def _probe(self, snapshot, output, image, probe, workload, timeout, **kwargs):
        role = "baseline" if snapshot == self.baseline else "candidate"
        self.probe_calls.append((role, probe, output))
        self.assertEqual(image, IMAGE)
        self.assertEqual(workload, "ugc-export-v1")
        self.assertNotEqual(snapshot, self.candidate)
        self.assertNotIn(snapshot, output.parents)
        scales, samples = {}, {}
        for scale, objects, resources in (("small", 300, 80), ("large", 6000, 600)):
            base = .01 if scale == "small" else 1.0
            sample = base if role == "baseline" else base * .7
            values = [sample * factor for factor in (.99, 1.0, 1.01, 1.0, .99, 1.01, 1.0)]
            value = {"semantic_ok": True, "fingerprint": hashlib.sha256(scale.encode()).hexdigest(),
                     "object_count": objects, "resource_count": resources}
            if probe == "benchmark":
                samples[scale] = values
                value["median_seconds"] = statistics.median(values)
            else:
                samples[scale] = [1000 if role == "baseline" else 1100]
                value["peak_traced_bytes"] = samples[scale][0]
            scales[scale] = value
        report = {
            "status": "ok", "cleanup_ok": True, "snapshot_id": snapshot_id(verify_snapshot(snapshot)),
            "image_id": image, "workload_id": workload, "probe": probe,
            "environment": copy.deepcopy(ENVIRONMENT), "harness_sha256": "b" * 64,
            "workload_fingerprint": "c" * 64, "summary": {"semantic_ok": True, "scales": scales},
            "samples": samples,
        }
        if self.probe_mutator:
            self.probe_mutator(report, role, probe, len(self.probe_calls))
        output.mkdir(parents=True)
        (output / "result.json").write_text(json.dumps(report))
        return report

    def verify(self, **kwargs):
        with patch("repair_workbench.performance.run_tests", side_effect=self._tests) as tests:
            with patch("repair_workbench.performance.run_probe", side_effect=self._probe) as probes:
                result = verify_performance_candidate(
                    self.baseline, self.candidate, self.destination, {"exporter.py"}, **kwargs
                )
        self.assertEqual(json.loads((self.destination / "performance-verification.json").read_text()), result)
        return result, tests, probes

    @staticmethod
    def _time(report, scale, factor):
        report["samples"][scale] = [value * factor for value in report["samples"][scale]]
        report["summary"]["scales"][scale]["median_seconds"] = statistics.median(report["samples"][scale])

    def test_rebuilds_independently_and_preserves_all_abba_samples(self):
        forged = self.candidate / "reports"
        forged.mkdir()
        (forged / "pytest.xml").write_text("forged candidate test report")
        before = (self.baseline / "reports/result.json").read_bytes()
        control = object()
        result, tests, probes = self.verify(execution_control=control)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["measurements"]), 8)
        self.assertEqual(tests.call_args.kwargs["execution_control"], control)
        self.assertTrue(all(call.kwargs["execution_control"] is control for call in probes.call_args_list))
        self.assertEqual(before, (self.baseline / "reports/result.json").read_bytes())
        self.assertEqual((forged / "pytest.xml").read_text(), "forged candidate test report")
        for probe in ("benchmark", "memory"):
            self.assertEqual([role for role, kind, _ in self.probe_calls if kind == probe],
                             ["baseline", "candidate", "candidate", "baseline"])
        for scale in ("small", "large"):
            self.assertEqual(len(result["metrics"][scale]["samples_seconds"]["baseline"]), 14)
            self.assertEqual(result["metrics"][scale]["memory_samples_bytes"]["candidate"], [1100, 1100])
        self.assertFalse(result["protocol"]["profiler_enabled_for_timing"])

    def test_baseline_requires_matching_passed_tests_counts_snapshot_and_immutable_image(self):
        path = self.baseline / "reports/result.json"
        original = json.loads(path.read_text())
        corruptions = (
            {"status": "tests_failed"}, {"cleanup_ok": False}, {"snapshot_id": "wrong"},
            {"image_id": "python:latest"}, {"counts": {"total": 1, "passed": 0}},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                path.write_text(json.dumps({**original, **corruption}))
                with patch("repair_workbench.performance.run_tests") as tests:
                    with self.assertRaises(ValueError):
                        verify_performance_candidate(self.baseline, self.candidate, self.destination, {"exporter.py"})
                tests.assert_not_called()
                self.assertFalse(self.destination.exists())

    def test_original_test_identity_cannot_be_replaced_by_equal_count(self):
        def change(result, snapshot):
            (snapshot / "reports/pytest.xml").write_text('<testsuite><testcase classname="tests.test_export" name="replacement"/></testsuite>')
        self.test_mutator = change
        result, _, probes = self.verify()
        self.assertEqual((result["status"], result["reason"]), ("failed", "test_set_changed"))
        probes.assert_not_called()

    def test_original_semantic_failure_stops_performance_measurement(self):
        def change(result, snapshot):
            result["status"] = "tests_failed"
            (snapshot / "reports/pytest.xml").write_text('<testsuite><testcase classname="tests.test_export" name="test_export"><failure/></testcase></testsuite>')
        self.test_mutator = change
        result, _, probes = self.verify()
        self.assertEqual(result["reason"], "candidate_semantics_failed")
        probes.assert_not_called()

    def test_unusable_original_test_report_is_inconclusive(self):
        self.test_mutator = lambda result, snapshot: (snapshot / "reports/pytest.xml").write_text("<broken")
        result, _, probes = self.verify()
        self.assertEqual(result["reason"], "unusable_candidate_test_report")
        probes.assert_not_called()

    def test_candidate_cleanup_and_test_report_mismatch_are_inconclusive(self):
        for index, mutation in enumerate(({"cleanup_ok": False}, {"image_id": "sha256:" + "b" * 64},
                                          {"counts": {"total": 1, "passed": 100}})):
            with self.subTest(mutation=mutation):
                self.destination = self.root / f"bad-tests-{index}"
                self.test_mutator = lambda result, snapshot: result.update(mutation)
                result, _, probes = self.verify()
                self.assertEqual(result["status"], "inconclusive")
                probes.assert_not_called()

    def test_candidate_does_not_change_tests_even_with_forged_metadata(self):
        protected = self.candidate / "workspace/tests/test_export.py"
        protected.write_text("def test_export():\n    pass\n")
        path = self.candidate / "manifest.json"
        manifest = json.loads(path.read_text())
        for item in manifest["files"]:
            if item["path"] == "tests/test_export.py":
                item["sha256"] = hashlib.sha256(protected.read_bytes()).hexdigest()
        manifest["changed_paths"] = ["exporter.py"]
        path.write_text(json.dumps(manifest))
        with patch("repair_workbench.performance.run_tests") as tests:
            with self.assertRaises(ValueError):
                verify_performance_candidate(self.baseline, self.candidate, self.destination,
                                             {"exporter.py", "tests/test_export.py"})
        tests.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_probe_semantics_cannot_be_compensated_by_faster_timing(self):
        def change(report, role, probe, number):
            if role == "candidate":
                report["summary"]["semantic_ok"] = False
        self.probe_mutator = change
        result, _, _ = self.verify()
        self.assertEqual((result["status"], result["reason"]), ("failed", "semantic_mismatch"))

    def test_each_scale_output_fingerprint_must_match_across_measurements(self):
        def change(report, role, probe, number):
            if role == "candidate":
                report["summary"]["scales"]["small"]["fingerprint"] = "d" * 64
        self.probe_mutator = change
        result, _, _ = self.verify()
        self.assertEqual(result["reason"], "semantic_fingerprint_changed")

    def test_environment_image_workload_and_harness_mixing_is_inconclusive(self):
        changes = (
            lambda r: r.update(image_id="sha256:" + "d" * 64),
            lambda r: r["environment"].update(machine="x86_64"),
            lambda r: r.update(workload_fingerprint="d" * 64),
            lambda r: r.update(harness_sha256="d" * 64),
            lambda r: r.update(snapshot_id="d" * 64),
            lambda r: r["summary"]["scales"]["large"].update(object_count=300),
        )
        for index, mutation in enumerate(changes):
            with self.subTest(index=index):
                self.destination = self.root / f"mixed-{index}"
                self.probe_calls = []
                self.probe_mutator = lambda report, role, probe, number: mutation(report) if role == "candidate" else None
                result, _, _ = self.verify()
                self.assertEqual(result["status"], "inconclusive")

    def test_large_gain_does_not_hide_small_workload_regression(self):
        def change(report, role, probe, number):
            if role == "candidate" and probe == "benchmark":
                self._time(report, "small", 2)
        self.probe_mutator = change
        result, _, _ = self.verify()
        self.assertEqual(result["reason"], "small_workload_regression")

    def test_gain_below_preregistered_threshold_is_failed(self):
        def change(report, role, probe, number):
            if role == "candidate" and probe == "benchmark":
                self._time(report, "large", .90 / .7)
        self.probe_mutator = change
        result, _, _ = self.verify()
        self.assertEqual((result["status"], result["reason"]), ("failed", "minimum_speedup_not_met"))

    def test_memory_uses_worst_candidate_against_lowest_baseline(self):
        def change(report, role, probe, number):
            if role == "candidate" and probe == "memory":
                report["samples"]["large"] = [1300 if number > 4 else 1100]
                report["summary"]["scales"]["large"]["peak_traced_bytes"] = report["samples"]["large"][0]
        self.probe_mutator = change
        result, _, _ = self.verify()
        self.assertEqual(result["reason"], "memory_regression")
        self.assertEqual(result["metrics"]["large"]["memory_samples_bytes"]["candidate"], [1100, 1300])

    def test_noise_and_order_drift_make_result_inconclusive(self):
        def change(report, role, probe, number):
            if role == "baseline" and probe == "benchmark" and number > 4:
                self._time(report, "large", 2)
        self.probe_mutator = change
        result, _, _ = self.verify()
        self.assertEqual((result["status"], result["reason"]), ("inconclusive", "measurement_noise_exceeded"))
        self.assertEqual(len(result["measurements"]), 8)

    def test_invalid_samples_and_inconsistent_summary_cannot_pass(self):
        mutations = (
            lambda r: r["samples"]["large"].pop(),
            lambda r: r["samples"]["large"].__setitem__(0, 0),
            lambda r: r["samples"]["large"].__setitem__(0, float("nan")),
            lambda r: r["summary"]["scales"]["large"].update(median_seconds=.00001),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.destination = self.root / f"bad-samples-{index}"
                self.probe_calls = []
                self.probe_mutator = lambda report, role, probe, number: mutation(report) if role == "candidate" and probe == "benchmark" else None
                # Non-finite values are represented as strings in saved evidence.
                with patch("repair_workbench.performance.run_tests", side_effect=self._tests):
                    with patch("repair_workbench.performance.run_probe", side_effect=self._probe):
                        result = verify_performance_candidate(self.baseline, self.candidate, self.destination, {"exporter.py"})
                self.assertEqual(result["status"], "inconclusive")
                saved = json.loads((self.destination / "performance-verification.json").read_text())
                self.assertEqual(saved["status"], "inconclusive")

    def test_timeout_or_cleanup_problem_preserves_partial_report_and_stops(self):
        for index, mutation in enumerate(({"status": "timeout"}, {"cleanup_ok": False})):
            with self.subTest(mutation=mutation):
                self.destination = self.root / f"interrupted-{index}"
                self.probe_calls = []
                self.probe_mutator = lambda report, role, probe, number: report.update(mutation)
                result, _, probes = self.verify()
                self.assertEqual(result["status"], "inconclusive")
                self.assertEqual(probes.call_count, 1)
                self.assertEqual(len(result["measurements"]), 1)

    def test_invalid_thresholds_and_unregistered_workload_fail_before_execution(self):
        invalid = ({"minimum_speedup": 0}, {"minimum_speedup": 1}, {"minimum_speedup": True},
                   {"minimum_speedup": float("nan")}, {"max_memory_ratio": .9},
                   {"max_memory_ratio": float("inf")}, {"timeout_seconds": 0},
                   {"timeout_seconds": True}, {"timeout_seconds": 121}, {"workload": "agent-proposed-script"})
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with patch("repair_workbench.performance.run_tests") as tests:
                    with self.assertRaises(ValueError):
                        verify_performance_candidate(self.baseline, self.candidate, self.destination,
                                                     {"exporter.py"}, **arguments)
                tests.assert_not_called()
                self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
