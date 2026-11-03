"""Diagnostic registration, evidence validation and container lifecycle contracts."""
import copy
import dataclasses
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repair_workbench import diagnostics, probe_worker
from repair_workbench.snapshot import create_snapshot


IMAGE = "sha256:" + "a" * 64


def correct_export(objects, resources):
    first = {}
    for resource in resources:
        first.setdefault(resource["id"], resource["path"])
    output = []
    for item in objects:
        if item["resource_id"] not in first:
            raise ValueError("missing resource")
        output.append({"object_id": item["id"], "resource_path": first[item["resource_id"]]})
    return output


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        # This must never execute during a host-side run_probe call.
        (self.repo / "map_exporter.py").write_text("raise RuntimeError('host source executed')\n")
        for args in (("init", "-q"), ("add", "map_exporter.py"), ("commit", "-qm", "fixture")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "-C", str(self.repo), *args], check=True, capture_output=True, timeout=30)
        self.snapshot = self.root / "baseline"
        create_snapshot(self.repo, self.snapshot)

    def report(self, probe="benchmark"):
        with patch.object(probe_worker, "_load_export", return_value=correct_export):
            return probe_worker.execute(probe, Path("/workspace"), self.root)

    def run_mocked(self, *, report=None, returncode=0, cleanup=0, control=None):
        destination = self.root / "probe"
        process = Mock()
        process.poll.return_value = returncode
        process.returncode = returncode

        def launch(command, **kwargs):
            if report is not None:
                (destination / "probe.json").write_text(json.dumps(report))
            return process

        with patch.object(diagnostics.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, IMAGE + "\n"),
                subprocess.CompletedProcess([], cleanup, "")]) as runs, \
                patch.object(diagnostics.subprocess, "Popen", side_effect=launch) as popen, \
                patch.object(probe_worker, "_load_export", side_effect=AssertionError("host import forbidden")):
            result = diagnostics.run_probe(self.snapshot, destination, "local:test", "benchmark",
                                           execution_control=control)
        return result, popen, runs, process

    def test_fixed_workload_is_immutable_and_rejects_arbitrary_entrypoints(self):
        workload = diagnostics.get_workload("ugc-export-v1")
        self.assertEqual(workload.entrypoint, "map_exporter.py:export_map")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            workload.entrypoint = "os:system"
        with self.assertRaises(ValueError):
            diagnostics.get_workload("os:system")
        for probe in ("shell", "export_map", "benchmark;echo bad"):
            with self.assertRaises(ValueError):
                diagnostics.run_probe(self.snapshot, self.root / "out", "local:test", probe)
        self.assertFalse((self.root / "out").exists())

    def test_invalid_parameters_and_unsafe_destinations_are_rejected_before_docker(self):
        with patch.object(diagnostics.subprocess, "run") as run:
            for timeout in (True, 0, -1, float("nan"), float("inf"), 121):
                with self.assertRaises(ValueError):
                    diagnostics.run_probe(self.snapshot, self.root / "out", "local:test", "cpu", timeout_seconds=timeout)
            for image in ("bad image", "$(whoami)", "-flag", ""):
                with self.assertRaises(ValueError):
                    diagnostics.run_probe(self.snapshot, self.root / "out", image, "cpu")
            for directory in (self.snapshot / "out", self.repo / "out"):
                with self.assertRaises(ValueError):
                    diagnostics.run_probe(self.snapshot, directory, "local:test", "cpu")
            run.assert_not_called()

    def test_changed_snapshot_is_rejected_before_docker(self):
        (self.snapshot / "workspace/map_exporter.py").write_text("changed")
        with patch.object(diagnostics.subprocess, "run") as run, self.assertRaises(ValueError):
            diagnostics.run_probe(self.snapshot, self.root / "out", "local:test", "cpu")
        run.assert_not_called()

    def test_registered_entrypoint_required(self):
        manifest = json.loads((self.snapshot / "manifest.json").read_text())
        manifest["files"] = []
        (self.snapshot / "workspace/map_exporter.py").unlink()
        (self.snapshot / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            diagnostics.run_probe(self.snapshot, self.root / "out", "local:test", "cpu")

    def test_worker_semantics_cover_order_duplicate_missing_and_mutation(self):
        checks = probe_worker._semantic_checks(correct_export)
        self.assertTrue(all(checks.values()))

        def last_wins(objects, resources):
            paths = {item["id"]: item["path"] for item in resources}
            return [{"object_id": item["id"], "resource_path": paths[item["resource_id"]]}
                    for item in objects]
        self.assertFalse(probe_worker._semantic_checks(last_wins)["ordered_first_duplicate"])

        def missing_none(objects, resources):
            if any(item["resource_id"] == 999 for item in objects):
                return []
            return correct_export(objects, resources)
        self.assertFalse(probe_worker._semantic_checks(missing_none)["missing_raises_value_error"])

        def mutating(objects, resources):
            output = correct_export(objects, resources)
            objects.clear()
            return output
        self.assertFalse(probe_worker._semantic_checks(mutating)["inputs_unchanged"])

        def mutating_empty(objects, resources):
            if not objects:
                resources.append({"id": 5000, "path": "unexpected"})
            return correct_export(objects, resources)
        self.assertFalse(probe_worker._semantic_checks(mutating_empty)["inputs_unchanged"])

    def test_each_probe_produces_bounded_valid_evidence(self):
        for probe in probe_worker.PROBES:
            with self.subTest(probe=probe):
                report = self.report(probe)
                path = self.root / "worker.json"
                path.write_text(json.dumps(report))
                validated = diagnostics._validated_report(path, probe, diagnostics.get_workload("ugc-export-v1"))
                self.assertTrue(validated["summary"]["semantic_ok"])
                self.assertEqual(set(validated["summary"]["scales"]), {"small", "large"})
                self.assertLess(len(json.dumps(validated)), 24000)
                for scale in ("small", "large"):
                    row = validated["summary"]["scales"][scale]
                    if probe == "benchmark":
                        self.assertEqual(len(validated["samples"][scale]), 7)
                    elif probe == "memory":
                        self.assertGreater(row["peak_traced_bytes"], 0)
                        self.assertIn("native/RSS", row["measurement_scope"])
                    else:
                        self.assertTrue((self.root / f"cpu-{scale}.pstats").is_file())
                        self.assertLessEqual(len(row["top_functions"]), 15)

    def test_tampered_worker_reports_are_rejected(self):
        original = self.report()
        mutations = [lambda r: r.update(workload_fingerprint="0" * 64),
                     lambda r: r.update(probe="cpu"),
                     lambda r: r["samples"]["large"].append(1.0),
                     lambda r: r["samples"]["small"].__setitem__(0, float("nan")),
                     lambda r: r["summary"]["scales"]["small"].update(median_seconds=999),
                     lambda r: r["summary"]["scales"]["small"].update(fingerprint="bad"),
                     lambda r: r["summary"]["scales"]["small"].update(fingerprint="0" * 64),
                     lambda r: r["summary"]["scales"]["small"].update(unbounded_extra="value"),
                     lambda r: r["summary"].update(semantic_ok=False),
                     lambda r: r["environment"].update(output_path="/tmp/variable")]
        for mutation in mutations:
            report = copy.deepcopy(original)
            mutation(report)
            path = self.root / "worker.json"
            path.write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                diagnostics._validated_report(path, "benchmark", diagnostics.get_workload("ugc-export-v1"))

    def test_run_probe_never_imports_host_source_and_pins_isolation_and_image(self):
        result, popen, runs, _ = self.run_mocked(report=self.report())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["cleanup_ok"])
        self.assertEqual(result["image_id"], IMAGE)
        command = popen.call_args.args[0]
        self.assertIn("--read-only", command)
        self.assertIn("none", command)
        self.assertIn("never", command)
        self.assertIn(IMAGE, command)
        self.assertIn("-I", command)
        self.assertTrue(any("probe_worker.py,readonly" in item for item in command))
        self.assertEqual(runs.call_args_list[-1].args[0][:3], ["docker", "rm", "-f"])
        self.assertTrue((self.root / "probe/probe.log").is_file())
        self.assertEqual(json.loads((self.root / "probe/result.json").read_text()), result)
        with self.assertRaises(FileExistsError):
            diagnostics.run_probe(self.snapshot, self.root / "probe", "local:test", "benchmark")

    def test_missing_report_and_execution_failure_have_distinct_statuses(self):
        result, _, _, _ = self.run_mocked()
        self.assertEqual(result["status"], "report_error")
        self.assertTrue(result["cleanup_ok"])
        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory)
            result, _, _, _ = self.run_mocked(returncode=3)
            self.assertEqual(result["status"], "execution_error")

    def test_cleanup_failure_remains_visible(self):
        result, _, _, _ = self.run_mocked(report=self.report(), cleanup=1)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["cleanup_ok"])

    def test_timeout_kills_client_and_confirms_container_cleanup(self):
        process = Mock()
        process.poll.return_value = None
        expired = [False]

        def launch(*args, **kwargs):
            expired[0] = True
            return process
        with patch.object(diagnostics.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, IMAGE), subprocess.CompletedProcess([], 0, "")]) as runs, \
                patch.object(diagnostics.subprocess, "Popen", side_effect=launch), \
                patch.object(diagnostics.time, "monotonic", side_effect=lambda: 2 if expired[0] else 0):
            result = diagnostics.run_probe(self.snapshot, self.root / "timeout", "local:test", "cpu", timeout_seconds=1)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["cleanup_ok"])
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(runs.call_args_list[-1].args[0][:3], ["docker", "rm", "-f"])

    def test_slow_image_inspection_cannot_return_ok_after_probe_deadline(self):
        destination = self.root / "slow-inspect"
        report = self.report()
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0

        def cli(command, **kwargs):
            if command[1:3] == ["image", "inspect"]:
                time.sleep(.08)
                return subprocess.CompletedProcess(command, 0, IMAGE)
            return subprocess.CompletedProcess(command, 0, "")

        def launch(*args, **kwargs):
            (destination / "probe.json").write_text(json.dumps(report))
            return process

        with patch.object(diagnostics.subprocess, "run", side_effect=cli) as runs, \
                patch.object(diagnostics.subprocess, "Popen", side_effect=launch) as popen:
            result = diagnostics.run_probe(self.snapshot, destination, "local:test", "benchmark", timeout_seconds=.01)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["cleanup_ok"])
        self.assertLessEqual(runs.call_args_list[0].kwargs["timeout"], .01)
        popen.assert_not_called()

    def test_finished_process_cannot_return_ok_after_probe_deadline(self):
        destination = self.root / "slow-finish"
        report = self.report()
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        expired = [False]

        def launch(*args, **kwargs):
            (destination / "probe.json").write_text(json.dumps(report))
            expired[0] = True
            return process

        with patch.object(diagnostics.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, IMAGE), subprocess.CompletedProcess([], 0, "")]), \
                patch.object(diagnostics.subprocess, "Popen", side_effect=launch), \
                patch.object(diagnostics.time, "monotonic", side_effect=lambda: 2 if expired[0] else 0):
            result = diagnostics.run_probe(self.snapshot, destination, "local:test", "benchmark", timeout_seconds=1)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["cleanup_ok"])
        process.kill.assert_not_called()

    def test_cancellation_propagates_after_cleanup_and_recording(self):
        class StopRequested(RuntimeError):
            pass
        control = Mock()
        launched = [False]

        def check():
            if launched[0]:
                raise StopRequested("budget stop")
        control.check.side_effect = check
        process = Mock()
        process.poll.return_value = None

        def launch(*args, **kwargs):
            launched[0] = True
            return process
        destination = self.root / "cancel"
        with patch.object(diagnostics.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, IMAGE), subprocess.CompletedProcess([], 0, "")]), \
                patch.object(diagnostics.subprocess, "Popen", side_effect=launch), \
                self.assertRaisesRegex(StopRequested, "budget stop"):
            diagnostics.run_probe(self.snapshot, destination, "local:test", "cpu", execution_control=control)
        process.kill.assert_called_once()
        self.assertTrue(json.loads((destination / "result.json").read_text())["cleanup_ok"])
        self.assertEqual(control.container.call_args_list[-1].args[1], "cleaned")


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS") == "1", "explicit local Docker diagnostics only")
class DiagnosticDockerTests(unittest.TestCase):
    def test_registered_worker_in_real_container(self):
        runtime = Path(__file__).resolve().parents[1] / "runtime"
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            root = Path(directory)
            repo = root / "source"
            repo.mkdir()
            source = ("def export_map(objects, resources):\n"
                      "    first = {}\n"
                      "    for resource in resources:\n"
                      "        first.setdefault(resource['id'], resource['path'])\n"
                      "    output = []\n"
                      "    for obj in objects:\n"
                      "        if obj['resource_id'] not in first:\n"
                      "            raise ValueError('missing')\n"
                      "        output.append({'object_id': obj['id'], 'resource_path': first[obj['resource_id']]})\n"
                      "    return output\n")
            (repo / "map_exporter.py").write_text(source)
            for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "fixture")):
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "-C", str(repo), *args], check=True, capture_output=True, timeout=30)
            snapshot = root / "baseline"
            create_snapshot(repo, snapshot)
            image = os.environ.get("REPAIR_TEST_IMAGE", "study-agent-deepagents:0.1")
            for probe in probe_worker.PROBES:
                result = diagnostics.run_probe(snapshot, root / probe, image, probe)
                self.assertEqual(result["status"], "ok", result)
                self.assertTrue(result["cleanup_ok"])
                self.assertTrue(result["summary"]["semantic_ok"])
                self.assertEqual((repo / "map_exporter.py").read_text(), source)


if __name__ == "__main__":
    unittest.main()
