"""Offline benchmark instrumentation; not imported by the product runtime."""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def percentile(values, p):
    if not values:
        return None
    if not 0 < p <= 1:
        raise ValueError("percentile must be in (0,1]")
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def distribution(values):
    return {
        "n": len(values),
        "mean": sum(values) / len(values) if values else None,
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def fingerprint():
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ("repair_workbench", "scripts")
        for p in sorted((ROOT / folder).glob("*.py"))
    }


class Observation:
    def __init__(self):
        self.lock = threading.Lock()
        self.transactions = []
        self.queries = []

    def record(self, kind, seconds, label=None):
        with self.lock:
            (self.transactions if kind == "transaction" else self.queries).append(
                (label, seconds)
            )

    def snapshot(self):
        from collections import Counter

        with self.lock:
            return {
                "connections": len(self.transactions),
                "transaction_seconds": sum(t for _, t in self.transactions),
                "transaction_latency_seconds": distribution(
                    [t for _, t in self.transactions]
                ),
                "queries": len(self.queries),
                "query_seconds": sum(t for _, t in self.queries),
                "query_kinds": dict(Counter(name for name, _ in self.queries)),
            }


class CursorObserver:
    def __init__(self, cursor, observation):
        self.cursor, self.observation = cursor, observation

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, query, args=None):
        start = time.perf_counter()
        try:
            return self.cursor.execute(query, args)
        finally:
            self.observation.record(
                "query", time.perf_counter() - start, query.split()[0].upper()
            )


def observed_store(settings):
    from repair_workbench.mysql_store import MySQLStore

    class ObservedStore(MySQLStore):
        def __init__(self, settings):
            super().__init__(settings)
            self.observation = Observation()

        @contextmanager
        def transaction(self):
            start = time.perf_counter()
            try:
                with super().transaction() as c:
                    yield CursorObserver(c, self.observation)
            finally:
                self.observation.record("transaction", time.perf_counter() - start)

    return ObservedStore(settings)


def api_process(settings, data, repo, image, port, output):
    from repair_workbench.mysql_store import DatabaseSettings
    from repair_workbench.mysql_service import MySQLService
    from repair_workbench.api import create_app
    import uvicorn

    os.environ.pop("DASHSCOPE_API_KEY", None)
    store = observed_store(DatabaseSettings(**settings))
    try:
        uvicorn.run(
            create_app(MySQLService(store, Path(data), [Path(repo)], image)),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="error",
        )
    finally:
        Path(output).write_text(json.dumps(store.observation.snapshot()))


def worker_process(settings, data, repo, image, output, ready, go, done, delay):
    import sys, resource

    sys.path.insert(0, str(ROOT / "tests"))
    from backend_worker_probe import FixtureModel
    from repair_workbench.mysql_store import DatabaseSettings
    from repair_workbench.durable_worker import Worker
    from pydantic import Field

    class DelayedFixtureModel(FixtureModel):
        latency: float = Field(default=delay)

        def _generate(self, *args, **kwargs):
            time.sleep(self.latency)
            return super()._generate(*args, **kwargs)

    os.environ.pop("DASHSCOPE_API_KEY", None)
    os.environ["LANGSMITH_TRACING"] = "false"
    root = Path(output).parent
    store = observed_store(DatabaseSettings(**settings))
    original = (Path(repo) / "pricing.py").read_text()
    worker = Worker(
        store,
        Path(data),
        [Path(repo)],
        image,
        model_factory=lambda: DelayedFixtureModel(
            original=original, barrier_dir=str(root)
        ),
        lease_seconds=15,
    )
    ready.set()
    go.wait()
    before = resource.getrusage(resource.RUSAGE_SELF)
    try:
        while not done.is_set():
            if not worker.run_once():
                done.wait(0.1)
    finally:
        after = resource.getrusage(resource.RUSAGE_SELF)
        Path(output).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "sql": store.observation.snapshot(),
                    "cpu_seconds": after.ru_utime
                    + after.ru_stime
                    - before.ru_utime
                    - before.ru_stime,
                    "exit_phase": "cooperative_stop",
                }
            )
        )


def check_cell(tasks, expected, worker_count):
    if len(tasks) != expected or len({r["id"] for r in tasks}) != expected:
        raise ValueError("missing_or_duplicate_task")
    for task in tasks:
        if (
            task["state"] != "completed"
            or (task.get("result") or {}).get("agent", {}).get("status") != "passed"
        ):
            raise ValueError("task_not_verified")
        if (
            task["generation"] != 1
            or task["calls_reserved"] != 3
            or task["candidates_reserved"] != 1
        ):
            raise ValueError("execution_or_budget_anomaly")
    if worker_count not in (1, 2, 4):
        raise ValueError("invalid worker count")
