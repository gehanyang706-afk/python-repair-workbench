"""Read-only paired HTTP/SQL probe; verifies a constant task payload across samples."""

import argparse
import dataclasses
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark_backend import start_api, stop_process, save
from backend_benchmark_support import observed_store, distribution, fingerprint
from repair_workbench.mysql_store import DatabaseSettings


def payload_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def run(args):
    import httpx

    if not 1 <= args.samples <= 100:
        raise ValueError("samples must be 1-100")
    settings = dataclasses.replace(
        DatabaseSettings.load(args.db_env), database="repair_workbench_test"
    )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    store = observed_store(settings)
    with store.transaction() as c:
        c.execute(
            "SELECT COUNT(*) AS n FROM repair_tasks WHERE state IN ('queued','running','cancelling')"
        )
        if c.fetchone()["n"]:
            raise RuntimeError("database_has_active_work")
    expected = store.list()
    if not expected:
        raise RuntimeError("history_required")
    identity = expected[0]["id"]
    digest = payload_hash(expected)
    source = args.source.resolve(strict=True)
    server, url = start_api(
        multiprocessing.get_context("spawn"),
        settings,
        output / "unused-data",
        source,
        args.image,
        output,
    )
    rows = []
    try:
        with httpx.Client(base_url=url, timeout=15) as client:
            for endpoint in ("list", "detail"):
                path = "/api/tasks" if endpoint == "list" else "/api/tasks/" + identity
                # Warmup excluded from the latency sample.
                client.get(path).raise_for_status()
                for _ in range(args.samples):
                    before = store.observation.snapshot()
                    start = time.perf_counter()
                    direct = store.list() if endpoint == "list" else store.get(identity)
                    direct_seconds = time.perf_counter() - start
                    after = store.observation.snapshot()
                    start = time.perf_counter()
                    response = client.get(path)
                    http_seconds = time.perf_counter() - start
                    response.raise_for_status()
                    if endpoint == "list" and (
                        payload_hash(direct) != digest
                        or payload_hash(response.json()) != digest
                    ):
                        raise RuntimeError("history_changed_during_probe")
                    if endpoint == "detail" and payload_hash(direct) != payload_hash(
                        response.json()
                    ):
                        raise RuntimeError("detail_changed_during_probe")
                    rows.append(
                        {
                            "endpoint": endpoint,
                            "direct_seconds": direct_seconds,
                            "http_seconds": http_seconds,
                            "direct_connections": after["connections"]
                            - before["connections"],
                            "direct_queries": after["queries"] - before["queries"],
                            "response_bytes": len(response.content),
                        }
                    )
        report = {
            "state": "completed",
            "source_sha256": fingerprint(),
            "samples_per_endpoint": args.samples,
            "history_rows": len(expected),
            "task_payload_sha256": digest,
            "detail_task_id": identity,
            "real_model_calls": 0,
            "samples": rows,
            "endpoints": {
                endpoint: {
                    "http_seconds": distribution(
                        [s["http_seconds"] for s in rows if s["endpoint"] == endpoint]
                    ),
                    "direct_seconds": distribution(
                        [s["direct_seconds"] for s in rows if s["endpoint"] == endpoint]
                    ),
                    "connections_per_request": sorted(
                        set(
                            s["direct_connections"]
                            for s in rows
                            if s["endpoint"] == endpoint
                        )
                    ),
                    "queries_per_request": sorted(
                        set(
                            s["direct_queries"]
                            for s in rows
                            if s["endpoint"] == endpoint
                        )
                    ),
                }
                for endpoint in ("list", "detail")
            },
        }
    finally:
        stop_process(server)
    save(output / "results.json", report)
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k in ("history_rows", "endpoints", "state")
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-env", type=Path, default=ROOT / "runtime/mysql-backend/client.env"
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--image", default="repair-workbench-test:20261014-pinned")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
