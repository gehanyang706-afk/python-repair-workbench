"""Native Deep Agents fan-out with shared evidence, budget and real stdio MCP."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from repair_workbench.agent import run_agent
    from langchain_core.messages import AIMessage
    from test_agent import ScriptedModel
except ImportError:
    raise unittest.SkipTest('optional Agent dependencies required')

from repair_workbench.diagnosis import DiagnosticSession
from test_diagnosis import passing_baseline


def batch(name, arguments, count=2):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': arguments, 'id': f'parallel-{i}'}
                                           for i in range(count)])


class DiagnosticConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.baseline = passing_baseline(self.root)

    def session(self, concurrency=2, **kwargs):
        return DiagnosticSession(self.baseline, self.root / 'agent', {'map_exporter.py'},
                                 tool_concurrency=concurrency, **kwargs)

    def test_native_graph_executes_independent_reads_concurrently(self):
        session = self.session()
        barrier = threading.Barrier(2)
        original = session.source_outline
        workers = set()
        def concurrent(**kwargs):
            workers.add(threading.get_ident())
            barrier.wait(timeout=5)
            return original(**kwargs)
        concurrent.__name__ = 'source_outline'
        model = ScriptedModel(responses=[batch('source_outline', {'path': 'map_exporter.py'}), AIMessage(content='Done')])
        with patch.object(session, 'source_outline', concurrent):
            result = run_agent(session, model, 'Inspect independent read observations.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(len(workers), 2)
        self.assertEqual(result['tool_calls'], 2)
        self.assertEqual(result['tool_concurrency'], 2)

    def test_mixed_batch_is_rejected_before_any_mutation_or_read(self):
        session = self.session()
        message = AIMessage(content='', tool_calls=[
            {'name': 'read_source', 'args': {'path': 'map_exporter.py'}, 'id': 'read'},
            {'name': 'conclude_diagnosis', 'args': {'decision': 'no_change', 'explanation': 'premature',
                'evidence_refs': ['E0001']}, 'id': 'conclude'}])
        result = run_agent(session, ScriptedModel(responses=[message, AIMessage(content='Done')]), 'Inspect safely.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertIsNone(session.final_decision)
        self.assertFalse(session.source_refs)
        self.assertEqual(result['tool_calls'], 2)
        events = [json.loads(s) for s in (session.output / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e['event'] == 'parallel_batch_rejected' for e in events), 2)

    def test_mixed_delegation_cannot_start_child_or_consume_dispatch_count(self):
        session = self.session(use_investigator=True)
        message = AIMessage(content='', tool_calls=[
            {'name': 'read_source', 'args': {'path': 'map_exporter.py'}, 'id': 'read'},
            {'name': 'task', 'args': {'subagent_type': 'investigator', 'description': 'Read source'}, 'id': 'child'}])
        result = run_agent(session, ScriptedModel(responses=[message, AIMessage(content='Done')]), 'Inspect safely.')
        self.assertEqual(result['delegation_count'], 0, result)
        self.assertEqual(result['model_calls'], 2)
        self.assertFalse(session.source_refs)

    def test_concurrent_reads_issue_unique_evidence_and_respect_shared_tool_cap(self):
        session = self.session(4)
        model = ScriptedModel(responses=[batch('read_source', {'path': 'map_exporter.py'}, 65), AIMessage(content='Done')])
        result = run_agent(session, model, 'Read within the shared cap.')
        self.assertEqual(result['stop_reason'], 'tool_call_limit', result)
        self.assertEqual(result['tool_calls'], 60)
        self.assertEqual(len(session.source_refs), 60)
        self.assertEqual(len(session.evidence), 61)
        self.assertEqual(len(list((session.output / 'evidence').glob('*.json'))), 61)

    def test_invalid_concurrency_is_rejected_and_default_stays_serial(self):
        for value in (0, 5, True, '2'):
            with self.assertRaises(ValueError):
                self.session(value)
        session = DiagnosticSession(self.baseline, self.root / 'default', {'map_exporter.py'})
        self.assertEqual(session.tool_concurrency, 1)

    def test_native_graph_merges_duplicate_real_mcp_report_queries(self):
        try:
            import mcp
        except ImportError:
            self.skipTest('optional MCP SDK required')
        from test_diagnostic_reports import fixture_probe
        from repair_workbench.diagnostic_report_mcp import query_mcp_report
        session = self.session(diagnostic_transport='mcp')
        with patch('repair_workbench.diagnostic_mcp_client.run_mcp_probe', side_effect=fixture_probe):
            evidence = session.run_diagnostic('cpu')
        physical = []
        def execute(*args, **kwargs):
            physical.append(1)
            deadline = time.monotonic() + 5
            while session.tool_runtime.summary()['coalesced'] < 1:
                if time.monotonic() >= deadline:
                    raise TimeoutError('second graph query did not join')
                threading.Event().wait(.005)
            return query_mcp_report(*args, **kwargs)
        model = ScriptedModel(responses=[batch('read_probe_report', {
            'evidence_ref': evidence['evidence_ref'], 'section': 'hotspots', 'limit': 1}), AIMessage(content='Done')])
        with patch('repair_workbench.diagnostic_report_mcp.query_mcp_report', side_effect=execute):
            result = run_agent(session, model, 'Review identical immutable pages concurrently.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(len(physical), 1)
        self.assertEqual(result['tool_runtime']['coalesced'], 1)
        self.assertEqual(result['tool_calls'], 3)  # One probe admission and both logical page queries.


if __name__ == '__main__':
    unittest.main()
