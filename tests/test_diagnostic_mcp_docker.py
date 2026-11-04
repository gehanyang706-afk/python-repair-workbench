"""Real stdio MCP and Docker acceptance; scripted policies are not LLM scores."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

try:
    from mcp import Client  # noqa: F401
    from repair_workbench.agent import run_agent
except ImportError:
    raise unittest.SkipTest('pinned optional Agent and MCP dependencies are required')

from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.diagnostic_mcp_client import run_mcp_probe
from repair_workbench.diagnostics import run_probe
from repair_workbench.runner import run_tests
from repair_workbench.session import BudgetExceeded
from repair_workbench.snapshot import create_snapshot
from test_diagnostic_docker import KnownPatchModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from audit_diagnostic_run import audit_run


def baseline(root, *, infinite=False):
    source = root / 'source'
    shutil.copytree(ROOT / 'benchmarks/ugc-diagnostics-dev-v1/case-01/source', source,
                    ignore=shutil.ignore_patterns('__pycache__'))
    if infinite:
        (source / 'map_exporter.py').write_text('def export_map(objects, resources):\n    while True:\n        pass\n')
    for args in (('init', '-q'), ('add', '--all'), ('commit', '-qm', 'MCP execution fixture')):
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        '-C', str(source), *args], check=True, capture_output=True, timeout=30)
    target = root / 'baseline'
    create_snapshot(source, target)
    return target


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'explicit local MCP/Docker acceptance')
class DiagnosticMCPDockerTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'runtime').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'runtime')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1')

    def test_all_three_mcp_probes_preserve_local_input_and_output_identities(self):
        target = baseline(self.root)
        for probe in ('cpu', 'memory', 'benchmark'):
            local = run_probe(target, self.root / f'local-{probe}', self.image, probe)
            remote = run_mcp_probe(target, self.root / f'mcp-{probe}', self.image, probe)
            self.assertEqual(remote['status'], 'ok', remote)
            self.assertTrue(remote['cleanup_ok'])
            for key in ('snapshot_id', 'image_id', 'workload_id', 'workload_fingerprint', 'harness_sha256'):
                self.assertEqual(remote[key], local[key])
            for scale in ('small', 'large'):
                self.assertEqual(remote['summary']['scales'][scale]['fingerprint'],
                                 local['summary']['scales'][scale]['fingerprint'])
            transport = json.loads((self.root / f'mcp-{probe}-mcp/transport.json').read_text())
            self.assertEqual(transport['status'], 'completed')
            self.assertTrue(transport['cleanup_ok'])
            self.assertTrue(transport['journal_valid'])
            self.assertEqual({tool['name'] for tool in transport['tools']},
                             {'list_diagnostics', 'run_probe', 'read_report'})

    def test_root_cancellation_waits_for_real_container_cleanup(self):
        target = baseline(self.root, infinite=True)

        class Control:
            reserved_at = None
            resources = {}

            def check(self):
                if self.reserved_at is not None and time.monotonic() - self.reserved_at >= .2:
                    raise BudgetExceeded('cancel_requested')

            def container(self, name, state):
                self.resources[name] = state
                if state == 'reserved':
                    self.reserved_at = time.monotonic()

        control = Control()
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            run_mcp_probe(target, self.root / 'cancelled', self.image, 'cpu',
                          execution_control=control)
        self.assertTrue(control.resources)
        self.assertTrue(all(state == 'cleaned' for state in control.resources.values()))
        transport = json.loads((self.root / 'cancelled-mcp/transport.json').read_text())
        self.assertEqual(transport['status'], 'stopped')
        self.assertTrue(transport['cleanup_ok'])
        self.assertTrue((self.root / 'cancelled-mcp/cancel').is_file())
        for name in control.resources:
            found = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True, timeout=15)
            self.assertNotEqual(found.returncode, 0)

    def test_scripted_agent_selects_real_mcp_probe_then_independent_verification(self):
        # Preserve every measured verdict, including noise and failed thresholds.
        root = ROOT / 'runtime' / ('diagnostic-mcp-acceptance-' + uuid.uuid4().hex[:12])
        root.mkdir()
        target = baseline(root)
        before = run_tests(target, self.image)
        self.assertEqual(before['status'], 'tests_passed', before)
        self.assertTrue(before['cleanup_ok'])
        session = DiagnosticSession(target, root / 'agent', {'map_exporter.py'},
                                    diagnostic_transport='mcp', deadline_seconds=180)
        result = run_agent(session, KnownPatchModel(), 'Diagnose slow export using MCP evidence.')
        (root / 'acceptance.json').write_text(json.dumps({
            'mode': 'scripted_known_candidate_real_mcp_container', 'real_model_calls': 0,
            'unseen_evaluation': False, 'result': result}, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual(result['diagnostic_transport'], 'mcp')
        self.assertEqual(len(result['attempts']), 1, result)
        verdict = result['attempts'][0]
        self.assertIn(verdict['reason'], {'passed_registered_performance_scope',
            'measurement_noise_exceeded', 'memory_regression',
            'small_workload_regression', 'minimum_speedup_not_met'}, verdict)
        self.assertEqual(len(verdict['measurements']), 8)
        self.assertTrue(all(row['report']['cleanup_ok'] for row in verdict['measurements']))
        transport = json.loads((root / 'agent/diagnostics/probe-01-mcp/transport.json').read_text())
        self.assertEqual(transport['status'], 'completed')
        audit = audit_run(root / 'agent', root / 'acceptance.json')
        (root / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual(audit['counts']['mcp_jobs'], 1)
        self.assertEqual(audit['counts']['physical_probe_runs'], 9)
        print(json.dumps({'mcp_acceptance': str(root), 'measured_status': result['status'],
                          'measured_reason': result['reason'], 'real_model_calls': 0}))

    def test_two_mcp_services_and_cached_report_pages_in_the_product_agent(self):
        class ReportReviewModel(KnownPatchModel):
            pages: int = 0
            probe_ref: str = ''

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                previous = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
                if previous is not None and previous.name in {'run_probe', 'read_probe_report'}:
                    value = json.loads(previous.content)
                    if previous.name == 'run_probe':
                        self.probe_ref = value['evidence_ref']
                    if self.pages < 2:
                        self.pages += 1
                        action, args = 'read_probe_report', {
                            'evidence_ref': self.probe_ref, 'section': 'hotspots', 'limit': 1}
                    else:
                        action, args = 'record_hypothesis', {
                            'hypothesis_id': self.hypothesis, 'status': 'supported',
                            'claim': 'Review the measured export hotspot before independent verification.',
                            'evidence_refs': [self.probe_ref]}
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[{
                        'name': action, 'args': args, 'id': f'report-page-{self.pages}-{action}'}]))])
                return super()._generate(messages, stop, run_manager, **kwargs)

        root = ROOT / 'runtime' / ('mcp-runtime-acceptance-' + uuid.uuid4().hex[:12])
        root.mkdir()
        target = baseline(root)
        self.assertEqual(run_tests(target, self.image)['status'], 'tests_passed')
        session = DiagnosticSession(target, root / 'agent', {'map_exporter.py'},
                                    diagnostic_transport='mcp', deadline_seconds=180)
        result = run_agent(session, ReportReviewModel(), 'Review measured CPU evidence, then verify a known candidate.')
        (root / 'acceptance.json').write_text(json.dumps({
            'mode': 'scripted_known_candidate_two_mcp_services', 'real_model_calls': 0,
            'unseen_evaluation': False, 'result': result}, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual(len(result['attempts']), 1, result)
        self.assertEqual(result['tool_runtime']['cache_hits'], 1)
        events = [json.loads(line) for line in (session.output / 'events.jsonl').read_text().splitlines()]
        endpoints = {e['endpoint'] for e in events if e['event'] == 'tool_runtime' and e['mode'] == 'dispatch'}
        self.assertEqual(endpoints, {'local', 'mcp:diagnostics', 'mcp:reports'})
        audit = audit_run(session.output, root / 'acceptance.json')
        (root / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual(audit['counts']['report_queries'], 2)
        self.assertEqual(audit['counts']['physical_probe_runs'], 9)
        self.assertTrue(all(row['report']['cleanup_ok'] for row in result['attempts'][0]['measurements']))
        print(json.dumps({'mcp_runtime_acceptance': str(root), 'measured_status': result['status'],
                          'measured_reason': result['reason'], 'real_model_calls': 0}))


if __name__ == '__main__':
    unittest.main()
