"""Trusted SWE-bench batch executor; no implicit keys, downloads or API calls.

Create a FrozenEvaluationBatch manifest with task ``evaluator`` metadata holding
absolute ``dataset``, ``workspace`` and ``qualification`` directories. Shared
``config.swebench`` holds ``harness_python``, ``network_policy``,
``evaluation_timeout_seconds`` and ``model_label``. Shared limits additionally
declare ``max_attempts`` (1–5), ``max_tool_calls`` (currently exactly 60), and the
batch driver's independent ``final_evaluation_seconds``. Existing requests per
run cannot exceed RepairSession's 30-request ceiling.

Bind every path returned by ``required_binding_paths`` into the frozen manifest,
alongside application source/dependency fingerprints. Qualification files are
evaluator-owned. This module checks all tasks before constructing the first
model, then rechecks each run. It never copies gold/test patches into Agent input.
Only a verified submitted candidate is exported and independently evaluated;
``submitted`` itself is never a successful repair.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import subprocess

from . import swebench_adapter as adapter
from .evaluation_batch import FrozenEvaluationBatch
from .evaluation_budget import _atomic_write
from .experiment_manifest import agent_task
from .session import BudgetExceeded
from .snapshot import verify_snapshot
from .diagnostic_evaluation import source_fingerprint


_POLICIES = {"on_demand", "issue_evidence"}


def _absolute(value, name):
    if type(value) is not str or not Path(value).is_absolute():
        raise ValueError("absolute_" + name + "_required")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("invalid_" + name)
    return path


def required_binding_paths(task: dict) -> list[Path]:
    """List evaluator receipts to freeze, without reading their hidden contents.

    Source files themselves are checked by verify_snapshot; the caller must also
    bind the Agent's implementation/dependency identity. The harness lock below
    is checked inside the selected interpreter before any model is constructed.
    """
    private = task.get("evaluator")
    if type(private) is not dict or set(private) != {"dataset", "workspace", "qualification"}:
        raise ValueError("SWE task requires dataset, workspace and qualification paths")
    dataset, workspace, qualification = (
        _absolute(private[key], key) for key in ("dataset", "workspace", "qualification"))
    instance = task["id"]
    if type(instance) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+", instance):
        raise ValueError("invalid_SWE_instance_id")
    return [workspace / "task.json", workspace / "workspace.json",
            workspace / "baseline/manifest.json", workspace / "baseline/reports/result.json",
            workspace / "baseline/reports/pytest.xml", dataset / "manifest.json",
            dataset / instance / "public.json", dataset / instance / "evaluator.json",
            qualification / "qualification.json",
            qualification / "negative/source-binding.json",
            qualification / "gold/source-binding.json",
            Path(adapter.__file__).with_name("swebench_harness_lock.json")]


def _harness_preflight(interpreter):
    """Verify the locked official harness in its own venv, without running tests."""
    if not interpreter.is_file():
        raise ValueError("harness_interpreter_missing")
    process = subprocess.run(
        [str(interpreter), "-c", "from repair_workbench.swebench_worker import verify_harness; verify_harness()"],
        env=adapter.isolated_env(), capture_output=True, timeout=30)
    if process.returncode != 0:
        # Keep provider keys and interpreter/fixture diagnostics out of prompts.
        raise ValueError("official_harness_preflight_failed")


class SWEbenchExecutor:
    """Evaluator-only callback for FrozenEvaluationBatch.run.

    The explicit key is retained only in this trusted object and is never written
    to the plan or returned to the Agent. Passing a key is not an authorization;
    the application must approve the frozen batch before calling ``run``.
    """
    def __init__(self, batch: FrozenEvaluationBatch, api_key: str):
        if type(api_key) is not str or not api_key.strip():
            raise ValueError("explicit_model_key_required")
        self.batch, self._key = batch, api_key
        self._ready = False

    def _settings(self):
        config = self.batch.config
        if config.get('implementation') != source_fingerprint():
            raise ValueError('SWE_implementation_or_dependency_drift')
        settings = config.get("swebench")
        if (type(settings) is not dict or set(settings) != {
                "harness_python", "network_policy", "evaluation_timeout_seconds", "model_label"}):
            raise ValueError("invalid_SWE_shared_settings")
        timeout = settings["evaluation_timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("invalid_SWE_evaluation_timeout")
        if type(settings["network_policy"]) is not str or not settings["network_policy"]:
            raise ValueError("invalid_SWE_network_policy")
        if type(settings["model_label"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", settings["model_label"]):
            raise ValueError("invalid_SWE_model_label")
        limits = config["limits"]
        for key, maximum in (("requests_per_run", 30), ("max_attempts", 5)):
            if type(limits.get(key)) is not int or not 1 <= limits[key] <= maximum:
                raise ValueError("unsupported_SWE_" + key)
        if type(limits.get("max_tool_calls")) is not int or limits["max_tool_calls"] != 60:
            raise ValueError("SWE_tool_limit_is_currently_60")
        if ({variant["policy"] for variant in self.batch.manifest["variants"]} != _POLICIES
                or len(self.batch.manifest["variants"]) != 2):
            raise ValueError("SWE_batch_requires_on_demand_and_issue_evidence")
        return settings

    def _binding(self, task):
        settings = self._settings()
        required = {str(path.absolute()) for path in required_binding_paths(task)}
        bound = {item["path"] for item in self.batch.manifest["files"]}
        if not required <= bound:
            raise ValueError("SWE_receipts_not_bound_in_manifest")
        private = task["evaluator"]
        workspace, qualification = Path(private["workspace"]), Path(private["qualification"])
        public = agent_task(self.batch.manifest, task["id"])
        expected = {"instance_id": public["id"], **{key: public[key] for key in ("repo", "base_commit", "problem_statement")}}
        checked = adapter.validate_qualification(workspace, qualification)
        if checked["task"] != expected or adapter.load(workspace / "task.json") != expected:
            raise ValueError("SWE_public_task_mismatch")
        record = adapter.task_record(private["dataset"], task["id"])
        if {key: record[key] for key in adapter.PUBLIC_FIELDS} != expected:
            raise ValueError("SWE_dataset_task_mismatch")
        receipt = adapter.load(workspace / "workspace.json")
        if receipt["task"] != expected:
            raise ValueError("SWE_workspace_task_mismatch")
        verify_snapshot(workspace / "baseline")
        policy = adapter.execution_policy(settings["network_policy"], task["id"])
        if checked["execution_policy"] != policy:
            raise ValueError("SWE_qualification_policy_mismatch")
        if adapter.image_identity(record["image"]) != checked["image_id"]:
            raise ValueError("SWE_qualification_image_mismatch")
        return {"workspace": workspace, "qualification": qualification, "record": record,
                "qualification_receipt": checked}

    def _preflight_all(self):
        settings = self._settings()
        _harness_preflight(_absolute(settings["harness_python"], "harness_python"))
        for task in self.batch.manifest["tasks"]:
            self.batch.check()
            self._binding(task)
        self._ready = True

    def __call__(self, ctx):
        receipt = {"phase": "preflight", "cleanup_ok": True, "evaluation_started": False}
        try:
            ctx.check()
            if not self._ready:
                self._preflight_all()
            task = next(item for item in self.batch.manifest["tasks"] if item["id"] == ctx.task["id"])
            binding = self._binding(task)
            if ctx.task != agent_task(self.batch.manifest, task["id"]):
                raise ValueError("SWE_context_task_mismatch")
            settings, limits = self._settings(), self.batch.config["limits"]
            ctx.check()
            model = ctx.metered_model(self._key)
            original_check = model.validate_frozen

            def check_model():
                original_check()
                self._settings()

            model = model.model_copy(update={'validate_frozen': check_model})
            ctx.emit("SWE_qualification_checked", **{key: binding["qualification_receipt"][key]
                     for key in ("image_id", "execution_policy", "qualification_sha256")})

            execution_control = ctx.execution_control()

            def control():
                execution_control.check()
                return False

            receipt["phase"] = "agent"
            agent_output = ctx.output / "agent"
            result = adapter.run_qualified_agent(binding["workspace"], agent_output, model,
                binding["qualification"], swe_policy=ctx.policy,
                max_attempts=limits["max_attempts"], max_model_calls=limits["requests_per_run"],
                deadline_seconds=ctx.remaining_seconds(), cancellation_check=control,
                event_sink=lambda event: ctx.emit("agent_event", receipt=event))
            receipt.update(agent_status=result.get("status"), agent_stop_reason=result.get("stop_reason"))
            ctx.emit("agent_finished", **{key: receipt[key] for key in ("agent_status", "agent_stop_reason")})
            try:
                ctx.check()  # Also fence drift on no-submission and budget-stop exits.
            except BudgetExceeded as exc:
                if str(exc) != 'run_deadline':
                    raise
                # check() validates frozen identity before its phase deadline.
                # The Agent owns no running container here; return its cleanup
                # receipt so the batch can retain a local stop and advance.
                return {'outcome': 'inconclusive', 'cleanup_ok': True, 'reason': 'run_deadline',
                        'agent_stop_reason': result.get('stop_reason'), 'agent_result': 'agent/result.json'}
            if result.get("status") != "submitted":
                return {"outcome": "failed", "cleanup_ok": True, "reason": "no_final_submission",
                        "agent_stop_reason": result.get("stop_reason"), "agent_result": "agent/result.json"}
            prediction_path = ctx.output / "predictions.jsonl"
            prediction = adapter.export_prediction(binding["workspace"], agent_output,
                prediction_path, model_name=settings["model_label"])
            prediction_hash = hashlib.sha256(prediction_path.read_bytes()).hexdigest()
            ctx.begin_evaluation(prediction_hash)
            timeout = min(settings["evaluation_timeout_seconds"], math.floor(ctx.remaining_seconds()))
            if timeout < 1:
                raise BudgetExceeded("evaluation_deadline")
            receipt.update(phase="evaluation", evaluation_started=True, cleanup_ok=False,
                           prediction_sha256=prediction_hash)
            ctx.emit("final_submission", prediction_sha256=prediction_hash,
                     prediction_ref="predictions.jsonl")
            official = adapter.evaluate(binding["record"], prediction, ctx.output / "official-evaluation",
                _absolute(settings["harness_python"], "harness_python"),
                workspace=binding["workspace"], timeout=timeout,
                network_policy=settings["network_policy"], execution_control=execution_control)
            receipt["cleanup_ok"] = official.get("cleanup_ok") is True
            ctx.check()  # Worker/container cleanup has finished before this full gate.
            status = official.get("status")
            qualified = binding["qualification_receipt"]
            if (receipt["cleanup_ok"] and status in {"resolved", "unresolved"}
                    and (official.get("image_id") != qualified["image_id"]
                         or official.get("execution_policy") != qualified["execution_policy"])):
                raise ValueError("SWE_scoring_environment_changed_after_qualification")
            outcome = {"resolved": "passed", "unresolved": "failed"}.get(status, "inconclusive")
            receipt.update(phase="completed", official_status=status, outcome=outcome)
            ctx.emit("independent_verdict", status=status, outcome=outcome,
                     cleanup_ok=receipt["cleanup_ok"], report_ref="official-evaluation/result.json")
            return {"outcome": outcome, "cleanup_ok": receipt["cleanup_ok"],
                    "reason": official.get("reason", "official_report_missing"),
                    "agent_result": "agent/result.json", "prediction_sha256": prediction_hash,
                    "official_result": "official-evaluation/result.json", "official_status": status}
        except BaseException as exc:
            receipt["error_type"] = type(exc).__name__
            raise
        finally:
            _atomic_write(ctx.output / "swe-executor-receipt.json", receipt, create=True)


def run_swebench_batch(manifest, output, *, expected_sha256, current_config,
                       api_key, cancelled=lambda: False):
    """Run an explicitly authorized frozen batch using an explicitly supplied key.

    All planned rows are saved before preflight. Failures never silently replace
    tasks or restart runs. Unknown usage and unconfirmed cleanup stop the batch.
    Official feedback stays on the evaluator side after the Agent has ended.
    """
    batch = FrozenEvaluationBatch(manifest, output, expected_sha256=expected_sha256,
                                  current_config=current_config, cancelled=cancelled)
    return batch.run(SWEbenchExecutor(batch, api_key))
