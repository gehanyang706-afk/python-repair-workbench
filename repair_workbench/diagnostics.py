"""Registered diagnostic probes executed in isolated local Docker containers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import probe_worker
from .snapshot import snapshot_id, verify_snapshot


@dataclass(frozen=True)
class Workload:
    id: str
    entrypoint: str
    scales: tuple[tuple[str, int, int], ...]
    warmups: int
    repeats: int
    fingerprint: str


def get_workload(name):
    """Return a fixed specification; callers cannot register arbitrary code or commands."""
    if name != probe_worker.WORKLOAD_ID:
        raise ValueError("unknown diagnostic workload")
    return Workload(name, "map_exporter.py:export_map", probe_worker.SCALES,
                    probe_worker.WARMUPS, probe_worker.REPEATS, probe_worker.workload_fingerprint())


def _number(value, *, integer=False):
    return (type(value) is int if integer else type(value) in {int, float}) and math.isfinite(value) and value >= 0


def _validated_report(path, probe, workload):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ValueError("missing_or_oversized_probe_report")
    report = json.loads(path.read_text(encoding="utf-8"))
    if (report.get("schema_version") != 1 or report.get("probe") != probe
            or report.get("workload_id") != workload.id
            or report.get("workload_fingerprint") != workload.fingerprint):
        raise ValueError("probe_report_identity_mismatch")
    summary = report.get("summary", {})
    if set(summary) != {"semantic_ok", "semantic_checks", "scales"} or type(summary.get("semantic_ok")) is not bool:
        raise ValueError("invalid_semantic_result")
    checks = summary.get("semantic_checks")
    if not isinstance(checks, dict) or set(checks) != {"ordered_first_duplicate", "inputs_unchanged", "empty_objects",
                                                       "missing_raises_value_error", "missing_inputs_unchanged"} or any(type(v) is not bool for v in checks.values()):
        raise ValueError("invalid_semantic_checks")
    scales, samples = summary.get("scales", {}), report.get("samples", {})
    names = {row[0] for row in workload.scales}
    if not isinstance(scales, dict) or set(scales) != names or not isinstance(samples, dict):
        raise ValueError("invalid_probe_scales")
    if set(samples) != (set() if probe == "cpu" else names):
        raise ValueError("invalid_probe_samples")
    for scale, objects, resources in workload.scales:
        row = scales[scale]
        fields = {"object_count", "resource_count", "semantic_ok", "fingerprint"}
        fields.update({"benchmark": {"median_seconds", "min_seconds", "max_seconds"},
                       "memory": {"peak_traced_bytes", "measurement_scope"},
                       "cpu": {"total_calls", "top_functions"}}[probe])
        if (not isinstance(row, dict) or row.get("object_count") != objects or row.get("resource_count") != resources
                or set(row) != fields or type(row.get("semantic_ok")) is not bool):
            raise ValueError("invalid_probe_scale")
        if row["semantic_ok"] and not re.fullmatch(r"[0-9a-f]{64}", str(row.get("fingerprint"))):
            raise ValueError("invalid_semantic_fingerprint")
        if row["semantic_ok"]:
            inputs = probe_worker.make_input(objects, resources)
            if row["fingerprint"] != probe_worker._digest(probe_worker._expected(*inputs)):
                raise ValueError("semantic_fingerprint_mismatch")
        if probe == "benchmark":
            values = samples[scale]
            if (not isinstance(values, list) or len(values) != workload.repeats
                    or not all(_number(v) for v in values)
                    or row.get("median_seconds") != statistics.median(values)
                    or row.get("min_seconds") != min(values) or row.get("max_seconds") != max(values)):
                raise ValueError("invalid_benchmark_samples")
        elif probe == "memory":
            if (not _number(row.get("peak_traced_bytes"), integer=True)
                    or samples[scale] != [row["peak_traced_bytes"]]
                    or not isinstance(row.get("measurement_scope"), str)
                    or len(row["measurement_scope"]) > 200):
                raise ValueError("invalid_memory_samples")
        else:
            top = row.get("top_functions")
            if not _number(row.get("total_calls"), integer=True) or not isinstance(top, list) or len(top) > 15:
                raise ValueError("invalid_cpu_profile")
            for function in top:
                if (not isinstance(function, dict) or set(function) != {"file", "function", "line", "primitive_calls", "calls", "own_seconds", "cumulative_seconds"}
                        or not all(isinstance(function.get(k), str) and len(function[k]) <= 180 for k in ("file", "function"))
                        or not all(_number(function.get(k), integer=True) for k in ("line", "primitive_calls", "calls"))
                        or not all(_number(function.get(k)) for k in ("own_seconds", "cumulative_seconds"))):
                    raise ValueError("invalid_cpu_function")
    expected_ok = all(checks.values()) and all(row["semantic_ok"] for row in scales.values())
    if summary["semantic_ok"] != expected_ok:
        raise ValueError("inconsistent_semantic_summary")
    environment = report.get("environment")
    if (not isinstance(environment, dict) or set(environment) != {"python_version", "python_implementation", "platform_system", "machine", "clock", "isolation"}
            or any(not isinstance(environment.get(k), str) or not 1 <= len(environment[k]) <= 100 for k in ("python_version", "python_implementation", "platform_system", "machine"))
            or environment.get("clock") != "perf_counter_ns"
            or environment.get("isolation") != {"cpus": 1, "memory_bytes": 268435456}):
        raise ValueError("invalid_probe_environment")
    return {key: report[key] for key in ("summary", "samples", "environment")}


def run_probe(snapshot: Path, destination: Path, image: str, probe: str,
              workload="ugc-export-v1", timeout_seconds=30, *, execution_control=None) -> dict:
    """Run only a registered probe, never a caller-provided shell or target function."""
    spec = get_workload(workload)
    if probe not in probe_worker.PROBES:
        raise ValueError("unknown diagnostic probe")
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120):
        raise ValueError("diagnostic timeout must be finite and in (0,120]")
    if not isinstance(image, str) or len(image) > 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image):
        raise ValueError("invalid diagnostic image")
    snapshot = Path(snapshot).resolve(strict=True)
    manifest = verify_snapshot(snapshot)
    if "map_exporter.py" not in {item["path"] for item in manifest["files"]}:
        raise ValueError("registered map_exporter.py entrypoint is absent")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("cannot overwrite diagnostic results")
    destination = destination.resolve()
    source_repo = Path(manifest["source_repo"]).resolve()
    if any(destination == root or root in destination.parents for root in (snapshot, source_repo)):
        raise ValueError("diagnostic results must be outside baseline and original repository")
    worker = Path(probe_worker.__file__).resolve(strict=True)
    if any("," in str(path) for path in (snapshot, destination, worker)):
        raise ValueError("diagnostic mount paths cannot contain commas")
    if execution_control:
        execution_control.check()
    destination.mkdir(parents=True, exist_ok=False)
    name = "repair-probe-" + uuid.uuid4().hex[:16]
    result = {"status": "execution_error", "snapshot_id": snapshot_id(manifest), "image_id": None,
              "workload_id": spec.id, "workload_fingerprint": spec.fingerprint,
              "probe": probe, "harness_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
              "cleanup_ok": True, "summary": {}, "samples": {}, "environment": {},
              "returncode": None, "timeout_seconds": timeout_seconds}
    started = time.monotonic()
    reserved = False

    def check_deadline():
        if execution_control:
            execution_control.check()
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("registered diagnostic probe", timeout_seconds)
        return remaining

    try:
        image_id = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                  capture_output=True, text=True, check=True,
                                  timeout=min(15, check_deadline())).stdout.strip()
        check_deadline()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("invalid_local_image_identity")
        result["image_id"] = image_id
        command = ["docker", "run", "--name", name, "--pull", "never", "--network", "none", "--read-only",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
                   "--pids-limit", "64", "--memory", "256m", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
                   "--mount", f"type=bind,src={snapshot / 'workspace'},dst=/workspace,readonly",
                   "--mount", f"type=bind,src={worker},dst=/trusted/probe_worker.py,readonly",
                   "--mount", f"type=bind,src={destination},dst=/reports", "--workdir", "/workspace",
                   "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "HOME=/tmp", "--entrypoint", "python",
                   image_id, "-I", "/trusted/probe_worker.py", "--probe", probe, "--workload", workload,
                   "--output", "/reports/probe.json"]
        result["command"] = command
        with (destination / "probe.log").open("x", encoding="utf-8") as log:
            check_deadline()
            if execution_control:
                execution_control.container(name, "reserved")
            reserved = True
            result["cleanup_ok"] = False
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    check_deadline()
                    time.sleep(.05)
                result["returncode"] = process.returncode
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        check_deadline()
        if result["returncode"] == 0:
            try:
                result.update(_validated_report(destination / "probe.json", probe, spec))
                check_deadline()
                result["status"] = "ok"
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                result["status"] = "report_error"
        else:
            result["status"] = "execution_error"
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
    finally:
        if reserved:
            try:
                cleaned = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=15)
                result["cleanup_ok"] = cleaned.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                result["cleanup_ok"] = False
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        with (destination / "result.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if execution_control and reserved and result["cleanup_ok"]:
            execution_control.container(name, "cleaned")
    return result
