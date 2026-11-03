"""Framework tools share the diagnostic execution cap; proposals are not executions."""
import json
from pathlib import Path
import tempfile
import unittest

try:
    from repair_workbench.agent import run_agent
    from repair_workbench.diagnosis import DiagnosticSession
    from langchain_core.messages import AIMessage
except ImportError:
    raise unittest.SkipTest("requires the pinned optional Agent dependencies")

from test_agent import ScriptedModel
from test_agent import fixture as failed_baseline
from test_diagnosis import passing_baseline
from repair_workbench.session import RepairSession


class DiagnosticFrameworkBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = passing_baseline(self.root)

    def run_burst(self, name, actions, **options):
        session = DiagnosticSession(self.baseline, self.root / name, {"map_exporter.py"}, **options)
        model = ScriptedModel(responses=[AIMessage(content="", tool_calls=[
            {"name": tool, "args": arguments, "id": f"framework-{number}"}
            for number, (tool, arguments) in enumerate(actions)
        ]), AIMessage(content="Stop after the diagnostic observations.")])
        result = run_agent(session, model, "Inspect bounded context without modifying sources.")
        events = [json.loads(line) for line in (session.output / "events.jsonl").read_text().splitlines()]
        executed = [item for item in events if item["event"] == "tool_started"]
        self.assertEqual(result["stop_reason"], "tool_call_limit", result)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["tool_calls"], 60)
        self.assertEqual(len(executed), 60)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["delegation_count"], 0)
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["diagnostic_probe_runs"], 0)
        self.assertEqual(result["tool_statistics"]["calls"], len(actions))
        return result, executed

    def test_read_file_budget_is_enforced_with_default_skills_or_investigator_options(self):
        for name, options in (("default", {}), ("skills", {"use_skills": True}),
                              ("investigator", {"use_investigator": True})):
            with self.subTest(configuration=name):
                result, executed = self.run_burst(
                    "read-" + name, [("read_file", {"file_path": "/missing.txt"})] * 65, **options)
                self.assertEqual(result["tool_statistics"]["by_tool"]["read_file"]["calls"], 65)
                self.assertEqual(sum(item["tool"] == "read_file" for item in executed), 60)

    def test_write_todos_budget_is_enforced_with_default_skills_or_investigator_options(self):
        for name, options in (("default", {}), ("skills", {"use_skills": True}),
                              ("investigator", {"use_investigator": True})):
            with self.subTest(configuration=name):
                # The SDK rejects multiple write_todos in one model turn before
                # tool execution. One valid planning call plus file reads tests
                # the actual shared execution cap without counting rejections.
                actions = [("write_todos", {"todos": [{"content": "Review current evidence", "status": "pending"}]})]
                actions += [("read_file", {"file_path": "/missing.txt"})] * 64
                result, executed = self.run_burst("todos-" + name, actions, **options)
                self.assertEqual(result["tool_statistics"]["by_tool"]["write_todos"]["calls"], 1)
                self.assertEqual(sum(item["tool"] == "write_todos" for item in executed), 1)

    def test_framework_reads_and_business_reads_consume_one_shared_cap(self):
        actions = [("read_source", {"path": "map_exporter.py"})] * 10
        actions += [("read_file", {"file_path": "/missing.txt"})] * 55
        result, executed = self.run_burst("mixed", actions)
        self.assertEqual(sum(item["tool"] == "read_source" for item in executed), 10)
        self.assertEqual(sum(item["tool"] == "read_file" for item in executed), 50)
        self.assertEqual(result["tool_statistics"]["by_tool"]["read_file"]["calls"], 55)

    def test_parallel_todo_rejections_are_not_counted_as_executions(self):
        session = DiagnosticSession(self.baseline, self.root / 'rejected-todos', {'map_exporter.py'})
        model = ScriptedModel(responses=[AIMessage(content='', tool_calls=[
            {'name': 'write_todos', 'args': {'todos': [{'content': 'Inspect', 'status': 'pending'}]},
             'id': f'rejected-{number}'} for number in range(65)
        ]), AIMessage(content='Stop without executing rejected parallel planning calls.')])
        result = run_agent(session, model, 'Inspect evidence.')
        self.assertEqual(result['stop_reason'], 'agent_finished')
        self.assertEqual(result['tool_calls'], 0)
        self.assertEqual(result['tool_statistics']['by_tool']['write_todos']['calls'], 65)


class RepairFrameworkBudgetTests(unittest.TestCase):
    def check_burst(self, tool, arguments):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = failed_baseline(root)
            session = RepairSession(baseline, root / 'agent', {'pricing.py'})
            actions = [{'name': tool, 'args': arguments, 'id': f'legacy-{number}'} for number in range(65)]
            if tool == 'write_todos':
                actions = actions[:1] + [{'name': 'read_file', 'args': {'file_path': '/missing.txt'},
                                          'id': f'legacy-file-{number}'} for number in range(64)]
            model = ScriptedModel(responses=[AIMessage(content='', tool_calls=[
                *actions
            ]), AIMessage(content='Stopped.')])
            result = run_agent(session, model, 'Investigate within the shared tool cap.')
            self.assertEqual(result['stop_reason'], 'tool_call_limit')
            self.assertEqual(result['tool_calls'], 60)
            self.assertEqual(result['model_calls'], 1)
            self.assertEqual(result['tool_statistics']['by_tool'][tool]['calls'],
                             1 if tool == 'write_todos' else 65)

    def test_legacy_file_tools_cannot_bypass_shared_cap(self):
        self.check_burst('read_file', {'file_path': '/missing.txt'})

    def test_legacy_planning_tools_cannot_bypass_shared_cap(self):
        self.check_burst('write_todos', {'todos': [{'content': 'Inspect', 'status': 'pending'}]})


if __name__ == "__main__":
    unittest.main()
