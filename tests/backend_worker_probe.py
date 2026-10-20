"""Offline subprocess probes; never creates a provider client."""

import hashlib
import json
import os
import sys
from pathlib import Path
import time
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from repair_workbench.durable_worker import Worker
from repair_workbench.mysql_store import DatabaseSettings, MySQLStore, OwnershipLost


class FixtureModel(BaseChatModel):
    original: str
    barrier_dir: str
    mode: str = "repair"
    calls: int = 0

    @property
    def _llm_type(self):
        return "offline-backend-probe"

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke_controlled(self, messages, session, stop=None, **kwargs):
        if self.mode != "unknown_process":
            return self.invoke(messages, stop=stop, **kwargs)
        from repair_workbench.model_transport import run_request_worker

        code = "import os,sys,json,time;from pathlib import Path;p=json.load(sys.stdin);Path(p['barrier']).write_text(str(os.getpid()));time.sleep(120)"
        return run_request_worker(
            {"barrier": str(Path(self.barrier_dir) / "request-child")},
            session,
            30,
            command=[sys.executable, "-c", code],
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        root = Path(self.barrier_dir)
        with (root / "fake-calls.jsonl").open("a") as f:
            f.write(json.dumps({"pid": os.getpid(), "call": self.calls}) + "\n")
        if self.mode == "unknown":
            (root / "model-entered").write_text(str(os.getpid()))
            while True:
                time.sleep(0.05)
        if self.calls == 0:
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {"path": "pricing.py", "raw": True},
                        "id": "read",
                    }
                ],
            )
        elif self.calls == 1:
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_edits",
                        "args": {
                            "edits": [
                                {
                                    "path": "pricing.py",
                                    "sha256": hashlib.sha256(
                                        self.original.encode()
                                    ).hexdigest(),
                                    "start_line": 5,
                                    "old_text": "    if amount > 100:",
                                    "new_text": "    if amount >= 100:",
                                }
                            ],
                            "diagnosis": "Offline contract fixture: inclusive threshold",
                        },
                        "id": "edit",
                    }
                ],
            )
        else:
            response = AIMessage(
                content="Fixture complete; inspect application verdict."
            )
        self.calls += 1
        if self.mode != "missing_usage":
            response.usage_metadata = {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            }
        return ChatResult(generations=[ChatGeneration(message=response)])


def run_probe(
    settings, data, repo, image, barrier, mode="normal", lease_seconds=3, task_id=None
):
    os.environ.pop("DASHSCOPE_API_KEY", None)
    os.environ["LANGSMITH_TRACING"] = "false"
    store = MySQLStore(DatabaseSettings(**settings))
    root = Path(barrier)

    def hook(event, lease):
        if mode == "lease" and event == "claimed":
            (root / "claimed.json").write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "generation": lease.generation,
                        "owner": lease.owner,
                        "task_id": lease.task_id,
                    }
                )
            )
            while not (root / "release").exists():
                time.sleep(0.02)
            rejected = []
            operations = {
                "event": lambda: store.event(lease, {"event": "stale"}),
                "heartbeat": lambda: store.heartbeat(lease, 3),
                "model_reservation": lambda: store.reserve_model(lease),
                "candidate_reservation": lambda: store.reserve_candidate(lease),
                "stage": lambda: store.stage(lease, "bad", "bad", "0" * 64, {}),
                "test_run": lambda: store.test_run(lease, "bad", "bad", "0" * 64, {}),
                "finish": lambda: store.finish(lease, {"bad": True}),
                "candidate_write": lambda: store.candidate(
                    lease, 1, "ready", path="bad", sha="0" * 64
                ),
                "model_receipt": lambda: store.model_finished(
                    lease, 1, {"input_tokens": 0, "output_tokens": 0}
                ),
            }
            for name, operation in operations.items():
                try:
                    operation()
                except OwnershipLost:
                    rejected.append(name)
            (root / "stale-writes.json").write_text(json.dumps(rejected))
        selected = {
            "candidate_crash": "candidate_persisted",
            "snapshot_crash": "snapshot_persisted",
            "reservation_crash": "model_started",
        }
        if selected.get(mode) == event:
            (root / "barrier.json").write_text(
                json.dumps(
                    {"pid": os.getpid(), "generation": lease.generation, "event": event}
                )
            )
            while True:
                time.sleep(0.05)

    original = (Path(repo) / "pricing.py").read_text()
    worker = Worker(
        store,
        Path(data),
        [Path(repo)],
        image,
        model_factory=lambda: FixtureModel(
            original=original, barrier_dir=str(root), mode=mode
        ),
        lease_seconds=lease_seconds,
        hook=hook,
        task_id=task_id,
    )
    worker.run_once()
    (root / "worker-exited").write_text(str(os.getpid()))


def claim_probe(settings, gate, output, task_id):
    while not Path(gate).exists():
        time.sleep(0.01)
    store = MySQLStore(DatabaseSettings(**settings))
    from uuid import uuid4

    lease = store.claim(uuid4().hex, 10, task_id)
    Path(output).write_text(
        json.dumps(
            None
            if lease is None
            else {
                "task_id": lease.task_id,
                "generation": lease.generation,
                "owner": lease.owner,
                "pid": os.getpid(),
            }
        )
    )
    if lease:
        store.finish(lease, {"probe": True})


def create_probe(settings, gate, output, request, key):
    while not Path(gate).exists():
        time.sleep(0.01)
    store = MySQLStore(DatabaseSettings(**settings))
    row = store.create(request, key)
    Path(output).write_text(json.dumps({"id": row["id"], "pid": os.getpid()}))
