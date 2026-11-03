"""Real graph feedback branches with synthetic executors, not model evaluations."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from repair_workbench.diagnostic_eval_policy import POLICY_ID, create_scripted_model
    from repair_workbench.agent import run_agent
    from repair_workbench.diagnosis import DiagnosticSession
    from langchain_core.messages import HumanMessage, ToolMessage
except ImportError:
    raise unittest.SkipTest("optional pinned Agent dependencies are required")

from test_diagnosis import passing_baseline, probe_report


class DiagnosticEvaluationPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.baseline = passing_baseline(self.root)
        self.sequence = 0

    def run_policy(self, function="export_map", strategy="adaptive", failed_cpu=False, **options):
        self.sequence += 1
        session = DiagnosticSession(self.baseline, self.root / f"run-{self.sequence}",
                                    {"map_exporter.py"}, strategy=strategy, **options)
        # Shift the reference numbering: the policy must consume issued references,
        # never rely on a fixture-specific E0002 or hypothesis-number shortcut.
        session._add_evidence("unrelated_setup", {"not_used_by_policy": True}, session.before["snapshot_id"])
        model = create_scripted_model()
        invoked = []

        def probe(snapshot, destination, image, kind, *args, **kwargs):
            kwargs["execution_control"].check()
            invoked.append(kind)
            result = copy.deepcopy(probe_report(kind))
            if kind == "cpu":
                result["summary"]["scales"]["large"]["top_functions"][0]["function"] = function
                if failed_cpu:
                    result["status"] = "report_error"
            destination.mkdir(parents=True)
            (destination / "result.json").write_text(json.dumps(result))
            return result

        with patch("repair_workbench.settings.ModelSettings.load", side_effect=AssertionError("model config forbidden")) as settings, \
                patch("repair_workbench.settings.ModelSettings.create_model", side_effect=AssertionError("provider forbidden")) as provider, \
                patch("repair_workbench.model_transport.run_request_worker", side_effect=AssertionError("API worker forbidden")) as request, \
                patch("repair_workbench.diagnostics.run_probe", side_effect=probe), \
                patch("repair_workbench.performance.verify_performance_candidate", side_effect=AssertionError("no candidate permitted")) as verifier:
            result = run_agent(session, model, "Exercise the registered diagnostic policy without proposing edits.")
        for mock in (settings, provider, request, verifier):
            mock.assert_not_called()
        events = [json.loads(line) for line in (session.output / "events.jsonl").read_text().splitlines()]
        return session, model, result, invoked, events

    def test_unique_identity_and_factory_creates_fresh_unbilled_policy(self):
        self.assertEqual(POLICY_ID, "diagnostic_probe_policy_v1")
        first, second = create_scripted_model(), create_scripted_model()
        self.assertIsNot(first, second)
        self.assertEqual(first._llm_type, POLICY_ID)
        first.bind_tools([{"name": name} for name in ("read_source", "record_hypothesis", "run_probe", "conclude_diagnosis")])
        message = first.invoke([HumanMessage(content="start offline policy")])
        self.assertIsNone(message.usage_metadata)
        self.assertNotIn("token_usage", message.response_metadata)
        self.assertEqual(message.tool_calls[0]["name"], "read_source")
        self.assertEqual(second.calls, 0)
        self.assertEqual(second.evidence_refs, [])

    def test_cpu_feedback_selects_followup_and_never_claims_repair_or_health_success(self):
        for function, expected in (("copy", "memory"), ("<listcomp>", "memory"),
                                   ("deepcopy", "memory"), ("export_map", "benchmark")):
            with self.subTest(function=function):
                session, model, result, invoked, events = self.run_policy(function)
                self.assertEqual(invoked, ["cpu", expected])
                self.assertEqual(model.selected_followup, expected)
                self.assertEqual(result["stop_reason"], "agent_finished", result)
                self.assertEqual(result["status"], "inconclusive")
                self.assertEqual(result["attempts"], [])
                self.assertEqual(result["final_decision"]["decision"], "insufficient_evidence")
                self.assertEqual(result["diagnostic_probe_runs"], 2)
                self.assertEqual(result["model_calls"], 6)
                self.assertFalse(result["usage_complete"])
                self.assertEqual(result["usage_missing_calls"], 6)
                self.assertEqual(result["token_usage"]["reports"], 0)
                self.assertTrue(all(not row["usage_reported"] and row["input_tokens"] is None
                                    and row["output_tokens"] is None for row in events if row["event"] == "model_finished"))
                starts = [row["tool"] for row in events if row["event"] == "tool_started"]
                self.assertEqual(starts, ["read_source", "record_hypothesis", "run_probe", "run_probe", "conclude_diagnosis"])
                self.assertTrue(all(item["status"] == "open" for item in result["hypotheses"].values()))
                refs = result["final_decision"]["evidence_refs"]
                self.assertEqual(refs, model.evidence_refs)
                self.assertEqual(len(refs), 3)
                self.assertTrue(all(ref in session.evidence for ref in refs))
                self.assertEqual([session.evidence[ref]["kind"] for ref in refs],
                                 ["source_excerpt", "diagnostic_probe", "diagnostic_probe"])
                self.assertNotIn("E0002", refs)

    def test_same_policy_uses_fixed_prerun_cache_instead_of_repeating_measurements(self):
        for function, expected in (("copy", "memory"), ("export_map", "benchmark")):
            with self.subTest(function=function):
                session, model, result, invoked, events = self.run_policy(function, strategy="fixed")
                self.assertEqual(invoked, ["cpu", "memory", "benchmark"])
                self.assertEqual(result["diagnostic_probe_runs"], 3)
                self.assertEqual(result["model_calls"], 6)
                self.assertEqual(model.selected_followup, expected)
                reused = [row for row in events if row["event"] == "diagnostic_reused"]
                self.assertEqual([row["probe"] for row in reused], ["cpu", expected])
                self.assertEqual(result["final_decision"]["decision"], "insufficient_evidence")
                self.assertEqual(result["status"], "inconclusive")
                self.assertTrue(all(ref in session.evidence for ref in model.evidence_refs))
                self.assertEqual(result["attempts"], [])

    def test_failed_cpu_is_not_treated_as_a_causal_signal(self):
        _, model, result, invoked, _ = self.run_policy("copy", failed_cpu=True)
        self.assertEqual(invoked, ["cpu"])
        self.assertIsNone(model.selected_followup)
        self.assertEqual(result["final_decision"]["decision"], "insufficient_evidence")
        self.assertIn("not root-cause evidence", result["final_decision"]["explanation"])
        self.assertEqual(result["status"], "inconclusive")

    def test_shared_request_budget_still_stops_the_offline_policy(self):
        _, _, result, invoked, _ = self.run_policy(max_model_calls=3)
        self.assertEqual(result["stop_reason"], "model_call_limit")
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["attempts"], [])
        self.assertEqual(invoked, ["cpu"])
        self.assertEqual(result["status"], "inconclusive")

    def test_unusable_tool_message_never_forges_an_evidence_reference(self):
        model = create_scripted_model()
        model.bind_tools([{"name": name} for name in ("read_source", "record_hypothesis", "run_probe", "conclude_diagnosis")])
        for value in ("not JSON", "[]", '{"status":"rejected"}'):
            with self.subTest(value=value):
                message = model.invoke([ToolMessage(content=value, name="read_source", tool_call_id="actual-call")])
                self.assertEqual(message.tool_calls, [])
                self.assertEqual(model.evidence_refs, [])
                self.assertIsNone(message.usage_metadata)
        observed_reference = "nonstandard-issued-source-29"
        source = model.invoke([ToolMessage(content=json.dumps({
            "content": "observed source", "evidence_ref": observed_reference,
        }), name="read_source", tool_call_id="issued-source-call")])
        self.assertEqual(source.tool_calls[0]["args"]["evidence_refs"], [observed_reference])
        message = model.invoke([ToolMessage(content='{"status":"rejected"}', name="record_hypothesis", tool_call_id="actual-call")])
        self.assertEqual(message.tool_calls[0]["name"], "conclude_diagnosis")
        self.assertEqual(message.tool_calls[0]["args"]["evidence_refs"], [observed_reference])

    def test_unavailable_tool_catalog_stops_without_any_action(self):
        model = create_scripted_model()
        model.bind_tools([{"name": "submit_ref_edits"}])
        message = model.invoke([HumanMessage(content="start")])
        self.assertEqual(message.tool_calls, [])
        self.assertIn("lacks the registered diagnostic tools", message.content)


if __name__ == "__main__":
    unittest.main()
