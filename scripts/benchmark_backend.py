"""Bounded same-host backend measurement with a delayed fixture model; no API egress."""

from __future__ import annotations
import argparse
import dataclasses
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend_benchmark_support import (
    api_process,
    worker_process,
    distribution,
    fingerprint,
    check_cell,
    observed_store,
)
from repair_workbench.mysql_store import DatabaseSettings


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def until(check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("condition_timeout")


def prepare_source(output):
    source = output / "source"
    shutil.copytree(
        ROOT / "tests/fixtures/broken_discount",
        source,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    for args in [
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Offline benchmark",
            "-c",
            "user.email=benchmark@localhost",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "Pinned fixture",
        ),
    ]:
        subprocess.run(
            ["git", "-C", str(source), *args], env=env, check=True, capture_output=True
        )
    return source


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_api(ctx, settings, data, source, image, cell):
    import httpx

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server = ctx.Process(
        target=api_process,
        args=(
            dataclasses.asdict(settings),
            str(data),
            str(source),
            image,
            port,
            str(cell / "api-observation.json"),
        ),
    )
    server.start()

    def ready():
        if not server.is_alive():
            raise RuntimeError("api_process_exited")
        try:
            return httpx.get(url + "/api/config", timeout=0.5).status_code == 200
        except httpx.TransportError:
            return False

    try:
        until(ready)
    except BaseException:
        server.kill()
        server.join(5)
        raise
    return server, url


def stop_process(process):
    if process.is_alive():
        os.kill(process.pid, signal.SIGINT)
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
        raise RuntimeError("forced_process_cleanup")
    if process.exitcode != 0:
        raise RuntimeError("process_exit_error")


def read_tasks(store, ids):
    with store.transaction() as c:
        c.execute(
            "SELECT * FROM repair_tasks WHERE id IN ("
            + ",".join(["%s"] * len(ids))
            + ")",
            ids,
        )
        return [store.decode(row) for row in c.fetchall()]


def monitor_resources(pids, stop, output):
    rows = []
    while not stop.is_set():
        sample = {"monotonic": time.monotonic()}
        try:
            result = subprocess.run(
                ["ps", "-p", ",".join(map(str, pids)), "-o", "pid=", "-o", "rss="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            entries = [line.split() for line in result.stdout.splitlines()]
            sample["process_rss_bytes"] = {pid: int(rss) * 1024 for pid, rss in entries}
        except (OSError, ValueError, subprocess.SubprocessError):
            sample["rss_unavailable"] = True
        rows.append(sample)
        stop.wait(0.5)
    save(output, rows)


def cell_run(settings, source, output, image, count, workers, delay):
    import httpx

    ctx = multiprocessing.get_context("spawn")
    cell = output
    cell.mkdir()
    data = cell / "data"
    store = observed_store(settings)
    server = None
    children = []
    ids = []
    go = ctx.Event()
    done = ctx.Event()
    sampler_stop = threading.Event()
    samples = []
    resource_stop = threading.Event()
    resource_thread = None
    http_thread = None
    start_fingerprint = fingerprint()
    result = {
        "workers": workers,
        "tasks_planned": count,
        "state": "preparing",
        "real_model_calls": 0,
    }
    save(cell / "result.json", result)
    try:
        # The dedicated test DB must not have other active workloads.
        with store.transaction() as c:
            c.execute(
                "SELECT COUNT(*) AS n FROM repair_tasks WHERE state IN ('queued','running','cancelling')"
            )
            if c.fetchone()["n"]:
                raise RuntimeError("dedicated_test_database_busy")
        server, url = start_api(ctx, settings, data, source, image, cell)
        ready = []
        for index in range(workers):
            folder = cell / f"worker-{index}"
            folder.mkdir()
            flag = ctx.Event()
            ready.append(flag)
            process = ctx.Process(
                target=worker_process,
                args=(
                    dataclasses.asdict(settings),
                    str(data),
                    str(source),
                    image,
                    str(folder / "observation.json"),
                    flag,
                    go,
                    done,
                    delay,
                ),
            )
            process.start()
            children.append(process)
        until(lambda: all(flag.is_set() for flag in ready))
        with httpx.Client(base_url=url, timeout=15) as client:
            creates = []
            for index in range(count):
                body = {
                    "repo": str(source),
                    "revision": "HEAD",
                    "task": f"Fix inclusive discount threshold; retain other behavior. Workload {index}.",
                    "allowed_paths": ["pricing.py"],
                }
                started = time.perf_counter()
                response = client.post(
                    "/api/tasks",
                    headers={
                        "X-Workbench-Request": "1",
                        "Idempotency-Key": uuid4().hex,
                    },
                    json=body,
                )
                creates.append(time.perf_counter() - started)
                response.raise_for_status()
                ids.append(response.json()["id"])
        save(cell / "task-ids.json", ids)

        def sample_http():
            with httpx.Client(base_url=url, timeout=15) as client:
                i = 0
                while not sampler_stop.is_set():
                    kind = ("detail", "events", "list")[i % 3]
                    path = (
                        "/api/tasks/" + ids[i % len(ids)]
                        if kind == "detail"
                        else "/api/tasks/" + ids[i % len(ids)] + "/events?follow=false"
                        if kind == "events"
                        else "/api/tasks"
                    )
                    t = time.perf_counter()
                    try:
                        response = client.get(path)
                        samples.append(
                            {
                                "endpoint": kind,
                                "seconds": time.perf_counter() - t,
                                "status": response.status_code,
                                "bytes": len(response.content),
                            }
                        )
                    except httpx.TransportError:
                        samples.append(
                            {
                                "endpoint": kind,
                                "seconds": time.perf_counter() - t,
                                "status": None,
                                "bytes": None,
                            }
                        )
                    i += 1
                    sampler_stop.wait(0.2)

        http_thread = threading.Thread(target=sample_http)
        http_thread.start()
        resource_thread = threading.Thread(
            target=monitor_resources,
            args=(
                [server.pid] + [p.pid for p in children],
                resource_stop,
                cell / "resources.json",
            ),
        )
        resource_thread.start()
        started = time.monotonic()
        started_utc = datetime.now(timezone.utc).isoformat()
        go.set()
        result.update(state="running", task_ids=ids, started_utc=started_utc)
        save(cell / "result.json", result)
        deadline = started + 300
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("cell_wall_limit")
            rows = read_tasks(store, ids)
            if any(not p.is_alive() for p in children):
                raise RuntimeError("worker_exited")
            if all(
                row["state"] in {"completed", "cancelled", "error", "interrupted"}
                for row in rows
            ):
                break
            time.sleep(0.2)
        elapsed = time.monotonic() - started
        check_cell(rows, count, workers)
        queue = []
        execution = []
        turnaround = []
        docker_seconds = []
        verified = []
        for row in rows:
            attempts = store.records(row["id"], "task_attempts")
            if len(attempts) != 1:
                raise RuntimeError("duplicate_or_recovered_execution")
            parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
            queue.append(
                (
                    parse(attempts[0]["started_at"]) - parse(row["created_at"])
                ).total_seconds()
            )
            execution.append(
                (
                    parse(row["updated_at"]) - parse(attempts[0]["started_at"])
                ).total_seconds()
            )
            turnaround.append(
                (parse(row["updated_at"]) - parse(row["created_at"])).total_seconds()
            )
            for run in store.records(row["id"], "test_runs"):
                report = data / run["artifact_path"] / "reports/result.json"
                docker_seconds.append(json.loads(report.read_text())["elapsed_seconds"])
            receipts = store.records(row["id"], "model_calls")
            if len(receipts) != 3 or any(r["state"] != "completed" for r in receipts):
                raise RuntimeError("invalid_model_receipts")
            verified.append(
                {
                    "task_id": row["id"],
                    "generation": row["generation"],
                    "calls_reserved": row["calls_reserved"],
                    "candidates_reserved": row["candidates_reserved"],
                    "model_receipts": len(receipts),
                    "status": row["state"],
                }
            )
        result.update(
            state="completed",
            elapsed_seconds=elapsed,
            tasks_completed=count,
            throughput_tasks_per_minute=60 * count / elapsed,
            queue_wait_seconds=distribution(queue),
            execution_seconds=distribution(execution),
            turnaround_seconds=distribution(turnaround),
            docker_run_seconds=distribution(docker_seconds),
            create_http_seconds=distribution(creates),
            task_invariants=verified,
            actual_synthetic_model_calls=count * 3,
        )
    except BaseException as exc:
        result.update(state="failed", reason=type(exc).__name__, detail=str(exc))
        raise
    finally:
        done.set()
        go.set()
        sampler_stop.set()
        resource_stop.set()
        if http_thread:
            http_thread.join(20)
        if resource_thread:
            resource_thread.join(5)
        for process in children:
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(5)
                result["cleanup_failed"] = True
        if server:
            try:
                stop_process(server)
            except RuntimeError:
                result["cleanup_failed"] = True
        if result["state"] != "completed":
            for identity in ids:
                store.cancel(identity)
        result["http_samples"] = samples
        result["http_latency_seconds"] = {
            kind: distribution([s["seconds"] for s in samples if s["endpoint"] == kind])
            for kind in ("detail", "events", "list")
        }
        result["http_error_count"] = sum(s["status"] != 200 for s in samples)
        result["driver_sql"] = store.observation.snapshot()
        result["worker_observations"] = [
            json.loads((cell / f"worker-{i}/observation.json").read_text())
            for i in range(workers)
            if (cell / f"worker-{i}/observation.json").exists()
        ]
        if (cell / "api-observation.json").exists():
            result["api_sql"] = json.loads((cell / "api-observation.json").read_text())
        if (cell / "resources.json").exists():
            resources = json.loads((cell / "resources.json").read_text())
            result["rss_samples"] = len(resources)
            result["observed_api_worker_rss_max_bytes"] = max(
                (sum(s.get("process_rss_bytes", {}).values()) for s in resources),
                default=None,
            )
        result["code_unchanged"] = fingerprint() == start_fingerprint
        save(cell / "result.json", result)
    if (
        result.get("cleanup_failed")
        or result["http_error_count"]
        or not result["code_unchanged"]
    ):
        raise RuntimeError("stop_after_anomaly")
    return result


def run(args):
    settings = DatabaseSettings.load(args.db_env)
    if settings.database not in {"repair_workbench", "repair_workbench_test"}:
        raise ValueError("dedicated database configuration required")
    settings = dataclasses.replace(settings, database="repair_workbench_test")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = prepare_source(output)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
    ).strip()
    plan = {
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": fingerprint(),
        "image_id": image_id,
        "worker_order": [1, 2, 4, 4, 2, 1],
        "tasks_per_cell": 8,
        "cells": 6,
        "max_tasks": 48,
        "fixture_delay_per_model_call_seconds": 0.25,
        "model": "offline deterministic FixtureModel",
        "max_synthetic_model_calls": 576,
        "expected_synthetic_calls": 144,
        "real_model_calls": 0,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "queue_wait": "DB first claim minus DB task creation; workers prewarmed before admission",
        "throughput": "verified completions / monotonic batch time from worker-release to final polling observation",
        "http_load": "one persistent client, sequential detail/events/list, 0.2 second delay after each response; not an open-loop load test",
        "scope": "same-host controlled fixture; no inference or universal capacity claim; no concurrent test-DB workload allowed",
        "resource_scope": "sampled API+worker RSS; excludes Docker test containers, MySQL and whole-host memory",
        "state": "planned",
    }
    save(output / "plan.json", plan)
    results = []
    try:
        for index, count in enumerate(plan["worker_order"]):
            result = cell_run(
                settings,
                source,
                output / f"cell-{index + 1}-{count}-workers",
                image_id,
                8,
                count,
                0.25,
            )
            results.append(result)
            save(
                output / "results.json",
                {"state": "running", "plan": plan, "cells": results},
            )
            print(
                json.dumps(
                    {
                        "cell": index + 1,
                        "workers": count,
                        "completed": result["tasks_completed"],
                        "seconds": round(result["elapsed_seconds"], 3),
                        "tasks_per_minute": round(
                            result["throughput_tasks_per_minute"], 3
                        ),
                    }
                ),
                flush=True,
            )
        save(
            output / "results.json",
            {"state": "completed", "plan": plan, "cells": results},
        )
    except BaseException:
        save(
            output / "results.json",
            {"state": "stopped", "plan": plan, "cells": results},
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-env", type=Path, default=ROOT / "runtime/mysql-backend/client.env"
    )
    parser.add_argument("--image", default="repair-workbench-test:20261014-pinned")
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
