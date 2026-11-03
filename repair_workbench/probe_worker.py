"""Trusted, stdlib-only workload runner. Repository code executes only in Docker."""
from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import importlib.util
import json
import platform
import pstats
import statistics
import time
import tracemalloc
from pathlib import Path


WORKLOAD_ID = "ugc-export-v1"
SCALES = (("small", 300, 80), ("large", 6000, 600))
WARMUPS = 2
REPEATS = 7
PROBES = ("cpu", "memory", "benchmark")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def workload_spec():
    return {"id": WORKLOAD_ID, "entrypoint": "map_exporter.py:export_map",
            "scales": [{"name": name, "objects": objects, "resources": resources}
                       for name, objects, resources in SCALES],
            "warmups": WARMUPS, "repeats": REPEATS, "input_version": 1,
            "semantics": "ordered mapping; first duplicate resource; missing raises ValueError; inputs unchanged"}


def workload_fingerprint():
    inputs = {name: make_input(objects, resources) for name, objects, resources in SCALES}
    return _digest({"specification": workload_spec(), "inputs": inputs})


def make_input(object_count, resource_count):
    resources = [{"id": number, "path": f"assets/resource_{number:04d}.bin"}
                 for number in range(resource_count)]
    # The duplicate distinguishes first-match lookup from a last-write-wins index.
    resources.append({"id": 0, "path": "assets/duplicate_must_not_win.bin"})
    objects = [{"id": number, "resource_id": (number * 37) % resource_count}
               for number in range(object_count)]
    return objects, resources


def _expected(objects, resources):
    first = {}
    for resource in resources:
        first.setdefault(resource["id"], resource["path"])
    return [{"object_id": item["id"], "resource_path": first[item["resource_id"]]}
            for item in objects]


def _valid_output(output, expected):
    return (type(output) is list and len(output) == len(expected)
            and all(type(item) is dict and set(item) == {"object_id", "resource_path"}
                    and type(item["object_id"]) is int and type(item["resource_path"]) is str
                    for item in output) and output == expected)


def _semantic_checks(export):
    objects = [{"id": 7, "resource_id": 2}, {"id": 3, "resource_id": 1},
               {"id": 9, "resource_id": 2}]
    resources = [{"id": 1, "path": "first"}, {"id": 2, "path": "second"},
                 {"id": 1, "path": "wrong"}]
    before = _digest([objects, resources])
    checks = {"ordered_first_duplicate": _valid_output(export(objects, resources),
                                                       _expected(objects, resources)),
              "inputs_unchanged": _digest([objects, resources]) == before}
    empty = []
    before_empty = _digest([empty, resources])
    checks["empty_objects"] = _valid_output(export(empty, resources), [])
    checks["inputs_unchanged"] = checks["inputs_unchanged"] and _digest([empty, resources]) == before_empty
    missing = [{"id": 1, "resource_id": 999}]
    before_missing = _digest([missing, resources])
    try:
        export(missing, resources)
    except ValueError:
        checks["missing_raises_value_error"] = True
    except Exception:
        checks["missing_raises_value_error"] = False
    else:
        checks["missing_raises_value_error"] = False
    checks["missing_inputs_unchanged"] = _digest([missing, resources]) == before_missing
    return checks


def _load_export(workspace):
    # This function is invoked by the container entrypoint, never by run_probe on the host.
    spec = importlib.util.spec_from_file_location("_registered_map_exporter", workspace / "map_exporter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    export = getattr(module, "export_map", None)
    if not callable(export):
        raise ValueError("registered_entrypoint_not_callable")
    return export


def _profile_summary(profile, workspace):
    stats = pstats.Stats(profile).stats
    rows = []
    for (filename, line, function), (primitive, calls, own, cumulative, _) in stats.items():
        try:
            filename = str(Path(filename).relative_to(workspace))
        except ValueError:
            filename = Path(filename).name
        rows.append({"file": filename[:180], "line": line, "function": function[:180],
                     "primitive_calls": primitive, "calls": calls,
                     "own_seconds": own, "cumulative_seconds": cumulative})
    rows.sort(key=lambda item: item["cumulative_seconds"], reverse=True)
    return {"total_calls": sum(item["calls"] for item in rows), "top_functions": rows[:15]}


def execute(probe, workspace, report_directory):
    if probe not in PROBES:
        raise ValueError("unknown_probe")
    export = _load_export(workspace)
    checks = _semantic_checks(export)
    scales, samples = {}, {}
    for scale, object_count, resource_count in SCALES:
        valid = True
        fingerprints = set()

        def invoke(measure):
            nonlocal valid
            objects, resources = make_input(object_count, resource_count)
            expected = _expected(objects, resources)
            before = _digest([objects, resources])
            output, metric = measure(export, objects, resources)
            output_ok = _valid_output(output, expected)
            valid = valid and output_ok and _digest([objects, resources]) == before
            if output_ok:
                fingerprints.add(_digest(output))
            return metric

        for _ in range(WARMUPS):
            invoke(lambda function, objects, resources: (function(objects, resources), None))
        row = {"object_count": object_count, "resource_count": resource_count}
        if probe == "benchmark":
            def timed(function, objects, resources):
                started = time.perf_counter_ns()
                output = function(objects, resources)
                elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
                return output, elapsed
            samples[scale] = [invoke(timed) for _ in range(REPEATS)]
            row.update(median_seconds=statistics.median(samples[scale]),
                       min_seconds=min(samples[scale]), max_seconds=max(samples[scale]))
        elif probe == "memory":
            def traced(function, objects, resources):
                gc.collect()
                tracemalloc.start()
                try:
                    output = function(objects, resources)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                return output, peak
            peak = invoke(traced)
            samples[scale] = [peak]
            row.update(peak_traced_bytes=peak,
                       measurement_scope="Python tracemalloc peak; excludes input construction and native/RSS memory")
        else:
            profile = cProfile.Profile()
            def profiled(function, objects, resources):
                profile.enable()
                try:
                    output = function(objects, resources)
                finally:
                    profile.disable()
                return output, None
            invoke(profiled)
            profile.dump_stats(str(report_directory / f"cpu-{scale}.pstats"))
            row.update(_profile_summary(profile, workspace))
        row.update(semantic_ok=valid and len(fingerprints) == 1,
                   fingerprint=next(iter(fingerprints)) if len(fingerprints) == 1 else None)
        scales[scale] = row
    return {"schema_version": 1, "workload_id": WORKLOAD_ID, "probe": probe,
            "workload_fingerprint": workload_fingerprint(),
            "summary": {"semantic_ok": all(checks.values()) and all(row["semantic_ok"] for row in scales.values()),
                        "semantic_checks": checks, "scales": scales},
            "samples": samples,
            "environment": {"python_version": platform.python_version(),
                            "python_implementation": platform.python_implementation(),
                            "platform_system": platform.system(), "machine": platform.machine(),
                            "clock": "perf_counter_ns",
                            "isolation": {"cpus": 1, "memory_bytes": 268435456}}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", choices=PROBES, required=True)
    parser.add_argument("--workload", choices=(WORKLOAD_ID,), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    report = execute(args.probe, Path("/workspace"), output.parent)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
