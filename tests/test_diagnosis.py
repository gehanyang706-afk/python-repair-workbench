"""Offline diagnostic-session/tool-loop contracts, not LLM or repair-quality scores.

Passing baseline receipts and probe/verifier outputs are synthetic test doubles.
Repository and candidate code are never executed on the host by these tests.
"""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.edits import EditRejected
from repair_workbench.session import BudgetExceeded, RepairSession
from repair_workbench.snapshot import create_snapshot, snapshot_id, verify_snapshot

try:
    from repair_workbench.agent import run_agent
    from repair_workbench.diagnostic_agent import build_diagnostic_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field, ValidationError
    AGENT_AVAILABLE = True
except ImportError:
    AGENT_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmarks/ugc-diagnostics-dev-v1/case-01/source"


def passing_baseline(root):
    """Create immutable fixture content plus explicitly synthetic passing receipts."""
    repo = root / "source"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    for args in (("init", "-q"), ("add", "--all"), ("commit", "-qm", "UGC contract fixture")):
        subprocess.run([
            "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-C", str(repo), *args,
        ], check=True, capture_output=True, timeout=30)
    baseline = root / "baseline"
    manifest = create_snapshot(repo, baseline)
    reports = baseline / "reports"
    reports.mkdir()
    (reports / "pytest.xml").write_text(
        '<testsuite><testcase classname="map_export" name="public_semantics"/></testsuite>'
    )
    (reports / "pytest.log").write_text("Synthetic passing receipt for session contracts.\n")
    (reports / "result.json").write_text(json.dumps({
        "snapshot_id": snapshot_id(manifest), "status": "tests_passed",
        "cleanup_ok": True, "image_id": "unit-test-only",
        "counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
    }))
    return baseline


def probe_report(probe, *, allocation=False, cleanup=True):
    """A bounded synthetic observation; never described as measured performance."""
    row = {"semantic_ok": True}
    if probe == "cpu":
        row.update(total_calls=6000, top_functions=[{
            "file": "map_exporter.py", "line": 1,
            "function": "copy" if allocation else "export_map",
            "primitive_calls": 6000, "calls": 6000,
            "own_seconds": .01, "cumulative_seconds": .02,
        }])
    elif probe == "memory":
        row.update(peak_traced_bytes=30000000, measurement_scope="Synthetic Python allocation")
    else:
        row.update(median_seconds=.01, min_seconds=.009, max_seconds=.011)
    return {"status": "ok", "cleanup_ok": cleanup,
            "summary": {"semantic_ok": True, "scales": {"large": row}}}


