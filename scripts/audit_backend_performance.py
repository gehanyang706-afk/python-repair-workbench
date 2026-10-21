"""Validate and sanitize fixed offline throughput and same-payload read measurements."""

import argparse
import json
import hashlib
import re
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summarize(matrix, before, after, history_counts=None):
    if history_counts is not None and (
        len(history_counts) != 6
        or any(type(n) is not int or not 0 <= n <= 100 for n in history_counts)
    ):
        raise ValueError("invalid_history_observation")
    if matrix["state"] != "completed" or len(matrix["cells"]) != 6:
        raise ValueError("incomplete_matrix")
    order = [cell["workers"] for cell in matrix["cells"]]
    if order != [1, 2, 4, 4, 2, 1]:
        raise ValueError("matrix_order_changed")
    if any(
        cell["state"] != "completed"
        or cell["tasks_completed"] != 8
        or cell["http_error_count"]
        or not cell["code_unchanged"]
        or cell.get("cleanup_failed")
        for cell in matrix["cells"]
    ):
        raise ValueError("invalid_matrix_cell")
    seen = set()
    for cell in matrix["cells"]:
        receipts = cell.get("task_invariants", [])
        identities = {r["task_id"] for r in receipts}
        if len(receipts) != 8 or len(identities) != 8 or seen & identities:
            raise ValueError("missing_or_reused_task_receipts")
        seen.update(identities)
        if any(
            r["generation"] != 1
            or r["calls_reserved"] != 3
            or r["candidates_reserved"] != 1
            or r["model_receipts"] != 3
            or r["status"] != "completed"
            for r in receipts
        ):
            raise ValueError("task_receipt_anomaly")
        if cell["actual_synthetic_model_calls"] != 24 or not math.isclose(
            cell["throughput_tasks_per_minute"],
            480 / cell["elapsed_seconds"],
            rel_tol=1e-10,
        ):
            raise ValueError("throughput_denominator_anomaly")
    if before["state"] != "completed" or after["state"] != "completed":
        raise ValueError("incomplete_read_probe")
    if (
        before["task_payload_sha256"] != after["task_payload_sha256"]
        or before["history_rows"] != after["history_rows"]
    ):
        raise ValueError("payload_not_comparable")
    if before["samples_per_endpoint"] != 30 or after["samples_per_endpoint"] != 30:
        raise ValueError("sample_size_changed")
    changed = {
        name
        for name in before["source_sha256"].keys() | after["source_sha256"].keys()
        if before["source_sha256"].get(name) != after["source_sha256"].get(name)
    }
    if changed != {"repair_workbench/mysql_store.py"}:
        raise ValueError("unexpected_product_or_instrumentation_change")
    if before["endpoints"]["list"]["queries_per_request"] != [101] or after[
        "endpoints"
    ]["list"]["queries_per_request"] != [1]:
        raise ValueError("query_count_contract_failed")
    cells = []
    for number, cell in enumerate(matrix["cells"], 1):
        cells.append(
            {
                k: cell[k]
                for k in (
                    "workers",
                    "tasks_completed",
                    "elapsed_seconds",
                    "throughput_tasks_per_minute",
                    "queue_wait_seconds",
                    "execution_seconds",
                    "turnaround_seconds",
                    "docker_run_seconds",
                    "create_http_seconds",
                    "http_latency_seconds",
                    "http_error_count",
                    "observed_api_worker_rss_max_bytes",
                    "rss_samples",
                    "actual_synthetic_model_calls",
                )
            }
        )
        cells[-1]["cell"] = number
        cells[-1]["history_rows_at_release_reconstructed"] = (
            history_counts[number - 1] if history_counts is not None else None
        )
        cells[-1]["worker_connections"] = sum(
            w["sql"]["connections"] for w in cell["worker_observations"]
        )
        cells[-1]["worker_cpu_seconds"] = sum(
            w["cpu_seconds"] for w in cell["worker_observations"]
        )
        cells[-1]["api_sql"] = cell["api_sql"]
        cells[-1]["all_tasks_generation_1"] = all(
            r["generation"] == 1 for r in cell["task_invariants"]
        )
    groups = []
    for workers in (1, 2, 4):
        matching = [cell for cell in cells if cell["workers"] == workers]
        total = sum(cell["elapsed_seconds"] for cell in matching)
        groups.append(
            {
                "workers": workers,
                "runs": 2,
                "tasks": 16,
                "pooled_throughput_tasks_per_minute": 960 / total,
                "per_run_throughput_tasks_per_minute": [
                    cell["throughput_tasks_per_minute"] for cell in matching
                ],
                "per_run_queue_p95_seconds": [
                    cell["queue_wait_seconds"]["p95"] for cell in matching
                ],
                "per_run_execution_p95_seconds": [
                    cell["execution_seconds"]["p95"] for cell in matching
                ],
                "observed_api_worker_rss_max_bytes": max(
                    cell["observed_api_worker_rss_max_bytes"] for cell in matching
                ),
            }
        )
    return {
        "date": "2026-10-21",
        "real_model_calls": 0,
        "fixture": "same deterministic discount fixture, repeated workload; not 48 distinct bugs",
        "baseline_product_commit": matrix["plan"]["source_commit"],
        "image_id": matrix["plan"]["image_id"],
        "matrix_application_source_sha256": matrix["plan"]["source_sha256"],
        "groups": groups,
        "cells": cells,
        "task_count": 48,
        "synthetic_model_calls": sum(c["actual_synthetic_model_calls"] for c in cells),
        "read_probe": {
            "history_rows": before["history_rows"],
            "samples_per_endpoint": 30,
            "same_complete_payload": True,
            "payload_sha256": before["task_payload_sha256"],
            "changed_source": sorted(changed),
            "before": before["endpoints"],
            "after": after["endpoints"],
            "before_source_sha256": before["source_sha256"][
                "repair_workbench/mysql_store.py"
            ],
            "after_source_sha256": after["source_sha256"][
                "repair_workbench/mysql_store.py"
            ],
        },
        "limits": [
            "Matrix history grew during workload; scaling is descriptive, not a strict causal capacity comparison.",
            "Concurrent-query client is closed-loop, not a peak QPS test.",
            "RSS covers sampled API and workers only; Docker, MySQL and whole-host memory excluded.",
            "One host, other applications present, two runs per worker count; no statistical stability claim.",
            "Worker matrix was not rerun after the list-query fix.",
            "No Redis component, model service, new repair-quality result or remote CI execution.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--history-counts", type=Path)
    parser.add_argument("--regression-log", type=Path)
    args = parser.parse_args()
    data = summarize(
        *(json.loads(p.read_text()) for p in (args.matrix, args.before, args.after)),
        history_counts=json.loads(args.history_counts.read_text())
        if args.history_counts
        else None,
    )
    if args.regression_log:
        log = args.regression_log.read_text()
        match = re.search(r"Ran (\d+) tests in ([\d.]+)s\n\nOK\s*$", log)
        if not match:
            raise ValueError("zero_skip_regression_required")
        data["regression"] = {
            "tests": int(match[1]),
            "seconds": float(match[2]),
            "skipped": 0,
            "log_sha256": hashlib.sha256(args.regression_log.read_bytes()).hexdigest(),
        }
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(
        "Validated 48 fixture tasks and constant-payload read comparison; no raw task data exported."
    )
