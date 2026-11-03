"""Offline real-framework delegation contracts, not multi-agent quality scores."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile,
                        create_deep_agent, register_harness_profile)
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import ConfigDict, Field, StrictInt

from repair_workbench.agent import BudgetedModel, run_agent
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.diagnostic_delegation import (
    READONLY_TOOLS, build_delegation_middleware, build_investigator,
)
from repair_workbench.session import BudgetExceeded
from scripts.audit_diagnostic_run import AuditError, audit_run
from test_diagnosis import passing_baseline


def readonly_tools(session):
    @tool
    def list_sources(offset: StrictInt = 0, limit: StrictInt = 100) -> dict:
        """List visible immutable source paths."""
        return session.list_sources(offset=offset, limit=limit)

    @tool
    def read_source(path: str, start_line: StrictInt = 1, line_count: StrictInt = 120) -> dict:
        """Read immutable source and receive real source/evidence references."""
        return session.read_source(path, start_line=start_line, line_count=line_count, raw=True)

    @tool
    def source_outline(path: str) -> dict:
        """Read symbol metadata without importing source."""
        return session.source_outline(path)

    @tool
    def read_symbol(path: str, symbol: str) -> dict:
        """Read an existing source symbol."""
        return session.read_symbol(path, symbol)

    @tool
    def search_source(query: str, path: str | None = None) -> dict:
        """Search visible source text."""
        return session.search_source(query, path)

    @tool
    def read_evidence(evidence_ref: str) -> dict:
        """Read an already-issued evidence record."""
        return session.read_evidence(evidence_ref)

    return [list_sources, read_source, source_outline, read_symbol, search_source, read_evidence]


class Driver:
    """One deterministic driver shared by provider bind_tools model copies."""

    def __init__(self, *, child_error=False, main_error=False, repeat=False,
                 forbidden_child=False, forged=False, invalid_subagent=False,
                 oversized=False, cancel_on_child=None):
        self.child_error = child_error
        self.main_error = main_error
        self.repeat = repeat
        self.forbidden_child = forbidden_child
        self.forged = forged
        self.invalid_subagent = invalid_subagent
        self.oversized = oversized
        self.cancel_on_child = cancel_on_child
        self.main_calls = 0
        self.child_calls = 0
        self.calls = []
        self.findings = []


class DelegationModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    driver: object = Field(exclude=True)
    tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-delegation-script"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tool_names": [item.name for item in tools]})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        driver = self.driver
        child = any("你是只读代码调查子Agent" in str(message.content) for message in messages)
        role = "investigator" if child else "main"
        driver.calls.append({"role": role, "tools": self.tool_names, "messages": list(messages)})
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if child:
            driver.child_calls += 1
            if driver.cancel_on_child:
                driver.cancel_on_child()
            if driver.child_calls == 1 and driver.forbidden_child:
                calls = [{"name": name, "args": {}, "id": f"forbidden-{name}"}
                         for name in ("submit_ref_edits", "run_probe", "task", "read_file")]
                response = AIMessage(content="", tool_calls=calls)
            elif not results or all(message.status == "error" for message in results):
                invalid = driver.child_error and driver.child_calls == 1
                response = self.action("read_source", {
                    "path": "map_exporter.py", "start_line": "bad" if invalid else 1,
                }, "shared-round-id" if invalid else f"child-read-{driver.child_calls}")
            else:
                reads = [json.loads(message.content) for message in results
                         if message.name == "read_source" and message.status != "error"]
                if not reads:
                    response = self.action("read_source", {"path": "map_exporter.py"},
                                           f"child-read-{driver.child_calls}")
                else:
                    read = reads[-1]
                    finding = {"summary": "Read the export source; causal correctness still needs review.",
                               "evidence_refs": ["E-forged" if driver.forged else read["evidence_ref"]],
                               "source_refs": [read["source_ref"]],
                               "limitations": "Deterministic offline mechanism test, no performance claim."}
                    if driver.oversized:
                        finding["summary"] = "Unbounded report fragment. " * 1000
                    driver.findings.append(finding)
                    response = AIMessage(content=json.dumps(finding))
        else:
            driver.main_calls += 1
            if driver.main_calls == 1 or driver.repeat:
                response = self.action("task", {
                    "description": "Inspect map_exporter.py with read-only tools and return cited findings.",
                    "subagent_type": "general-purpose" if driver.invalid_subagent else "investigator",
                }, f"main-task-{driver.main_calls}")
                if driver.main_error and driver.main_calls == 1:
                    response.tool_calls.append({"name": "read_source", "args": {
                        "path": "map_exporter.py", "start_line": "bad",
                    }, "id": "shared-round-id", "type": "tool_call"})
            elif driver.main_error and driver.main_calls == 2:
                response = self.action("read_source", {"path": "map_exporter.py", "start_line": 1},
                                       "main-corrected")
            else:
                response = AIMessage(content="Delegated findings remain unverified; no candidate submitted.")
        response.usage_metadata = {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
        return ChatResult(generations=[ChatGeneration(message=response)])

    @staticmethod
    def action(name, args, identity):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identity}])


class DiagnosticDelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = passing_baseline(self.root)

    def session(self, *, enabled=True, delegations=1, **kwargs):
        return DiagnosticSession(
            self.baseline, self.root / "diagnosis", {"map_exporter.py"},
            use_investigator=enabled, max_delegations=delegations, **kwargs)

    def graph(self, session, model):
        tools = readonly_tools(session)
        register_harness_profile("repair-workbench:bounded", HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
        backend = StateBackend()
        middleware = [FilesystemMiddleware(backend=backend, tools=["read_file"])]
        subagents = []
        if session.use_investigator:
            subagents = [build_investigator(session, model, tools)]
            middleware.append(build_delegation_middleware(session))
        graph = create_deep_agent(
            model=BudgetedModel(inner=model, session=session, cache=False), tools=tools,
            system_prompt="Main Agent: use optional investigator when useful, preserve evidence boundaries.",
            backend=backend, middleware=middleware, subagents=subagents,
        )
        return graph, subagents

    def run_driver(self, driver, **session_kwargs):
        session = self.session(**session_kwargs)
        model = DelegationModel(driver=driver)
        graph, spec = self.graph(session, model)
        with patch("repair_workbench.agent.build_agent", return_value=graph):
            result = run_agent(session, model, "PARENT_PRIVATE_SENTINEL: investigate the local export workload.")
        return session, result, spec

    def test_true_task_tool_runs_isolated_readonly_child_and_shares_budget(self):
        driver = Driver()
        session, result, specs = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(session.delegation_count, 1)
        self.assertEqual(driver.main_calls, 2)
        self.assertEqual(driver.child_calls, 2)
        self.assertEqual(session.model_calls, 4)
        self.assertEqual(session.token_usage, {"input_tokens": 20, "output_tokens": 12, "reports": 4})
        children = [call for call in driver.calls if call["role"] == "investigator"]
        for call in children:
            self.assertEqual(set(call["tools"]), READONLY_TOOLS)
            self.assertNotIn("PARENT_PRIVATE_SENTINEL", str([message.content for message in call["messages"]]))
        self.assertEqual(specs[0]["mode"], "isolated")
        self.assertEqual(specs[0]["skills"], [])
        history = session.delegation_history[0]
        self.assertEqual(history["status"], "investigator_proposal")
        self.assertTrue(all(ref in session.evidence for ref in history["evidence_refs"]))
        self.assertFalse(session.attempts)
        self.assertEqual(result["status"], "inconclusive")
        events = [json.loads(line) for line in (session.output / "events.jsonl").read_text().splitlines()]
        for event in ("delegation_started", "delegation_finished"):
            self.assertEqual(sum(item["event"] == event for item in events), 1)
        self.assertEqual(sum(item["event"] == "model_started" and item.get("agent_role") == "investigator"
                             for item in events), 2)

    def test_production_diagnostic_agent_integrates_the_optional_task_tool(self):
        session = self.session()
        driver = Driver()
        result = run_agent(session, DelegationModel(driver=driver), "Inspect the export source with cited findings.")
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(result["delegation_count"], 1)
        self.assertEqual(result["delegation_history"][0]["status"], "investigator_proposal")
        self.assertEqual(driver.child_calls, 2)
        self.assertTrue(all(set(call["tools"]) == READONLY_TOOLS
                            for call in driver.calls if call["role"] == "investigator"))

    def test_delegation_duration_preserves_root_event_clock_and_audit_order(self):
        session = self.session()
        # A late delegation must not replace the root task's age with its short duration.
        session.started -= 5
        model = DelegationModel(driver=Driver())
        graph, _ = self.graph(session, model)
        with patch('repair_workbench.agent.build_agent', return_value=graph):
            result = run_agent(session, model, 'Inspect the export source with cited findings.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        path = session.output / 'events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        root_times = [event['elapsed_seconds'] for event in events]
        self.assertEqual(root_times, sorted(root_times))
        finish_index = next(index for index, event in enumerate(events)
                            if event['event'] == 'delegation_finished')
        finish = events[finish_index]
        self.assertGreaterEqual(finish['elapsed_seconds'], 5)
        self.assertGreaterEqual(finish['delegation_elapsed_seconds'], 0)
        self.assertLess(finish['delegation_elapsed_seconds'], finish['elapsed_seconds'])
        history = session.delegation_history[0]
        self.assertNotIn('elapsed_seconds', history)
        self.assertEqual(history['delegation_elapsed_seconds'], finish['delegation_elapsed_seconds'])
        self.assertEqual(audit_run(session.output)['audit_status'], 'passed')
        # The original strict audit rule must still reject a genuinely reversed root clock.
        events[finish_index]['elapsed_seconds'] = events[finish_index - 1]['elapsed_seconds'] - 1
        path.write_text(''.join(json.dumps(event) + '\n' for event in events))
        with self.assertRaisesRegex(AuditError, 'event_time_order_changed'):
            audit_run(session.output)

    def test_child_cannot_execute_forged_edit_probe_or_nested_task(self):
        driver = Driver(forbidden_child=True)
        with patch("repair_workbench.diagnostics.run_probe") as probe, \
                patch("repair_workbench.performance.verify_performance_candidate") as verifier:
            session, result, _ = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(session.delegation_count, 1)
        self.assertFalse(session.attempts)
        self.assertEqual(session.probe_runs, 0)
        probe.assert_not_called()
        verifier.assert_not_called()
        children = [call for call in driver.calls if call["role"] == "investigator"]
        for call in children:
            for name in ("submit_ref_edits", "run_probe", "task", "read_file", "write_file", "write_todos"):
                self.assertNotIn(name, call["tools"])

    def test_main_and_child_schema_recovery_rounds_do_not_overwrite_each_other(self):
        driver = Driver(child_error=True, main_error=True)
        session, result, specs = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(driver.main_calls, 3)
        self.assertEqual(driver.child_calls, 3)
        self.assertEqual(session.invalid_tool_recoveries, 1)
        self.assertEqual(specs[0]["model"].session.invalid_tool_recoveries, 1)
        self.assertIsNone(session.active_tool_round)
        self.assertNotEqual(session.processed_tool_results, specs[0]["model"].session.processed_tool_results)
        events = [json.loads(line) for line in (session.output / "events.jsonl").read_text().splitlines()]
        rejected = [item for item in events if item["event"] == "schema_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertEqual(sum(item.get("agent_role") == "investigator" for item in rejected), 1)
        self.assertTrue(session.closed)

    def test_small_root_model_cap_declines_child_and_retains_main_completion(self):
        driver = Driver()
        session, result, _ = self.run_driver(driver, max_model_calls=2)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(session.model_calls, 2)
        self.assertEqual(driver.child_calls, 0)
        self.assertEqual(session.delegation_count, 1)
        self.assertEqual(session.delegation_history[0]["status"], "delegation_declined")
        self.assertEqual(session.delegation_history[0]["stop_reason"], "main_model_calls_reserve")
        self.assertEqual(session.delegation_history[0]["model_calls_used"], 0)
        self.assertFalse(session.attempts)

    def test_cancel_in_child_uses_root_cancellation_signal(self):
        flag = [False]
        driver = Driver(cancel_on_child=lambda: flag.__setitem__(0, True))
        session, result, _ = self.run_driver(driver, cancellation_check=lambda: flag[0])
        self.assertEqual(result["stop_reason"], "cancel_requested", result)
        self.assertEqual(session.delegation_history[0]["status"], "stopped")
        self.assertEqual(driver.child_calls, 1)

    def test_deadline_expiring_in_child_stops_the_root_task(self):
        session = self.session()
        driver = Driver(cancel_on_child=lambda: setattr(
            session, "started", session.started - session.deadline_seconds - 1))
        model = DelegationModel(driver=driver)
        graph, _ = self.graph(session, model)
        with patch("repair_workbench.agent.build_agent", return_value=graph):
            result = run_agent(session, model, "Inspect the export source.")
        self.assertEqual(result["stop_reason"], "deadline_exceeded", result)
        self.assertEqual(session.delegation_history[0]["status"], "stopped")
        self.assertEqual(driver.child_calls, 1)

    def test_delegation_cap_never_refunds_completed_or_invalid_tasks(self):
        driver = Driver(repeat=True)
        session, result, _ = self.run_driver(driver, delegations=2)
        self.assertEqual(result["stop_reason"], "delegation_limit", result)
        self.assertEqual(session.delegation_count, 2)
        self.assertEqual(len(session.delegation_history), 2)
        self.assertTrue(all(item["status"] == "investigator_proposal" for item in session.delegation_history))

    def test_unknown_default_subagent_cannot_run_and_consumes_one_admitted_task(self):
        driver = Driver(invalid_subagent=True)
        session, result, _ = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(driver.child_calls, 0)
        self.assertEqual(session.delegation_count, 1)
        self.assertEqual(session.delegation_history[0]["status"], "unverified_invalid_findings")

    def test_forged_findings_references_do_not_become_verified_evidence(self):
        driver = Driver(forged=True)
        session, result, _ = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(session.delegation_history[0]["status"], "unverified_invalid_findings")
        parent_after_child = [call for call in driver.calls if call["role"] == "main"][-1]
        output = [message for message in parent_after_child["messages"] if isinstance(message, ToolMessage)][-1]
        data = json.loads(output.content)
        self.assertEqual(data["reason"], "unknown_or_missing_findings_references")
        self.assertNotIn("E-forged", session.evidence)

    def test_oversized_child_report_is_replaced_with_a_short_unverified_receipt(self):
        driver = Driver(oversized=True)
        session, result, _ = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(session.delegation_history[0]["status"], "unverified_invalid_findings")
        parent_after_child = [call for call in driver.calls if call["role"] == "main"][-1]
        output = [message for message in parent_after_child["messages"] if isinstance(message, ToolMessage)][-1]
        self.assertLess(len(output.content), 1000)
        self.assertNotIn("Unbounded report fragment", output.content)

    def test_enabled_investigator_is_optional_when_main_selects_no_delegation(self):
        driver = Driver()
        driver.main_calls = 1
        session, result, specs = self.run_driver(driver)
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(len(specs), 1)
        self.assertEqual(driver.child_calls, 0)
        self.assertIn("task", driver.calls[0]["tools"])
        self.assertEqual(session.delegation_count, 0)
        self.assertFalse(session.delegation_history)

    def test_disabled_configuration_exposes_no_task_and_requires_no_child(self):
        session = self.session(enabled=False)
        driver = Driver()
        driver.main_calls = 1  # The deterministic main policy ends without requesting work.
        model = DelegationModel(driver=driver)
        result = run_agent(session, model, "No optional investigation requested.")
        self.assertEqual(result["stop_reason"], "agent_finished", result)
        self.assertEqual(driver.child_calls, 0)
        self.assertNotIn("task", driver.calls[0]["tools"])
        self.assertEqual(session.delegation_count, 0)

    def test_builder_rejects_extra_tools_double_wrapping_and_invalid_caps(self):
        session = self.session()
        model = DelegationModel(driver=Driver())
        tools = readonly_tools(session)
        with self.assertRaises(ValueError):
            build_investigator(session, model, tools[:-1])
        with self.assertRaises(ValueError):
            build_investigator(session, model, tools + [tools[0]])
        with self.assertRaises(ValueError):
            build_investigator(session, BudgetedModel(inner=model, session=session), tools)
        for cap in (True, "1", 0, 3):
            session.max_delegations = cap
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                build_delegation_middleware(session)


if __name__ == "__main__":
    unittest.main()
