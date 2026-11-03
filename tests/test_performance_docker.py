"""Opt-in real performance-verifier integration; reference patches are not Agent results."""
import importlib.util
import json
import os
import subprocess
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from repair_workbench.candidate import create_candidate
from repair_workbench.performance import verify_performance_candidate
from repair_workbench.runner import run_tests
from repair_workbench.snapshot import create_snapshot, snapshot_id, verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_SOURCE = '''"""Known first-match index, used only to exercise the performance verifier."""


def export_map(objects, resources):
    first = {}
    for resource in resources:
        first.setdefault(resource["id"], resource["path"])
    result = []
    for item in objects:
        if item["resource_id"] not in first:
            raise ValueError("unknown resource")
        result.append({"object_id": item["id"], "resource_path": first[item["resource_id"]]})
    return result
'''


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS") == "1", "explicit local Docker performance verification only")
class PerformanceDockerTests(unittest.TestCase):
    def test_reviewed_case01_reference_candidate_has_complete_independent_measurements(self):
        # Deliberately retain every attempt, including failures and uncertain timing.
        # Never retry automatically or remove this directory to select a better run.
        runtime = ROOT / "runtime" / "performance-verifier-docker"
        runtime.mkdir(parents=True, exist_ok=True)
        attempt = runtime / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
        attempt.mkdir()
        evidence = {
            "origin": "known_reference_candidate", "counts_as_agent_result": False,
            "real_model_calls": 0, "fixture": "case-01", "status": "running",
            "attempt_directory": str(attempt),
            "thresholds": {"minimum_speedup": .15, "max_memory_ratio": 1.25},
        }

        def save():
            (attempt / "integration.json").write_text(json.dumps(evidence, indent=2) + "\n")

        save()
        try:
            spec = importlib.util.spec_from_file_location(
                "performance_docker_fixture_qualification",
                ROOT / "benchmarks/ugc-diagnostics-dev-v1/qualification.py",
            )
            qualification = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(qualification)
            qualification.fixture_manifest()
            repo = attempt / "source"
            repo.mkdir()
            # Read hash-pinned bundled bytes only; no submitted code runs on the host.
            for relative in qualification.TRUSTED_FILES["case-01"]:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(qualification._trusted_bytes("case-01", relative))
            for arguments in (("init", "-q"), ("add", "."), ("commit", "-qm", "reviewed-case01")):
                subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                     "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "-C", str(repo), *arguments], check=True, capture_output=True, timeout=30,
                )
            baseline = attempt / "baseline"
            baseline_manifest = create_snapshot(repo, baseline)
            baseline_id = snapshot_id(baseline_manifest)
            image = os.environ.get("REPAIR_TEST_IMAGE", "study-agent-deepagents:0.1")
            before = run_tests(baseline, image, 30)
            evidence["baseline_result"] = before
            save()
            self.assertEqual(before["status"], "tests_passed", before)
            self.assertTrue(before["cleanup_ok"], before)
            self.assertEqual(before["snapshot_id"], baseline_id)

            candidate, verified = attempt / "candidate", attempt / "verified"
            create_candidate(baseline, candidate, {"map_exporter.py": REFERENCE_SOURCE}, {"map_exporter.py"})
            result = verify_performance_candidate(baseline, candidate, verified, {"map_exporter.py"})
            evidence.update(status="measured", verification_status=result["status"],
                            verification_reason=result["reason"],
                            verification_report=str(verified / "performance-verification.json"))
            save()

            # A failed threshold or uncertain measurement remains a legitimate result.
            # Missing measurements, changed identity or incomplete cleanup are integration failures.
            self.assertIn(result["status"], {"passed", "failed", "inconclusive"})
            self.assertIn(result["reason"], {
                "passed_registered_performance_scope", "minimum_speedup_not_met",
                "small_workload_regression", "memory_regression", "measurement_noise_exceeded",
            }, result)
            self.assertEqual(result["base_snapshot_id"], baseline_id)
            self.assertEqual(result["candidate_snapshot_id"], snapshot_id(verify_snapshot(verified)))
            self.assertEqual(result["image_id"], before["image_id"])
            self.assertEqual(result["thresholds"]["minimum_speedup"], .15)
            self.assertEqual(result["thresholds"]["max_memory_ratio"], 1.25)
            self.assertEqual(result["candidate_test_result"]["status"], "tests_passed")
            self.assertTrue(result["candidate_test_result"]["cleanup_ok"])
            self.assertEqual(len(result["measurements"]), 8)
            for measurement in result["measurements"]:
                report = measurement["report"]
                expected_id = baseline_id if measurement["role"] == "baseline" else result["candidate_snapshot_id"]
                self.assertEqual(report["snapshot_id"], expected_id)
                self.assertEqual(report["image_id"], before["image_id"])
                self.assertEqual(report["status"], "ok", report)
                self.assertTrue(report["cleanup_ok"], report)
                self.assertTrue(report["summary"]["semantic_ok"], report)
                self.assertEqual(report["environment"], result["environment"])
                self.assertEqual(report["harness_sha256"], result["harness_sha256"])
                self.assertEqual(report["workload_fingerprint"], result["workload_fingerprint"])
                expected_samples = 7 if measurement["probe"] == "benchmark" else 1
                for scale in ("small", "large"):
                    self.assertEqual(len(report["samples"][scale]), expected_samples)
            for scale in ("small", "large"):
                for role in ("baseline", "candidate"):
                    self.assertEqual(len(result["metrics"][scale]["samples_seconds"][role]), 14)
                    self.assertEqual(len(result["metrics"][scale]["memory_samples_bytes"][role]), 2)
            if result["status"] == "passed":
                # A noisy run may never become a success merely because its median is faster.
                self.assertTrue(all(noise <= .20 for scale in result["metrics"].values()
                                    for noise in scale["relative_noise"].values()))
                self.assertGreaterEqual(result["metrics"]["large"]["speedup"], .15)
                self.assertLessEqual(result["metrics"]["small"]["time_ratio"], 1.10)
                self.assertTrue(all(scale["memory_ratio"] <= 1.25 for scale in result["metrics"].values()))
            self.assertEqual(json.loads((verified / "performance-verification.json").read_text()), result)
            self.assertEqual((repo / "map_exporter.py").read_bytes(),
                             qualification._trusted_bytes("case-01", "map_exporter.py"))
            evidence["status"] = "integration_verified"
        except BaseException as exc:
            evidence.update(status="integration_failed", error_type=type(exc).__name__)
            raise
        finally:
            save()


if __name__ == "__main__":
    unittest.main()
