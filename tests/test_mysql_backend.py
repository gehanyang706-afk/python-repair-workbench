"""Real MySQL + independent-process ownership and crash acceptance."""

import dataclasses
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest
from uuid import uuid4

from repair_workbench.mysql_store import (
    DatabaseSettings,
    MySQLStore,
    IdempotencyConflict,
)

ROOT = Path(__file__).resolve().parents[1]
ENABLED = os.environ.get("RUN_MYSQL_TESTS") == "1"


def wait_until(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached before timeout")


@unittest.skipUnless(
    ENABLED, "requires explicitly enabled dedicated MySQL acceptance database"
)
class MySQLBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = DatabaseSettings.load(
            os.environ.get("REPAIR_MYSQL_TEST_ENV", "runtime/mysql-backend/client.env")
        )
        if settings.database not in {"repair_workbench", "repair_workbench_test"}:
            raise ValueError("dedicated acceptance database required")
        cls.settings = dataclasses.replace(settings, database="repair_workbench_test")
        cls.store = MySQLStore(cls.settings)
        cls.store.migrate()
        cls.evidence = ROOT / "runtime" / ("backend-acceptance-" + uuid4().hex[:10])
        cls.evidence.mkdir()
        print("MySQL evidence:", cls.evidence, flush=True)

    def setUp(self):
        from test_agent import fixture

        self.root = self.evidence / self._testMethodName
        self.root.mkdir()
        fixture(self.root)
        self.source = self.root / "source"
        self.data = self.root / "data"
        self.children = []
        self.ctx = multiprocessing.get_context("spawn")
        self.request = {
            "repo": str(self.source),
            "revision": subprocess.check_output(
                ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
            ).strip(),
            "task": "Fix the inclusive discount threshold without changing other behavior.",
            "allowed_paths": ["pricing.py"],
            "max_attempts": 3,
            "max_model_calls": 12,
            "deadline": 180,
            "test_timeout": 30,
            "profile": "v2",
        }
        self.task_ids = []

    def tearDown(self):
        for process in self.children:
            if process.is_alive():
                os.kill(process.pid, signal.SIGCONT)
                process.kill()
            process.join(8)
        # Cancel only tasks explicitly created by this test; preserve receipts.
        for task_id in self.task_ids:
            row = self.store.get(task_id)
            if row["state"] in {"queued", "running", "cancelling"}:
                self.store.cancel(task_id)
                if self.store.get(task_id)["state"] == "cancelling":
                    # Normal expired-lease claim for cleanup, never an owner edit.
                    lease = wait_until(
                        lambda: self.store.claim(uuid4().hex, 3, task_id), 15
                    )
                    self.store.finish(
                        lease, {"reason": "acceptance_cleanup"}, state="cancelled"
                    )
        for task_id in self.task_ids:
            rows = self.store.records(task_id, "execution_resources")
            for row in rows:
                subprocess.run(
                    ["docker", "rm", "-f", row["container_name"]],
                    capture_output=True,
                    timeout=15,
                )

    def task(self, key=None):
        row = self.store.create(self.request, key or uuid4().hex)
        self.task_ids.append(row["id"])
        return row

    def spawn(self, target, *args):
        process = self.ctx.Process(target=target, args=args)
        process.start()
        self.children.append(process)
        return process

    def worker(self, mode="normal", name=None, lease=3):
        from backend_worker_probe import run_probe

        barrier = self.root / (name or mode)
        barrier.mkdir()
        process = self.spawn(
            run_probe,
            dataclasses.asdict(self.settings),
            str(self.data),
            str(self.source),
            os.environ.get(
                "REPAIR_TEST_IMAGE", "repair-workbench-test:20261014-pinned"
            ),
            str(barrier),
            mode,
            lease,
            self.task_ids[-1],
        )
        return process, barrier

    def expire(self, task_id):
        def expired():
            with self.store.transaction() as c:
                c.execute(
                    "SELECT lease_until<=UTC_TIMESTAMP(6) AS expired FROM repair_tasks WHERE id=%s",
                    (task_id,),
                )
                return bool(c.fetchone()["expired"])

        wait_until(expired, 15)

    def test_concurrent_idempotent_creation_and_conflict(self):
        from backend_worker_probe import create_probe

        gate = self.root / "go"
        key = uuid4().hex
        outputs = []
        for i in range(4):
            output = self.root / f"created-{i}.json"
            outputs.append(output)
            self.spawn(
                create_probe,
                dataclasses.asdict(self.settings),
                str(gate),
                str(output),
                self.request,
                key,
            )
        gate.touch()
        wait_until(lambda: all(p.exists() for p in outputs))
        rows = [json.loads(p.read_text()) for p in outputs]
        self.assertEqual(len({r["pid"] for r in rows}), 4)
        self.assertEqual(len({r["id"] for r in rows}), 1)
        identity = rows[0]["id"]
        self.task_ids.append(identity)
        self.assertEqual(self.store.create(self.request, key)["id"], identity)
        with self.assertRaises(IdempotencyConflict):
            self.store.create({**self.request, "task": "different"}, key)
        self.assertEqual(len(self.store.events(identity)), 1)

    def test_idempotent_retry_keeps_original_commit_after_head_moves(self):
        from repair_workbench.mysql_service import MySQLService

        service = MySQLService(self.store, self.data, [self.source], "unused")
        request = {**self.request, "revision": "HEAD"}
        key = uuid4().hex
        first = service.submit(request, key)
        self.task_ids.append(first["id"])
        source = self.source / "pricing.py"
        source.write_text(source.read_text() + "\n# advanced input revision\n")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Head moved",
            ],
            check=True,
            capture_output=True,
        )
        repeated = service.submit(request, key)
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["request"]["revision"], repeated["request"]["revision"])
        fresh = service.submit(request, uuid4().hex)
        self.task_ids.append(fresh["id"])
        self.assertNotEqual(first["request"]["revision"], fresh["request"]["revision"])

    def test_process_competition_has_one_claim(self):
        from backend_worker_probe import claim_probe

        row = self.task()
        gate = self.root / "go"
        outputs = []
        for i in range(4):
            output = self.root / f"claim-{i}.json"
            outputs.append(output)
            self.spawn(
                claim_probe,
                dataclasses.asdict(self.settings),
                str(gate),
                str(output),
                row["id"],
            )
        gate.touch()
        wait_until(lambda: all(p.exists() for p in outputs))
        claims = [json.loads(p.read_text()) for p in outputs]
        self.assertEqual(sum(r is not None for r in claims), 1)
        self.assertEqual(self.store.get(row["id"])["generation"], 1)
        self.assertEqual(len(self.store.records(row["id"], "task_attempts")), 1)

    def test_natural_lease_takeover_fences_old_process_all_writes(self):
        row = self.task()
        old, barrier = self.worker("lease")
        wait_until(lambda: (barrier / "claimed.json").exists())
        info = json.loads((barrier / "claimed.json").read_text())
        self.assertEqual(info["pid"], old.pid)
        os.kill(old.pid, signal.SIGSTOP)
        self.expire(row["id"])
        new, new_barrier = self.worker("lease", "replacement")
        wait_until(lambda: (new_barrier / "claimed.json").exists())
        self.assertEqual(self.store.get(row["id"])["generation"], 2)
        # Resume A only after B's real claim. Never edit lease_owner in SQL.
        os.kill(old.pid, signal.SIGCONT)
        (barrier / "release").touch()
        wait_until(lambda: (barrier / "stale-writes.json").exists())
        self.assertEqual(
            len(json.loads((barrier / "stale-writes.json").read_text())), 9
        )
        old.join(10)
        self.assertEqual(old.exitcode, 0)
        # B's hook uses the same stale-write probe; kill after fencing evidence.
        new.kill()
        new.join(10)
        self.expire(row["id"])
        final, _ = self.worker("normal", "final")
        final.join(50)
        self.assertEqual(final.exitcode, 0)
        self.assertEqual(
            self.store.get(row["id"])["result"]["agent"]["status"], "passed"
        )

    def test_actual_candidate_crash_recovers_without_model_replay(self):
        row = self.task()
        first, barrier = self.worker("candidate_crash")
        wait_until(lambda: (barrier / "barrier.json").exists())
        candidates = self.store.records(row["id"], "repair_candidates")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["state"], "ready")
        candidate_path = self.data / candidates[0]["artifact_path"]
        before = {
            str(p.relative_to(candidate_path)): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in candidate_path.rglob("*")
            if p.is_file()
        }
        first.kill()
        first.join(10)
        self.assertEqual(first.exitcode, -signal.SIGKILL)
        self.expire(row["id"])
        second, recovery = self.worker("normal", "recovery")
        second.join(50)
        self.assertEqual(second.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["state"], "completed", finished)
        self.assertEqual(finished["result"]["agent"]["status"], "passed")
        self.assertEqual(finished["calls_reserved"], 2)
        self.assertEqual(finished["candidates_reserved"], 1)
        self.assertFalse((recovery / "fake-calls.jsonl").exists())
        after = {
            str(p.relative_to(candidate_path)): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in candidate_path.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertIn(
            "candidate_recovered", [e["event"] for e in self.store.events(row["id"])]
        )

    def test_unknown_model_response_keeps_budget_and_never_reissues(self):
        row = self.task()
        first, barrier = self.worker("unknown")
        wait_until(lambda: (barrier / "model-entered").exists())
        self.assertEqual(
            self.store.records(row["id"], "model_calls")[0]["state"], "started"
        )
        first.kill()
        first.join(10)
        self.expire(row["id"])
        second, recovery = self.worker("normal", "recovery")
        second.join(40)
        self.assertEqual(second.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["state"], "interrupted")
        self.assertEqual(finished["error_type"], "model_response_unknown")
        self.assertEqual(finished["calls_reserved"], 1)
        receipts = self.store.records(row["id"], "model_calls")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["state"], "unknown")
        self.assertIsNone(receipts[0]["usage_json"])
        self.assertFalse((recovery / "fake-calls.jsonl").exists())

    def test_worker_crash_cleans_identified_request_child_without_reissue(self):
        row = self.task()
        first, barrier = self.worker("unknown_process")
        wait_until(lambda: (barrier / "request-child").exists())
        pid = int((barrier / "request-child").read_text())
        token = next(
            e["request_process_id"]
            for e in self.store.events(row["id"])
            if e["event"] == "model_worker_started" and e["pid"] == pid
        )

        def cleanup_child():
            args = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True
            ).stdout
            if "--repair-request-id " + token in args:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup_child)
        first.kill()
        first.join(10)
        os.kill(pid, 0)  # The SDK request boundary really outlived its parent.
        self.expire(row["id"])
        second, recovery = self.worker("normal", "recovery")
        second.join(40)
        self.assertEqual(second.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["state"], "interrupted", finished)
        self.assertEqual(finished["error_type"], "model_response_unknown")
        self.assertEqual(finished["calls_reserved"], 1)
        self.assertFalse((recovery / "fake-calls.jsonl").exists())

        def gone():
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
            )
            return result.returncode != 0 or result.stdout.strip().startswith("Z")

        wait_until(gone, 5)
        self.assertIn(
            "orphan_request_terminated",
            [e["event"] for e in self.store.events(row["id"])],
        )

    def test_confirmed_snapshot_reused_after_process_crash(self):
        row = self.task()
        first, barrier = self.worker("snapshot_crash")
        wait_until(lambda: (barrier / "barrier.json").exists())
        first.kill()
        first.join(10)
        self.expire(row["id"])
        second, _ = self.worker("normal", "recovery")
        second.join(50)
        self.assertEqual(second.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["result"]["agent"]["status"], "passed", finished)
        self.assertIn(
            "snapshot_reused", [e["event"] for e in self.store.events(row["id"])]
        )

    def test_budget_and_receipt_states_remain_after_new_store(self):
        from uuid import uuid4

        row = self.task()
        lease = self.store.claim(uuid4().hex, 10, row["id"])
        n = self.store.reserve_model(lease)
        self.assertEqual(n, 1)
        self.store.model_started(lease, n)
        self.store.model_finished(lease, n, {"input_tokens": 7, "output_tokens": 3})
        self.store.reserve_candidate(lease)
        reopened = MySQLStore(self.settings)
        self.assertEqual(reopened.get(row["id"])["calls_reserved"], 1)
        self.assertEqual(reopened.get(row["id"])["candidates_reserved"], 1)
        self.assertEqual(
            reopened.records(row["id"], "model_calls")[0]["usage_json"]["input_tokens"],
            7,
        )
        self.store.finish(lease, {}, state="interrupted", reason="offline_contract")

    def test_unissued_reservation_is_not_refunded_after_crash(self):
        self.request["max_model_calls"] = 3
        row = self.task()
        first, barrier = self.worker("reservation_crash")
        wait_until(lambda: (barrier / "barrier.json").exists())
        self.assertEqual(
            self.store.records(row["id"], "model_calls")[0]["state"], "reserved"
        )
        first.kill()
        first.join(10)
        self.expire(row["id"])
        second, recovery = self.worker("normal", "recovery")
        second.join(50)
        self.assertEqual(second.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["calls_reserved"], 3)
        self.assertEqual(finished["result"]["agent"]["status"], "passed", finished)
        calls = self.store.records(row["id"], "model_calls")
        self.assertEqual(
            [r["state"] for r in sorted(calls, key=lambda r: r["number"])],
            ["abandoned", "completed", "completed"],
        )
        self.assertEqual(
            len((recovery / "fake-calls.jsonl").read_text().splitlines()), 2
        )

    def test_missing_usage_stops_before_more_tools_or_requests(self):
        row = self.task()
        worker, barrier = self.worker("missing_usage")
        worker.join(45)
        self.assertEqual(worker.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["state"], "interrupted", finished)
        self.assertEqual(finished["calls_reserved"], 1)
        calls = self.store.records(row["id"], "model_calls")
        self.assertEqual(calls[0]["state"], "unknown")
        self.assertIsNone(calls[0]["usage_json"])
        self.assertEqual(finished["candidates_reserved"], 0)

    def test_persisted_deadline_prevents_database_reservation(self):
        self.request["deadline"] = 0.15
        row = self.task()
        lease = self.store.claim(uuid4().hex, 10, row["id"])
        self.store.begin_agent(lease)
        wait_until(lambda: self.store.check(lease)["deadline_expired"])
        from repair_workbench.session import BudgetExceeded

        with self.assertRaisesRegex(BudgetExceeded, "deadline_exceeded"):
            self.store.reserve_model(lease)
        self.assertEqual(self.store.get(row["id"])["calls_reserved"], 0)
        self.store.finish(lease, {}, state="interrupted", reason="deadline_exceeded")

    def test_durable_candidate_tampering_is_rejected_after_crash(self):
        row = self.task()
        first, barrier = self.worker("candidate_crash")
        wait_until(lambda: (barrier / "barrier.json").exists())
        candidate = self.store.records(row["id"], "repair_candidates")[0]
        first.kill()
        first.join(10)
        self.expire(row["id"])
        path = self.data / candidate["artifact_path"] / "workspace/pricing.py"
        path.write_text(path.read_text() + "\n# tampered acceptance candidate\n")
        second, recovery = self.worker("normal", "recovery")
        second.join(40)
        self.assertEqual(second.exitcode, 0)
        finished = self.store.get(row["id"])
        self.assertEqual(finished["state"], "error")
        self.assertEqual(finished["calls_reserved"], 2)
        self.assertFalse((recovery / "fake-calls.jsonl").exists())

    def test_live_heartbeat_prevents_takeover_past_original_lease(self):
        row = self.task()
        worker, barrier = self.worker("candidate_crash")
        wait_until(lambda: (barrier / "barrier.json").exists())
        initial = self.store.records(row["id"], "task_attempts")[0]["heartbeat_at"]
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            self.assertIsNone(self.store.claim(uuid4().hex, 3, row["id"]))
            time.sleep(0.1)
        self.assertNotEqual(
            self.store.records(row["id"], "task_attempts")[0]["heartbeat_at"], initial
        )
        self.assertEqual(self.store.get(row["id"])["generation"], 1)

    def test_cancel_during_real_docker_execution_cleans_container(self):
        tests = self.source / "tests/test_pricing.py"
        tests.write_text(
            "import time\ndef test_slow():\n    time.sleep(20)\n    assert False\n"
        )
        subprocess.run(
            ["git", "-C", str(self.source), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Cancellation fixture",
            ],
            check=True,
            capture_output=True,
        )
        self.request["revision"] = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        row = self.task()
        worker, _ = self.worker()
        resources = wait_until(
            lambda: self.store.records(row["id"], "execution_resources")
        )
        name = resources[0]["container_name"]

        def running():
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"

        wait_until(running)
        self.store.cancel(row["id"])
        worker.join(20)
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(self.store.get(row["id"])["state"], "cancelled")
        self.assertNotEqual(
            subprocess.run(["docker", "inspect", name], capture_output=True).returncode,
            0,
        )
        self.assertEqual(
            self.store.records(row["id"], "execution_resources")[0]["state"], "cleaned"
        )

    def test_real_http_service_restart_queries_receipts_and_patch(self):
        import httpx

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Per-test credential file, never logs values or passes a password in argv.
        config = self.root / "database.env"
        config.write_text(
            "\n".join(
                f"{k}={v}"
                for k, v in {
                    "REPAIR_DB_HOST": self.settings.host,
                    "REPAIR_DB_PORT": self.settings.port,
                    "REPAIR_DB_USER": self.settings.user,
                    "REPAIR_DB_PASSWORD": self.settings.password,
                    "REPAIR_DB_NAME": self.settings.database,
                }.items()
            )
            + "\n"
        )
        config.chmod(0o600)
        command = [
            sys.executable,
            "-m",
            "repair_workbench",
            "serve",
            "--backend",
            "mysql",
            "--db-env",
            str(config),
            "--data-dir",
            str(self.data),
            "--repo-root",
            str(self.source),
            "--image",
            os.environ.get(
                "REPAIR_TEST_IMAGE", "repair-workbench-test:20261014-pinned"
            ),
            "--port",
            str(port),
        ]

        def start():
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.addCleanup(lambda: process.poll() is None and process.terminate())

            def ready():
                try:
                    return (
                        httpx.get(
                            f"http://127.0.0.1:{port}/api/config", timeout=0.5
                        ).status_code
                        == 200
                    )
                except httpx.TransportError:
                    return False

            wait_until(ready)
            return process

        server = start()
        url = f"http://127.0.0.1:{port}"
        headers = {"X-Workbench-Request": "1", "Idempotency-Key": uuid4().hex}
        body = {k: v for k, v in self.request.items() if k != "profile"}
        response = httpx.post(url + "/api/tasks", headers=headers, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()
        self.task_ids.append(row["id"])
        duplicate = httpx.post(url + "/api/tasks", headers=headers, json=body)
        self.assertEqual(duplicate.json()["id"], row["id"])
        conflict = httpx.post(
            url + "/api/tasks", headers=headers, json={**body, "task": "other"}
        )
        self.assertEqual(conflict.status_code, 409)
        worker, _ = self.worker()
        worker.join(50)
        self.assertEqual(worker.exitcode, 0)
        server.terminate()
        server.wait(10)
        server = start()
        task = httpx.get(url + "/api/tasks/" + row["id"]).json()
        self.assertEqual(task["result"]["agent"]["status"], "passed", task)
        receipts = httpx.get(url + "/api/tasks/" + row["id"] + "/execution").json()
        self.assertEqual(len(receipts["model_calls"]), 3)
        self.assertTrue(all(r["state"] == "completed" for r in receipts["model_calls"]))
        self.assertIn(
            "amount >= 100", httpx.get(url + "/api/tasks/" + row["id"] + "/patch").text
        )
        events = self.store.events(row["id"])
        cursor = events[1]["id"]
        text = httpx.get(
            url + "/api/tasks/" + row["id"] + "/events?follow=false",
            headers={"Last-Event-ID": str(cursor)},
        ).text
        self.assertNotIn(f"id: {cursor}\n", text)
        self.assertIn("event: complete", text)
        server.terminate()
        server.wait(10)
