"""HTTP-side MySQL service; never runs repair work in the API process."""

from pathlib import Path
import re
from .service import TaskService
from .mysql_store import canonical_digest


class MySQLService(TaskService):
    supports_idempotency = True

    def __init__(self, store, data_dir, repo_roots, image, *, profile="v2"):
        from .source_context import PROFILES

        if profile not in PROFILES:
            raise ValueError("unknown profile")
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        self.repo_roots = [Path(p).resolve(strict=True) for p in repo_roots]
        if not self.repo_roots:
            raise ValueError("approved repository roots required")
        self.image = image
        self.profile = profile
        # Configuration is owned by workers; readiness here means task admission.
        self.model_factory = True
        self.model_name = "worker-configured"

    def start(self):
        pass

    def stop(self):
        pass

    def submit(self, request, idempotency_key=None):
        original = {
            **request,
            "allowed_paths": sorted(set(request["allowed_paths"])),
            "repo": str(Path(request["repo"]).expanduser().resolve()),
            "profile": self.profile,
        }
        digest = canonical_digest(original)
        existing = self.store.existing_request(idempotency_key, digest)
        if existing:
            return existing
        info = self.repository(request["repo"], request.get("revision", "HEAD"))
        if not original["allowed_paths"] or not set(original["allowed_paths"]) <= set(
            info["source_files"]
        ):
            raise ValueError("unapproved source path")
        if not info["has_tests"]:
            raise ValueError("tests/ is required")
        frozen = {**original, "repo": info["repo"], "revision": info["commit"]}
        return self.store.create(frozen, idempotency_key, digest)

    def artifact(self, task_id, kind, attempt=0):
        task = self.store.get(task_id)
        if task is None or not re.fullmatch("[0-9a-f]{32}", task_id):
            raise FileNotFoundError("task not found")
        result = task.get("result") or {}
        if kind == "patch":
            relative = (result.get("agent") or {}).get("patch")
        elif attempt:
            rows = [
                r
                for r in self.store.records(task_id, "repair_candidates")
                if r["number"] == attempt and r["state"] == "verified"
            ]
            relative = (
                (rows[0]["result"]["verification_path"] + "/reports/pytest.log")
                if rows
                else None
            )
        else:
            rows = [
                r
                for r in self.store.records(task_id, "stage_artifacts")
                if r["kind"] == "baseline"
            ]
            relative = (
                (
                    max(rows, key=lambda r: r["generation"])["artifact_path"]
                    + "/reports/pytest.log"
                )
                if rows
                else None
            )
        if not relative:
            raise FileNotFoundError("artifact unavailable")
        path = (self.data_dir / relative).resolve(strict=True)
        task_root = (self.data_dir / "runs" / task_id).resolve()
        if task_root not in path.parents:
            raise ValueError("artifact path escape")
        return path
