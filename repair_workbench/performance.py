"""Independent, registered-workload performance verification for Python candidates.

The verifier owns measurement order and acceptance rules. Profiling reports are
diagnostic evidence only; timings here come from the unprofiled benchmark probe.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .candidate import create_candidate
from .diagnostics import run_probe
from .runner import run_tests
from .snapshot import snapshot_id, verify_snapshot
from .verification import test_outcomes


WORKLOAD = "ugc-export-v1"
SCALES = {"small": (300, 80), "large": (6000, 600)}
MAX_RELATIVE_NOISE = 0.20
MAX_SMALL_TIME_RATIO = 1.10
BENCHMARK_REPEATS = 7
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")


def _number(value, *, positive=True):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _counts_match(result, outcomes):
    expected = {"total": len(outcomes), "passed": len(outcomes),
                "failed": 0, "errors": 0, "skipped": 0}
    return (bool(outcomes) and all(value == "passed" for value in outcomes.values())
            and result.get("counts") == expected)


def _safe_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _save(destination, record, status, reason, **details):
    record.update(status=status, reason=reason, **details)
    (destination / "performance-verification.json").write_text(
        json.dumps(_safe_json(record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def _probe_problem(report, expected_snapshot, image, probe):
    if not isinstance(report, dict):
        return "inconclusive", "unusable_probe_report"
    if report.get("cleanup_ok") is not True:
        return "inconclusive", "container_cleanup_unconfirmed"
    if report.get("status") != "ok":
        return "inconclusive", "probe_" + str(report.get("status", "report_error"))
    if report.get("snapshot_id") != expected_snapshot:
        return "inconclusive", "probe_snapshot_mismatch"
    if report.get("image_id") != image:
        return "inconclusive", "environment_changed"
    if report.get("workload_id") != WORKLOAD or report.get("probe") != probe:
        return "inconclusive", "workload_or_probe_changed"
    environment = report.get("environment", {})
    if (not isinstance(environment, dict)
            or any(not isinstance(environment.get(key), str) or not environment[key]
                   for key in ("python_version", "python_implementation", "platform_system", "machine"))
            or environment.get("clock") != "perf_counter_ns"
            or environment.get("isolation") != {"cpus": 1, "memory_bytes": 268435456}
            or not all(isinstance(report.get(key), str) and _DIGEST.fullmatch(report[key])
                       for key in ("harness_sha256", "workload_fingerprint"))):
        return "inconclusive", "unusable_probe_environment"
    summary = report.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("semantic_ok"), bool):
        return "inconclusive", "unusable_probe_report"
    if not summary["semantic_ok"]:
        return "failed", "semantic_mismatch"
    scales, samples = summary.get("scales"), report.get("samples")
    if (not isinstance(scales, dict) or set(scales) != set(SCALES)
            or not isinstance(samples, dict) or set(samples) != set(SCALES)):
        return "inconclusive", "unusable_probe_report"
    for scale, (objects, resources) in SCALES.items():
        value, measured = scales[scale], samples.get(scale)
        if not isinstance(value, dict) or not isinstance(value.get("semantic_ok"), bool):
            return "inconclusive", "unusable_probe_report"
        if not value["semantic_ok"]:
            return "failed", "semantic_mismatch"
        if (value.get("object_count") != objects or value.get("resource_count") != resources
                or not isinstance(value.get("fingerprint"), str)
                or not _DIGEST.fullmatch(value["fingerprint"])):
            return "inconclusive", "workload_changed"
        count = BENCHMARK_REPEATS if probe == "benchmark" else 1
        if (not isinstance(measured, list) or len(measured) != count
                or not all(_number(item) for item in measured)):
            return "inconclusive", "unusable_measurement_samples"
        field = "median_seconds" if probe == "benchmark" else "peak_traced_bytes"
        expected = statistics.median(measured) if probe == "benchmark" else measured[0]
        if (not _number(value.get(field))
                or not math.isclose(value[field], expected, rel_tol=1e-9, abs_tol=0)):
            return "inconclusive", "measurement_summary_mismatch"
    return None


def verify_performance_candidate(
    baseline: Path, candidate: Path, destination: Path, allowed_paths: set[str],
    workload="ugc-export-v1", timeout_seconds=30, *, execution_control=None,
    minimum_speedup=0.15, max_memory_ratio=1.25,
) -> dict:
    """Rebuild approved source changes and measure ABBA on one immutable image.

    ``allowed_paths`` and thresholds must be supplied by the trusted application,
    never copied from an Agent's proposal. This is a controlled-project verifier,
    not an adversarial-code sandbox or a proof of all Python program semantics.
    """
    if (workload != WORKLOAD or not _number(timeout_seconds) or timeout_seconds > 120
            or not _number(minimum_speedup) or minimum_speedup >= 1
            or not _number(max_memory_ratio) or max_memory_ratio < 1):
        raise ValueError("Invalid registered workload, timeout, or performance thresholds")
    baseline, candidate = Path(baseline).resolve(strict=True), Path(candidate).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination == candidate or candidate in destination.parents:
        raise ValueError("Verification must not overwrite or nest inside the submitted candidate")
    measurement_directory = destination.with_name(destination.name + "-measurements")
    if measurement_directory.exists() or measurement_directory.is_symlink():
        raise FileExistsError("Cannot overwrite prior performance measurements")
    base, proposed = verify_snapshot(baseline), verify_snapshot(candidate)
    base_id, proposed_id = snapshot_id(base), snapshot_id(proposed)
    if proposed.get("base_snapshot_id") != base_id:
        raise ValueError("Candidate does not derive from this baseline")
    before = json.loads((baseline / "reports/result.json").read_text(encoding="utf-8"))
    original_tests = test_outcomes(baseline / "reports/pytest.xml")
    if (before.get("snapshot_id") != base_id or before.get("status") != "tests_passed"
            or before.get("cleanup_ok") is not True or not _counts_match(before, original_tests)
            or not isinstance(before.get("image_id"), str) or not _IMAGE.fullmatch(before["image_id"])):
        raise ValueError("Performance baseline needs matching, clean, passing original tests and an immutable image")
    base_files = {item["path"]: item["sha256"] for item in base["files"]}
    proposed_files = {item["path"]: item["sha256"] for item in proposed["files"]}
    if base_files.keys() != proposed_files.keys():
        raise ValueError("Candidate cannot add or remove files")
    changes = {path: (candidate / "workspace" / path).read_text(encoding="utf-8")
               for path in base_files if base_files[path] != proposed_files[path]}
    rebuilt = create_candidate(baseline, destination, changes, allowed_paths)
    rebuilt_id, image = snapshot_id(rebuilt), before["image_id"]
    record = {
        "workload_id": workload, "base_snapshot_id": base_id,
        "submitted_snapshot_id": proposed_id, "candidate_snapshot_id": rebuilt_id,
        "changed_paths": sorted(changes), "image_id": image,
        "measurement_directory": str(measurement_directory),
        "thresholds": {"minimum_speedup": minimum_speedup, "max_memory_ratio": max_memory_ratio,
                       "max_relative_noise": MAX_RELATIVE_NOISE,
                       "max_small_time_ratio": MAX_SMALL_TIME_RATIO},
        "protocol": {"order": ["baseline", "candidate", "candidate", "baseline"],
                     "warmups_per_run": 2,
                     "benchmark_samples_per_run": BENCHMARK_REPEATS,
                     "timeout_seconds_per_run": timeout_seconds,
                     "profiler_enabled_for_timing": False},
        "original_tests": original_tests, "baseline_test_result": before, "measurements": [],
        "scope": "Original tests and the registered UGC workload only; not all input semantics or game rendering",
    }
    control = {"execution_control": execution_control} if execution_control is not None else {}
    try:
        after = run_tests(destination, image, timeout_seconds, **control)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return _save(destination, record, "inconclusive", "candidate_test_execution_error",
                     error_type=type(exc).__name__)
    record["candidate_test_result"] = after
    if after.get("cleanup_ok") is not True:
        return _save(destination, record, "inconclusive", "container_cleanup_unconfirmed")
    try:
        unchanged = snapshot_id(verify_snapshot(destination)) == rebuilt_id
    except (OSError, ValueError):
        unchanged = False
    if (after.get("snapshot_id") != rebuilt_id or after.get("image_id") != image or not unchanged):
        return _save(destination, record, "inconclusive", "candidate_test_environment_changed")
    if after.get("status") in {"timeout", "execution_error", "report_error"}:
        return _save(destination, record, "inconclusive", "candidate_test_" + after["status"])
    try:
        candidate_tests = test_outcomes(destination / "reports/pytest.xml")
    except (OSError, ValueError, ET.ParseError) as exc:
        return _save(destination, record, "inconclusive", "unusable_candidate_test_report",
                     error_type=type(exc).__name__)
    if original_tests.keys() != candidate_tests.keys():
        return _save(destination, record, "failed", "test_set_changed")
    if any(value != "passed" for value in candidate_tests.values()) or after.get("status") != "tests_passed":
        return _save(destination, record, "failed", "candidate_semantics_failed")
    if not _counts_match(after, candidate_tests):
        return _save(destination, record, "inconclusive", "candidate_test_report_mismatch")

    reference = None
    # Each probe has ABBA order. The second pair reverses order to expose drift.
    for round_number, order in enumerate((("baseline", "candidate"), ("candidate", "baseline")), 1):
        for probe in ("benchmark", "memory"):
            for role in order:
                snapshot = baseline if role == "baseline" else destination
                expected_id = base_id if role == "baseline" else rebuilt_id
                output = measurement_directory / f"round-{round_number}-{probe}-{role}"
                try:
                    report = run_probe(snapshot, output, image, probe, workload, timeout_seconds, **control)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    return _save(destination, record, "inconclusive", "probe_execution_error",
                                 error_type=type(exc).__name__)
                record["measurements"].append({"round": round_number, "role": role,
                                               "probe": probe, "report": report})
                problem = _probe_problem(report, expected_id, image, probe)
                if problem:
                    return _save(destination, record, *problem)
                identity = {key: report[key] for key in ("environment", "harness_sha256", "workload_fingerprint")}
                if reference is None:
                    reference = identity
                elif identity != reference:
                    return _save(destination, record, "inconclusive", "measurement_environment_or_workload_changed")

    metrics = {}
    for scale in SCALES:
        fingerprints, timings, peaks = set(), {}, {}
        for role in ("baseline", "candidate"):
            rows = [item for item in record["measurements"] if item["role"] == role]
            fingerprints.update(item["report"]["summary"]["scales"][scale]["fingerprint"] for item in rows)
            timings[role] = [sample for item in rows if item["probe"] == "benchmark"
                             for sample in item["report"]["samples"][scale]]
            peaks[role] = [sample for item in rows if item["probe"] == "memory"
                          for sample in item["report"]["samples"][scale]]
        if len(fingerprints) != 1:
            return _save(destination, record, "failed", "semantic_fingerprint_changed")
        medians = {role: statistics.median(values) for role, values in timings.items()}
        noise = {role: statistics.pstdev(values) / statistics.mean(values) for role, values in timings.items()}
        metrics[scale] = {
            "samples_seconds": timings, "memory_samples_bytes": peaks,
            "median_seconds": medians, "relative_noise": noise,
            "time_ratio": medians["candidate"] / medians["baseline"],
            "speedup": 1 - medians["candidate"] / medians["baseline"],
            "memory_ratio": max(peaks["candidate"]) / min(peaks["baseline"]),
        }
    record["metrics"], record["environment"] = metrics, reference["environment"]
    record["harness_sha256"] = reference["harness_sha256"]
    record["workload_fingerprint"] = reference["workload_fingerprint"]
    if any(noise > MAX_RELATIVE_NOISE for value in metrics.values() for noise in value["relative_noise"].values()):
        return _save(destination, record, "inconclusive", "measurement_noise_exceeded")
    if any(value["memory_ratio"] > max_memory_ratio for value in metrics.values()):
        return _save(destination, record, "failed", "memory_regression")
    if metrics["small"]["time_ratio"] > MAX_SMALL_TIME_RATIO:
        return _save(destination, record, "failed", "small_workload_regression")
    if metrics["large"]["speedup"] < minimum_speedup:
        return _save(destination, record, "failed", "minimum_speedup_not_met")
    return _save(destination, record, "passed", "passed_registered_performance_scope")
