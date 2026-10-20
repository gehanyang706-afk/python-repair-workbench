"""Export bounded local fault evidence without database credentials or source paths."""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_workbench.mysql_store import DatabaseSettings, MySQLStore
from repair_workbench.durable_worker import snapshot_digest


def audit(evidence, log, env):
    settings = replace(DatabaseSettings.load(env), database="repair_workbench_test")
    store = MySQLStore(settings)
    text = log.read_text()
    match = re.search(r"Ran (\d+) tests in ([\d.]+)s\n\nOK\s*$", text)
    if not match:
        raise ValueError("complete zero-skip passing regression required")
    cases = []
    for folder in sorted(evidence.glob("test_*")):
        with store.transaction() as c:
            c.execute(
                "SELECT id FROM repair_tasks WHERE JSON_UNQUOTE(JSON_EXTRACT(request,'$.repo'))=%s ORDER BY created_at",
                (str(folder / "source"),),
            )
            identities = [row["id"] for row in c.fetchall()]
        tasks = []
        for identity in identities:
            row = store.get(identity)
            calls = store.records(identity, "model_calls")
            candidates = store.records(identity, "repair_candidates")
            stages = store.records(identity, "stage_artifacts")
            attempts = store.records(identity, "task_attempts")
            events = []
            cursor = 0
            while True:
                page = store.events(identity, cursor)
                if not page:
                    break
                events += page
                cursor = page[-1]["id"]
            materialized = []
            for candidate in candidates:
                consistent = None
                if candidate["artifact_path"]:
                    try:
                        consistent = (
                            snapshot_digest(
                                folder / "data" / candidate["artifact_path"]
                            )
                            == candidate["artifact_sha"]
                        )
                    except (OSError, ValueError):
                        consistent = False
                materialized.append(
                    {
                        "number": candidate["number"],
                        "generation": candidate["generation"],
                        "state": candidate["state"],
                        "artifact_sha256": candidate["artifact_sha"],
                        "artifact_matches_receipt": consistent,
                    }
                )
            tasks.append(
                {
                    "id": identity,
                    "state": row["state"],
                    "generation": row["generation"],
                    "reason": row["error_type"],
                    "calls_reserved": row["calls_reserved"],
                    "candidates_reserved": row["candidates_reserved"],
                    "model_receipts": [
                        {
                            "number": call["number"],
                            "generation": call["generation"],
                            "state": call["state"],
                            "usage": call["usage_json"],
                        }
                        for call in calls
                    ],
                    "candidates": materialized,
                    "confirmed_stages": [
                        {
                            "kind": r["kind"],
                            "generation": r["generation"],
                            "artifact_sha256": r["artifact_sha"],
                        }
                        for r in stages
                    ],
                    "attempts": [
                        {"generation": r["generation"], "state": r["state"]}
                        for r in attempts
                    ],
                    "event_count": len(events),
                    "events": sorted(set(e["event"] for e in events)),
                }
            )
        barriers = []
        for barrier in folder.rglob("barrier.json"):
            barriers.append(json.loads(barrier.read_text()))
        stale = []
        for barrier in folder.rglob("stale-writes.json"):
            stale += json.loads(barrier.read_text())
        cases.append(
            {
                "test": folder.name,
                "tasks": tasks,
                "actual_crash_barriers": barriers,
                "stale_writes_rejected": stale,
            }
        )
    if len(cases) != 16:
        raise ValueError("expected sixteen MySQL fault cases")
    return {
        "date": "2026-10-20",
        "source_base_commit": "f980087b97e641b3a39f924609dab186ecbe31aa",
        "schema_sha256":hashlib.sha256((ROOT/'repair_workbench/migrations/001_mysql.sql').read_bytes()).hexdigest(),
        "fault_test_sha256":{name:hashlib.sha256((ROOT/'tests'/name).read_bytes()).hexdigest()
                              for name in ('test_mysql_backend.py','backend_worker_probe.py')},
        "runtime_source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "repair_workbench").glob("*.py"))
        },
        "mysql_image_digest": "sha256:0f51e63e537a1f176423fa03ef559aabe6fa4372a53160a44bc7e75da7d15364",
        "dependency": "PyMySQL==1.2.3",
        "remote_ci": "configured_not_executed",
        "backend": "MySQL 8.4.11 / PyMySQL 1.2.3",
        "regression": {
            "tests": int(match[1]),
            "seconds": float(match[2]),
            "skipped": 0,
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        },
        "real_model_calls": 0,
        "model": "deterministic offline fixture with synthetic usage",
        "scope": "same-host independent processes; original single-Agent repair algorithm unchanged",
        "cases": cases,
        "limitations": [
            "No paid-model repair-quality conclusion",
            "No throughput benchmark",
            "No remote CI execution",
            "No arbitrary graph checkpoint resume",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument(
        "--db-env", type=Path, default=Path("runtime/mysql-backend/client.env")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(
            audit(args.evidence.resolve(), args.log, args.db_env),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print("Sanitized offline acceptance exported.")