if AGENT_AVAILABLE:
    class FeedbackModel(BaseChatModel):
        """Deterministic policy driven by actual ToolMessages and issued references.

        This tests wiring and feedback-dependent branching. It is neither a real
        LLM nor evidence that a model discovered a correct optimization.
        """

        calls: int = 0
        exposed: list[str] = Field(default_factory=list)
        probes: list[str] = Field(default_factory=list)
        source: dict = Field(default_factory=dict)
        hypothesis: str = ""
        probe_refs: list[str] = Field(default_factory=list)
        final_mode: str = "edit"

        @property
        def _llm_type(self):
            return "offline-diagnostic-feedback-policy"

        def bind_tools(self, tools, **kwargs):
            self.exposed = [item.name for item in tools]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls += 1
            latest = next((item for item in reversed(messages) if isinstance(item, ToolMessage)), None)
            data = json.loads(latest.content) if latest is not None else None
            name = latest.name if latest is not None else None
            if latest is None:
                action, args = "read_source", {"path": "map_exporter.py"}
            elif name == "read_source":
                self.source = data
                action, args = "record_hypothesis", {
                    "claim": "The current export work may contain repeated work.",
                    "status": "open", "evidence_refs": [data["evidence_ref"]],
                    "next_action": "Inspect the CPU observation to choose a discriminating probe.",
                }
            elif name == "record_hypothesis" and data["status"] == "open":
                self.hypothesis = data["hypothesis_id"]
                self.probes.append("cpu")
                action, args = "run_probe", {"probe": "cpu", "hypothesis_id": self.hypothesis}
            elif name == "run_probe":
                observation = data["observation"]
                self.probe_refs.append(data["evidence_ref"])
                if observation["probe"] == "cpu":
                    top = observation["summary"]["scales"]["large"]["top_functions"]
                    following = "memory" if any(row["function"] == "copy" for row in top) else "benchmark"
                    self.probes.append(following)
                    action, args = "run_probe", {"probe": following, "hypothesis_id": self.hypothesis}
                else:
                    action, args = "record_hypothesis", {
                        "claim": "The observations support investigating the selected repeated work.",
                        "status": "supported", "evidence_refs": self.probe_refs,
                        "hypothesis_id": self.hypothesis,
                        "next_action": "Submit a mechanism-only candidate for the independent verifier.",
                    }
            elif name == "record_hypothesis" and data["status"] == "supported":
                if self.final_mode == "no_change":
                    action, args = "conclude_diagnosis", {
                        "decision": "no_change", "explanation": "No justified change in this offline policy.",
                        "evidence_refs": self.probe_refs,
                    }
                else:
                    # Edit only text that was actually delivered by read_source.
                    # This harmless marker is not a reference optimization patch.
                    old = self.source["content"].splitlines(keepends=True)[0]
                    action, args = "submit_ref_edits", {
                        "edits": [{"source_ref": self.source["source_ref"], "old_text": old,
                                   "new_text": old + "# Offline mechanism candidate.\n"}],
                        "diagnosis": "Exercise issued source references and trusted verification routing.",
                    }
            else:
                response = AIMessage(
                    content="Offline policy finished; only the verifier receipt determines acceptance.",
                    usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                )
                return ChatResult(generations=[ChatGeneration(message=response)])
            response = AIMessage(content="", tool_calls=[{
                "name": action, "args": args, "id": f"feedback-{self.calls}",
            }], usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6})
            return ChatResult(generations=[ChatGeneration(message=response)])


class DiagnosticSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = passing_baseline(self.root)
        self.sequence = 0

    def session(self, **kwargs):
        self.sequence += 1
        return DiagnosticSession(self.baseline, self.root / f"diagnosis-{self.sequence}",
                                 {"map_exporter.py"}, **kwargs)

    def test_passing_baseline_is_diagnostic_only_and_invalid_receipts_rejected(self):
        session = self.session()
        self.assertEqual(session.before["status"], "tests_passed")
        self.assertEqual(session.initial_context()["task_type"], "performance_optimization")
        with self.assertRaises(ValueError):
            RepairSession(self.baseline, self.root / "old-repair", {"map_exporter.py"})
        path = self.baseline / "reports/result.json"
        original = json.loads(path.read_text())
        for mutation in ({"cleanup_ok": False}, {"image_id": ""}, {"status": "tests_failed"},
                         {"snapshot_id": "forged"}):
            path.write_text(json.dumps({**original, **mutation}))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.session()
        path.write_text(json.dumps(original))

    def test_source_reference_is_an_edit_handle_not_a_forged_evidence_handle(self):
        session = self.session()
        read = session.read_source("map_exporter.py", raw=True)
        self.assertIn(read["evidence_ref"], session.evidence)
        self.assertIn(read["source_ref"], session.source_refs)
        for refs in ([read["source_ref"]], ["E-forged"], []):
            with self.subTest(refs=refs), self.assertRaises(EditRejected):
                session.record_hypothesis("Claim", status="supported", evidence_refs=refs)
        with patch("repair_workbench.performance.verify_performance_candidate") as verify:
            response = session.submit_ref_edits([{
                "source_ref": "invented", "old_text": "anything", "new_text": "replacement",
            }], "forged edit")
        self.assertEqual(response["reason"], "invalid_source_ref")
        verify.assert_not_called()
        self.assertFalse(session.hypotheses)

    def test_supported_and_rejected_hypothesis_revisions_preserve_real_evidence(self):
        session = self.session()
        read = session.read_source("map_exporter.py", raw=True)
        hypothesis = session.record_hypothesis("Repeated work?", evidence_refs=[read["evidence_ref"]])
        with patch("repair_workbench.diagnostics.run_probe", return_value=probe_report("cpu")):
            observation = session.run_diagnostic("cpu", hypothesis["hypothesis_id"])
        for status in ("supported", "rejected"):
            updated = session.record_hypothesis(
                "Repeated work?", status=status, evidence_refs=[observation["evidence_ref"]],
                hypothesis_id=hypothesis["hypothesis_id"], next_action="Review alternate explanation.",
            )
            self.assertEqual(updated["trust"], "model_judgement_not_verified_fact")
        self.assertEqual(updated["revision"], 3)
        evidence = session.read_evidence(observation["evidence_ref"])
        self.assertEqual(evidence["snapshot_id"], snapshot_id(session.manifest))
        saved = json.loads((session.output / "evidence" / f'{evidence["evidence_ref"]}.json').read_text())
        digest = saved.pop("sha256")
        self.assertEqual(digest, hashlib.sha256(json.dumps(saved, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        events = [json.loads(line) for line in (session.output / "events.jsonl").read_text().splitlines()]
        self.assertEqual([row["status"] for row in events if row["event"] == "hypothesis_updated"],
                         ["open", "supported", "rejected"])

    def test_cached_probe_reuses_immutable_evidence_but_candidate_versions_do_not(self):
        session = self.session()
        source = (self.baseline / "workspace/map_exporter.py").read_text()
        with patch("repair_workbench.diagnostics.run_probe", return_value=probe_report("cpu")) as probe, \
                patch("repair_workbench.performance.verify_performance_candidate", return_value={
                    "status": "failed", "reason": "performance_threshold_not_met", "cleanup_ok": True,
                }):
            baseline = session.run_diagnostic("cpu")
            repeated = session.run_diagnostic("cpu")
            self.assertTrue(repeated["reused"])
            self.assertEqual(repeated["evidence_ref"], baseline["evidence_ref"])
            self.assertEqual(probe.call_count, 1)
            versions = {baseline["snapshot_id"]}
            for number in (1, 2):
                response = session.submit_candidate({"map_exporter.py": source + f"\n# candidate {number}\n"}, "offline")
                self.assertEqual(response["status"], "failed")
                observed = session.run_diagnostic("cpu", attempt=number)
                versions.add(observed["snapshot_id"])
            self.assertEqual(probe.call_count, 3)
            self.assertEqual(session.probe_runs, 3)
            self.assertEqual(len(versions), 3)
        self.assertEqual((self.baseline / "workspace/map_exporter.py").read_text(), source)

    def test_no_change_is_a_review_proposal_and_prevents_later_submission(self):
        session = self.session()
        ref = next(iter(session.evidence))
        with self.assertRaises(EditRejected):
            session.conclude_diagnosis("passed", "Claimed success", [ref])
        decision = session.conclude_diagnosis("no_change", "No supported optimization.", [ref])
        self.assertEqual(decision["trust"], "model_proposal_not_independent_acceptance")
        saved = session.save("running")
        self.assertEqual(saved["status"], "inconclusive")
        self.assertIsNone(saved["patch"])
        for function, args in ((session.submit_candidate, ({"map_exporter.py": "x=1\n"}, "late")),
                               (session.run_diagnostic, ("cpu",)),
                               (session.record_hypothesis, ("late",))):
            with self.subTest(function=function.__name__), self.assertRaises(EditRejected):
                function(*args)
        self.assertFalse(session.attempts)

    def test_exploration_limits_deadline_and_cancellation_stop_before_probe(self):
        session = self.session(max_probe_runs=3)
        source = (self.baseline / "workspace/map_exporter.py").read_text()
        with patch("repair_workbench.diagnostics.run_probe", side_effect=lambda *args, **kw: probe_report(args[3])) as probe, \
                patch("repair_workbench.performance.verify_performance_candidate", return_value={
                    "status": "failed", "reason": "performance_threshold_not_met", "cleanup_ok": True,
                }):
            for kind in ("cpu", "memory", "benchmark"):
                session.run_diagnostic(kind)
            session.submit_candidate({"map_exporter.py": source + "\n# candidate\n"}, "offline")
            with self.assertRaisesRegex(BudgetExceeded, "diagnostic_probe_limit"):
                session.run_diagnostic("cpu", attempt=1)
            self.assertEqual(probe.call_count, 3)
        cancelled = [False]
        session = self.session(cancellation_check=lambda: cancelled[0])
        cancelled[0] = True
        with patch("repair_workbench.diagnostics.run_probe") as probe:
            with self.assertRaisesRegex(BudgetExceeded, "cancel_requested"):
                session.run_diagnostic("cpu")
            probe.assert_not_called()
        session = self.session()
        session.started -= session.deadline_seconds + 1
        with patch("repair_workbench.diagnostics.run_probe") as probe:
            with self.assertRaisesRegex(BudgetExceeded, "deadline_exceeded"):
                session.run_diagnostic("cpu")
            probe.assert_not_called()

    def test_candidate_verification_cleanup_failure_hard_stops(self):
        session = self.session()
        read = session.read_source("map_exporter.py", raw=True)
        with patch("repair_workbench.performance.verify_performance_candidate", return_value={
                "status": "inconclusive", "reason": "container_cleanup_unconfirmed", "cleanup_ok": False,
        }):
            with self.assertRaisesRegex(BudgetExceeded, "cleanup"):
                session.submit_ref_edits([{
                    "source_ref": read["source_ref"], "old_text": read["content"],
                    "new_text": read["content"] + "\n# offline candidate\n",
                }], "offline")
        self.assertNotEqual(session.attempts[-1]["status"], "passed")

    def test_large_repository_initial_context_is_bounded_and_paths_are_pageable(self):
        repo = self.root / "source"
        paths = []
        for number in range(140):
            relative = f"modules/module_{number:03d}_" + "x" * 140 + ".py"
            target = repo / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"VALUE = {number}\n")
            paths.append(relative)
        for args in (("add", "--all"), ("commit", "-qm", "Large context fixture")):
            subprocess.run([
                "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-C", str(repo), *args,
            ], check=True, capture_output=True, timeout=30)
        baseline = self.root / "large-baseline"
        manifest = create_snapshot(repo, baseline)
        # Retain the explicitly synthetic passing-receipt convention of this module.
        shutil.copytree(self.baseline / "reports", baseline / "reports")
        report_path = baseline / "reports/result.json"
        report = json.loads(report_path.read_text())
        report["snapshot_id"] = snapshot_id(manifest)
        report_path.write_text(json.dumps(report))
        session = DiagnosticSession(baseline, self.root / "large-diagnosis",
                                    {"map_exporter.py", *paths})
        packet = session.initial_context()
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False)), 12000)
        self.assertTrue(packet["context_truncated"])
        self.assertEqual(packet["file_count"], len(session.files))
        self.assertEqual(packet["allowed_path_count"], len(session.allowed_paths))
        self.assertLess(len(packet["files"]), len(session.files))
        self.assertLess(len(packet["allowed_paths"]), len(session.allowed_paths))
        recovered = []
        for offset in range(0, len(session.files), 37):
            page = session.list_sources(offset=offset, limit=37)
            self.assertLessEqual(len(page["files"]), 37)
            self.assertEqual(page["total"], len(session.files))
            recovered.extend(page["files"])
        self.assertEqual(recovered, sorted(session.files))
        omitted = next(path for path in paths if path not in packet["files"])
        self.assertIn("VALUE =", session.read_source(omitted, raw=True)["content"])

    @unittest.skipUnless(AGENT_AVAILABLE, "agent dependencies required")
    def test_candidate_measurements_stay_on_disk_while_visible_receipts_are_summaries(self):
        session = self.session()
        read = session.read_source("map_exporter.py", raw=True)
        with patch("repair_workbench.diagnostic_agent.create_deep_agent") as create:
            build_diagnostic_agent(session, FeedbackModel())
        tools = {item.name: item for item in create.call_args.kwargs["tools"]}
        raw_marker = "RAW_MEASUREMENT_ONLY_ON_DISK"
        verdict = {
            "status": "passed", "reason": "passed_performance_scope", "scope": "public dev",
            "cleanup_ok": True, "changed_paths": ["map_exporter.py"],
            "metrics": {"large_speedup": .25, "large_memory_ratio": .75},
            "thresholds": {"minimum_speedup": .15, "max_memory_ratio": 1.25},
            "harness_sha256": "a" * 64, "workload_fingerprint": "b" * 64,
            "measurements": [{"sample": number, "raw": raw_marker * 100} for number in range(120)],
            "original_tests": {f"public_test_{number:05d}": "passed" for number in range(5000)},
        }

        def verify(baseline, candidate, destination, *args, **kwargs):
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "performance-verification.json").write_text(json.dumps(verdict))
            return verdict

        with patch("repair_workbench.performance.verify_performance_candidate", side_effect=verify):
            visible = tools["submit_ref_edits"].invoke({
                "edits": [{"source_ref": read["source_ref"], "old_text": read["content"],
                           "new_text": read["content"] + "\n# offline summary contract candidate\n"}],
                "diagnosis": "Offline summary/provenance contract only.",
            })
        self.assertEqual(visible["status"], "passed")
        self.assertLess(len(json.dumps(visible)), 6000)
        for field in ("measurements", "original_tests"):
            self.assertNotIn(field, visible)
        self.assertNotIn(raw_marker, json.dumps(visible))
        evidence = tools["read_evidence"].invoke({"evidence_ref": visible["evidence_ref"]})
        self.assertLess(len(json.dumps(evidence)), 6000)
        self.assertNotIn(raw_marker, json.dumps(evidence))
        for field in ("measurements", "original_tests"):
            self.assertNotIn(field, evidence["observation"])
        self.assertEqual(evidence["observation"]["metrics"], verdict["metrics"])
        artifact_path = session.output / evidence["observation"]["report"]
        full = json.loads(artifact_path.read_text())
        self.assertEqual(len(full["measurements"]), 120)
        self.assertEqual(len(full["original_tests"]), 5000)
        self.assertEqual(full["measurements"][0]["raw"], raw_marker * 100)
        candidate = session.output / "attempt-01/candidate"
        self.assertEqual(evidence["snapshot_id"], snapshot_id(verify_snapshot(candidate)))
        saved = json.loads((session.output / "result.json").read_text())
        self.assertEqual(saved["attempts"][0]["evidence_ref"], visible["evidence_ref"])
        self.assertEqual(len(saved["attempts"][0]["measurements"]), 120)


