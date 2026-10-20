"""Independent local workers with fenced receipts and conservative stage recovery."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import os
import re
import signal
import threading
import time
from uuid import uuid4

from .mysql_store import MySQLStore, OwnershipLost
from .session import RepairSession, BudgetExceeded
from .snapshot import create_snapshot, verify_snapshot, snapshot_id
from .runner import run_tests
from .verification import verify_candidate


def directory_digest(path):
    entries = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError("artifact_symlink")
        if child.is_file():
            entries.append(
                (
                    child.relative_to(path).as_posix(),
                    hashlib.sha256(child.read_bytes()).hexdigest(),
                )
            )
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def snapshot_digest(path):
    manifest = verify_snapshot(path)
    value = {
        "manifest_sha": hashlib.sha256(
            (path / "manifest.json").read_bytes()
        ).hexdigest(),
        "snapshot_id": snapshot_id(manifest),
    }
    if (path / "patch.diff").exists():
        value["patch_sha"] = hashlib.sha256(
            (path / "patch.diff").read_bytes()
        ).hexdigest()
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class DurableControl:
    def __init__(self, store, lease, root, stopping):
        self.store, self.lease, self.root, self.stopping = (
            store,
            lease,
            root.resolve(),
            stopping,
        )
        self.agent_active = False

    def relative(self, path):
        path = path.resolve(strict=True)
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            raise ValueError("artifact_outside_data_directory") from None

    def resolve(self, relative):
        path = (self.root / relative).resolve(strict=True)
        if self.root not in path.parents:
            raise ValueError("artifact_outside_data_directory")
        return path

    def check(self):
        if self.stopping.is_set():
            raise OwnershipLost("worker_stopping_or_lease_lost")
        row = self.store.check(self.lease)
        if row["state"] == "cancelling":
            raise BudgetExceeded("cancel_requested")
        if self.agent_active and row["deadline_expired"]:
            raise BudgetExceeded("deadline_exceeded")
        return row

    def reserve_model(self):
        self.check()
        return self.store.reserve_model(self.lease)

    def model_started(self, number):
        self.check()
        self.store.model_started(self.lease, number)

    def model_finished(self, number, usage):
        self.store.model_finished(self.lease, number, usage)
        if usage is None:
            raise BudgetExceeded("model_usage_unknown")

    def model_failed(self, number, reason):
        self.store.model_finished(self.lease, number, None, reason)

    def reserve_candidate(self):
        self.check()
        return self.store.reserve_candidate(self.lease)

    def candidate_ready(self, number, path):
        self.check()
        self.store.candidate(
            self.lease,
            number,
            "ready",
            path=self.relative(path),
            sha=snapshot_digest(path),
        )
        self.store.event(self.lease, {"event": "candidate_persisted", "number": number})

    def candidate_rejected(self, number, result):
        self.store.candidate(self.lease, number, "rejected", result=result)

    def candidate_verified(self, number, path, result):
        self.store.test_run(
            self.lease,
            "candidate",
            self.relative(path),
            directory_digest(path / "reports"),
            result,
            number,
        )
        result = {
            **result,
            "verification_path": self.relative(path),
            "verification_sha": snapshot_digest(path),
            "reports_sha": directory_digest(path / "reports"),
        }
        self.store.candidate(self.lease, number, "verified", result=result)

    def container(self, name, state):
        self.store.resource(self.lease, name, state)


class Worker:
    def __init__(
        self,
        store: MySQLStore,
        data_dir: Path,
        repo_roots: list[Path],
        image: str,
        *,
        model_factory=None,
        lease_seconds=15,
        owner=None,
        hook=None,
        task_id=None,
    ):
        if not 0.6 <= lease_seconds <= 300:
            raise ValueError("lease must be 0.6-300 seconds")
        self.store = store
        self.root = data_dir.resolve()
        self.repo_roots = repo_roots
        self.image = image
        self.model_factory = model_factory
        self.lease_seconds = lease_seconds
        self.owner = owner or uuid4().hex
        self.hook = hook or (lambda event, lease: None)
        self.task_id = task_id
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def run_once(self):
        lease = self.store.claim(self.owner, self.lease_seconds, self.task_id)
        if lease is None:
            return False
        end = threading.Event()
        lost = threading.Event()
        control = DurableControl(self.store, lease, self.root, lost)

        def heartbeat():
            while not end.wait(self.lease_seconds / 3):
                try:
                    self.store.heartbeat(lease, self.lease_seconds)
                except Exception:
                    lost.set()
                    return

        beat = threading.Thread(target=heartbeat, name="lease-heartbeat", daemon=True)
        beat.start()
        try:
            self.hook("claimed", lease)
            self.execute(lease, control)
        except OwnershipLost:
            # Fenced out; never persist a stale terminal result.
            pass
        except BudgetExceeded as exc:
            self.store.finish(
                lease,
                {"reason": str(exc)},
                state="cancelled" if str(exc) == "cancel_requested" else "interrupted",
                reason=str(exc),
            )
        except Exception as exc:
            try:
                self.store.finish(
                    lease,
                    {
                        "reason": "worker_execution_error",
                        "error_type": type(exc).__name__,
                    },
                    state="error",
                    reason=type(exc).__name__,
                )
            except OwnershipLost:
                pass
        finally:
            end.set()
            beat.join(timeout=6)
        return True

    def run(self):
        while not self.stopping.is_set():
            if not self.run_once():
                self.stopping.wait(0.25)

    def clean_expired_resources(self, lease):
        cursor = 0
        requests = {}
        while True:
            events = self.store.events(lease.task_id, cursor)
            if not events:
                break
            cursor = events[-1]["id"]
            for event in events:
                if event["generation"] >= lease.generation:
                    continue
                key = (event["generation"], event.get("pid"))
                if event["event"] == "model_worker_started":
                    requests[key] = event
                elif event["event"] == "model_worker_finished" and event.get(
                    "cleanup_ok"
                ):
                    requests.pop(key, None)
        for event in requests.values():
            token = event.get("request_process_id", "")
            pid = event["pid"]
            if (
                type(pid) is not int
                or pid <= 1
                or not re.fullmatch("[0-9a-f]{32}", token)
            ):
                raise RuntimeError("orphan_request_identity_unconfirmed")

            def process_info():
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat=", "-o", "args="],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                return result.stdout.strip() if result.returncode == 0 else ""

            info = process_info()
            if info and not info.startswith("Z"):
                if "--repair-request-id " + token not in info or os.getpgid(pid) != pid:
                    raise RuntimeError("orphan_request_identity_unconfirmed")
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline and process_info():
                    time.sleep(0.02)
                if process_info():
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    info = process_info()
                    if not info or info.startswith("Z"):
                        break
                    time.sleep(0.02)
                if info and not info.startswith("Z"):
                    raise RuntimeError("orphan_request_cleanup_unconfirmed")
            self.store.event(
                lease,
                {
                    "event": "orphan_request_terminated",
                    "pid": pid,
                    "origin_generation": event["generation"],
                },
            )
        for resource in self.store.records(lease.task_id, "execution_resources"):
            if (
                resource["generation"] >= lease.generation
                or resource["state"] == "cleaned"
            ):
                continue
            name = resource["container_name"]
            # Only database-registered task-specific random container names.
            result = subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=15
            )
            if result.returncode:
                inspected = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if inspected.returncode == 0 or "No such" not in inspected.stderr:
                    raise RuntimeError("orphan_container_cleanup_unconfirmed")
            self.store.resource(lease, name, "cleaned")

    def execute(self, lease, control):
        task = self.store.check(lease)
        request = task["request"]
        # Reclaim old execution resources even if the repository disappeared.
        self.clean_expired_resources(lease)
        # Reuse the existing source-repository approval boundary before any stage.
        from .mysql_service import MySQLService

        checker = MySQLService(self.store, self.root, self.repo_roots, self.image)
        checker.repository(request["repo"], request["revision"])
        if task["state"] == "cancelling":
            raise BudgetExceeded("cancel_requested")
        base_dir = (
            self.root / "runs" / lease.task_id / f"generation-{lease.generation:06d}"
        )
        base_dir.mkdir(parents=True, exist_ok=False)
        stages = self.store.records(lease.task_id, "stage_artifacts")
        baseline_records = sorted(
            (r for r in stages if r["kind"] == "baseline"),
            key=lambda r: r["generation"],
        )
        snapshots = sorted(
            (r for r in stages if r["kind"] == "snapshot"),
            key=lambda r: r["generation"],
        )
        if baseline_records:
            receipt = baseline_records[-1]
            baseline = control.resolve(receipt["artifact_path"])
            if (
                snapshot_digest(baseline) != receipt["artifact_sha"]
                or directory_digest(baseline / "reports")
                != receipt["metadata"]["reports_sha"]
            ):
                raise ValueError("persisted_baseline_changed")
            before = receipt["metadata"]["result"]
            self.store.event(
                lease,
                {
                    "event": "baseline_reused",
                    "origin_generation": receipt["generation"],
                },
            )
        else:
            if snapshots:
                import shutil

                receipt = snapshots[-1]
                source = control.resolve(receipt["artifact_path"])
                if snapshot_digest(source) != receipt["artifact_sha"]:
                    raise ValueError("persisted_snapshot_changed")
                # Failed/incomplete reports never copied or overwritten.
                baseline = base_dir / "baseline"
                shutil.copytree(
                    source, baseline, ignore=shutil.ignore_patterns("reports")
                )
                self.store.event(
                    lease,
                    {
                        "event": "snapshot_reused",
                        "origin_generation": receipt["generation"],
                    },
                )
            else:
                baseline = base_dir / "baseline"
                create_snapshot(Path(request["repo"]), baseline, request["revision"])
            self.store.stage(
                lease,
                "snapshot",
                control.relative(baseline),
                snapshot_digest(baseline),
                {},
            )
            self.hook("snapshot_persisted", lease)
            before = run_tests(
                baseline, self.image, request["test_timeout"], execution_control=control
            )
            self.store.test_run(
                lease,
                "baseline",
                control.relative(baseline),
                directory_digest(baseline / "reports"),
                before,
            )
            self.store.stage(
                lease,
                "baseline",
                control.relative(baseline),
                snapshot_digest(baseline),
                {
                    "result": before,
                    "reports_sha": directory_digest(baseline / "reports"),
                },
            )
            self.hook("baseline_persisted", lease)
        result = {
            "baseline": before,
            "baseline_path": control.relative(baseline),
            "agent": None,
        }
        if before["status"] != "tests_failed" or not before["cleanup_ok"]:
            result["reason"] = (
                "no_failure_to_repair"
                if before["status"] == "tests_passed"
                else "baseline_not_reproduced"
            )
            self.store.finish(lease, result)
            return
        calls = self.store.records(lease.task_id, "model_calls")
        if any(r["state"] in {"unknown", "started"} for r in calls):
            result["reason"] = "model_response_unknown"
            self.store.finish(
                lease, result, state="interrupted", reason=result["reason"]
            )
            return
        if task["agent_deadline"] is not None:
            control.agent_active = True
            control.check()
        candidates = sorted(
            self.store.records(lease.task_id, "repair_candidates"),
            key=lambda r: r["number"],
        )
        for candidate in candidates:
            if candidate["state"] not in {"ready", "verified"}:
                continue
            path = control.resolve(candidate["artifact_path"])
            if snapshot_digest(path) != candidate["artifact_sha"]:
                raise ValueError("persisted_candidate_changed")
            n = candidate["number"]
            if candidate["state"] == "ready":
                verification = base_dir / f"recovery-{n:02d}"
                self.store.event(
                    lease,
                    {
                        "event": "candidate_recovered",
                        "number": n,
                        "origin_generation": candidate["generation"],
                    },
                )
                verdict = verify_candidate(
                    baseline,
                    path,
                    verification,
                    set(request["allowed_paths"]),
                    request["test_timeout"],
                    execution_control=control,
                )
                control.candidate_verified(n, verification, verdict)
                candidate = next(
                    r
                    for r in self.store.records(lease.task_id, "repair_candidates")
                    if r["number"] == n
                )
            receipt = candidate["result"]
            verification = control.resolve(receipt["verification_path"])
            if (
                snapshot_digest(verification) != receipt["verification_sha"]
                or directory_digest(verification / "reports") != receipt["reports_sha"]
            ):
                raise ValueError("persisted_verification_changed")
            if receipt["status"] == "passed":
                result["agent"] = {
                    "state": "stopped",
                    "status": "passed",
                    "reason": "passed_original_test_scope",
                    "stop_reason": "recovered_verified_candidate",
                    "patch": control.relative(verification / "patch.diff"),
                    "attempts": [{"number": n, **receipt}],
                    "model_calls": task["calls_reserved"],
                }
                self.store.finish(lease, result)
                return
        if any(r["state"] == "completed" for r in calls) or candidates:
            # No graph/session/source_ref replay. Completed model calls stay charged.
            result["reason"] = "agent_context_not_resumable"
            self.store.finish(
                lease, result, state="interrupted", reason=result["reason"]
            )
            return
        if self.model_factory is None:
            self.store.finish(
                lease, result, state="interrupted", reason="model_not_configured"
            )
            return
        self.store.begin_agent(lease)
        control.agent_active = True

        def event(payload):
            control.check()
            self.store.event(lease, payload)
            self.hook(payload["event"], lease)

        original_ready = control.candidate_ready

        def ready(number, path):
            original_ready(number, path)
            self.hook("candidate_persisted", lease)

        control.candidate_ready = ready
        session = RepairSession(
            baseline,
            base_dir / "agent",
            set(request["allowed_paths"]),
            max_attempts=request["max_attempts"],
            max_model_calls=request["max_model_calls"],
            deadline_seconds=request["deadline"],
            test_timeout=request["test_timeout"],
            profile=request["profile"],
            event_sink=event,
            durability=control,
            execution_control=control,
        )
        session.model_calls = task["calls_reserved"]
        from .agent import run_agent

        agent = run_agent(session, self.model_factory(), request["task"])
        control.agent_active = False
        if agent.get("patch"):
            agent["patch"] = control.relative(base_dir / "agent" / agent["patch"])
        result["agent"] = agent
        result["agent_path"] = control.relative(base_dir / "agent")
        if any(
            r["state"] == "unknown"
            for r in self.store.records(lease.task_id, "model_calls")
        ):
            self.store.finish(
                lease, result, state="interrupted", reason="model_response_unknown"
            )
        else:
            self.store.finish(lease, result)