@unittest.skipUnless(AGENT_AVAILABLE, "agent dependencies required")
class DiagnosticAgentLoopTests(unittest.TestCase):
    setUp = DiagnosticSessionTests.setUp
    session = DiagnosticSessionTests.session

    def test_actual_tool_schemas_reject_boolean_and_string_integer_arguments(self):
        session = self.session()
        model = FeedbackModel()
        with patch("repair_workbench.diagnostic_agent.create_deep_agent") as create:
            build_diagnostic_agent(session, model)
        tools = {item.name: item for item in create.call_args.kwargs["tools"]}
        for tool_name, base, field, valid in (
            ("read_source", {"path": "map_exporter.py"}, "start_line", 1),
            ("run_probe", {"probe": "cpu"}, "attempt", 0),
        ):
            schema = tools[tool_name].args_schema
            self.assertEqual(getattr(schema.model_validate({**base, field: valid}), field), valid)
            for invalid in (True, False, str(valid)):
                with self.subTest(tool=tool_name, invalid=invalid):
                    with self.assertRaises(ValidationError):
                        schema.model_validate({**base, field: invalid})
                    # Exercise StructuredTool's own validation boundary as well.
                    with self.assertRaises(ValidationError):
                        tools[tool_name].invoke({**base, field: invalid})
        self.assertEqual(session.tool_calls, 0)
        self.assertEqual(session.probe_runs, 0)

    def run_feedback(self, *, allocation=False, strategy="adaptive", cleanup=True,
                     final_mode="edit", max_model_calls=12):
        session = self.session(strategy=strategy, max_model_calls=max_model_calls)
        model = FeedbackModel(final_mode=final_mode)
        observed = []

        def probe(*args, **kwargs):
            kwargs["execution_control"].check()
            observed.append(args[3])
            return probe_report(args[3], allocation=allocation, cleanup=cleanup)

        with patch("repair_workbench.diagnostics.run_probe", side_effect=probe), \
                patch("repair_workbench.performance.verify_performance_candidate", return_value={
                    "status": "passed", "reason": "passed_performance_scope", "cleanup_ok": True,
                }) as verify:
            result = run_agent(session, model, "Diagnose the local map export workload and preserve its behavior.")
        return session, model, result, observed, verify

    def test_real_deepagents_loop_uses_issued_refs_and_branches_on_probe_feedback(self):
        outcomes = []
        for allocation in (False, True):
            session, model, result, observed, verify = self.run_feedback(allocation=allocation)
            self.assertEqual(result["stop_reason"], "agent_finished", result)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(verify.call_count, 1)
            self.assertIn(model.source["source_ref"], session.source_refs)
            self.assertTrue(all(ref in session.evidence for ref in model.probe_refs))
            self.assertEqual(result["model_calls"], model.calls)
            self.assertEqual(result["token_usage"]["reports"], model.calls)
            self.assertEqual(result["token_usage"]["input_tokens"], model.calls * 4)
            self.assertIn("run_probe", model.exposed)
            for forbidden in ("task", "execute", "write_file", "submit_candidate"):
                self.assertNotIn(forbidden, model.exposed)
            outcomes.append(observed)
        self.assertEqual(outcomes, [["cpu", "benchmark"], ["cpu", "memory"]])

    def test_fixed_strategy_preruns_identical_tools_and_shared_budgets_in_order(self):
        adaptive = self.run_feedback(allocation=True)
        fixed = self.run_feedback(allocation=True, strategy="fixed")
        a_session, a_model, _, a_observed, _ = adaptive
        session, model, result, observed, _ = fixed
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(observed, ["cpu", "memory", "benchmark"])
        self.assertEqual(a_observed, ["cpu", "memory"])
        self.assertEqual(set(model.exposed), set(a_model.exposed))
        self.assertEqual(session.max_model_calls, a_session.max_model_calls)
        self.assertEqual(session.max_probe_runs, a_session.max_probe_runs)
        self.assertEqual(session.deadline_seconds, a_session.deadline_seconds)
        self.assertEqual(session.probe_runs, 3)
        events = [json.loads(line) for line in (session.output / "events.jsonl").read_text().splitlines()]
        first_model = next(i for i, event in enumerate(events) if event["event"] == "model_started")
        self.assertEqual([event["probe"] for event in events[:first_model]
                          if event["event"] == "diagnostic_started"], ["cpu", "memory", "benchmark"])

    def test_offline_model_no_change_cannot_turn_narrative_into_acceptance(self):
        _, _, result, _, verify = self.run_feedback(final_mode="no_change")
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["final_decision"]["decision"], "no_change")
        self.assertFalse(result["attempts"])
        verify.assert_not_called()

    def test_one_shared_model_call_limit_stops_the_diagnostic_tool_loop(self):
        session, model, result, observed, verify = self.run_feedback(max_model_calls=2)
        self.assertEqual(result["stop_reason"], "model_call_limit", result)
        self.assertEqual(session.model_calls, 2)
        self.assertEqual(model.calls, 2)
        self.assertFalse(observed)
        verify.assert_not_called()

    def test_unconfirmed_probe_cleanup_terminates_agent_before_more_tools(self):
        session, model, result, observed, verify = self.run_feedback(cleanup=False)
        self.assertEqual(result["stop_reason"], "diagnostic_cleanup_unconfirmed", result)
        self.assertEqual(observed, ["cpu"])
        self.assertEqual(model.calls, 3)
        self.assertEqual(result["status"], "inconclusive")
        self.assertTrue(session.closed)
        self.assertFalse(session.diagnostic_cache)
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
