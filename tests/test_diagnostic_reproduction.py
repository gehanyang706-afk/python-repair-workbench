"""Native graph and shared-session reproduction wiring, without provider calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from repair_workbench.agent import run_agent
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.diagnostic_agent import build_diagnostic_agent
from repair_workbench.edits import EditRejected
from repair_workbench.session import BudgetExceeded
from test_diagnosis import passing_baseline


class ReproductionPolicy(BaseChatModel):
    calls: int = 0
    exposed: list[str] = Field(default_factory=list)
    observations: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return 'offline-reproduction-wiring-policy'

    def bind_tools(self, tools, **kwargs):
        self.exposed = [t.name for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        latest = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
        if latest is None:
            name, args = 'propose_reproducer', {'code':
                'def run():\n    return {"claimed_status": "passed", "input_size": 8}\n'}
        else:
            data = json.loads(latest.content)
            self.observations.append(data)
            if latest.name == 'propose_reproducer':
                name, args = 'run_reproducer', {'reproducer_ref': data['reproducer_ref']}
            elif latest.name == 'run_reproducer':
                name, args = 'conclude_diagnosis', {
                    'decision': 'insufficient_evidence',
                    'explanation': 'Program output alone does not establish the business contract.',
                    'evidence_refs': [data['evidence_ref']]}
            else:
                response = AIMessage(content='Offline mechanism check finished.')
                return ChatResult(generations=[ChatGeneration(message=response)])
        response = AIMessage(content='', tool_calls=[{
            'name': name, 'args': args, 'id': 'repro-%s' % self.calls}])
        return ChatResult(generations=[ChatGeneration(message=response)])


class DiagnosticReproductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = passing_baseline(self.root)

    def session(self, **kwargs):
        return DiagnosticSession(self.baseline, self.root / 'agent', {'map_exporter.py'}, **kwargs)

    def test_disabled_tools_are_absent_and_cannot_start_docker(self):
        session = self.session()
        model = ReproductionPolicy()
        build_diagnostic_agent(session, model)
        self.assertNotIn('propose_reproducer', model.exposed)
        self.assertNotIn('run_reproducer', model.exposed)
        with patch('repair_workbench.reproduction.ReproductionWorkspace') as factory:
            with self.assertRaisesRegex(EditRejected, 'reproduction_disabled'):
                session.propose_reproducer('def run(): return 1')
        factory.assert_not_called()
        self.assertEqual(session.tool_calls, 1)

    def test_root_tool_deadline_and_conclusion_gates_precede_workspace_creation(self):
        session = self.session(enable_reproduction=True)
        with patch('repair_workbench.reproduction.ReproductionWorkspace') as factory:
            session.tool_calls = 60
            with self.assertRaisesRegex(BudgetExceeded, 'tool_call_limit'):
                session.propose_reproducer('def run(): return 1')
            session.tool_calls = 0
            session.final_decision = {'decision': 'insufficient_evidence'}
            with self.assertRaisesRegex(EditRejected, 'already_concluded'):
                session.propose_reproducer('def run(): return 1')
            session.final_decision = None
            session.cancellation_check = lambda: True
            with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
                session.propose_reproducer('def run(): return 1')
        factory.assert_not_called()

    def test_untrusted_result_stays_untrusted_on_disk_and_on_recall(self):
        session = self.session(enable_reproduction=True)
        with patch('repair_workbench.reproduction.ReproductionWorkspace') as factory:
            manager = factory.return_value
            manager.blocked_reason, manager.cleanup_ok = None, True
            manager.propose_reproducer.return_value = {'reproducer_ref': 'rp-fixed'}
            manager.run_reproducer.return_value = {
                'status': 'completed', 'cleanup_ok': True, 'input_integrity_ok': True,
                'trust': 'model_program_output', 'observation': {'status': 'passed'}}
            session.propose_reproducer('def run(): return {"status":"passed"}')
            result = session.run_reproducer('rp-fixed')
            self.assertEqual(manager.run_reproducer.call_args.kwargs['max_output_bytes'], 4096)
            self.assertIsNotNone(factory.call_args.kwargs['execution_control'])
            self.assertTrue(callable(factory.call_args.kwargs['admit']))
        self.assertEqual(session.tool_calls, 2)
        self.assertEqual(result['trust'], 'model_program_output')
        self.assertEqual(session.read_evidence(result['evidence_ref'])['trust'], 'model_program_output')
        from repair_workbench.diagnostic_delegation import investigation_context
        child_context, refs = investigation_context(session, json.dumps({
            'question': 'Review this observation without assuming its claim is correct.',
            'evidence_refs': [result['evidence_ref']]}))
        self.assertEqual(json.loads(child_context)['upstream_evidence'][0]['trust'], 'model_program_output')
        self.assertEqual(refs, [result['evidence_ref']])
        self.assertEqual(json.loads((session.output / 'evidence' /
            (result['evidence_ref'] + '.json')).read_text())['trust'], 'model_program_output')
        self.assertEqual(session.attempts, [])
        self.assertEqual(session.save('completed')['status'], 'inconclusive')

    def test_unconfirmed_cleanup_or_input_drift_stops_all_session_tools(self):
        session = self.session(enable_reproduction=True)
        with patch('repair_workbench.reproduction.ReproductionWorkspace') as factory:
            factory.return_value.blocked_reason, factory.return_value.cleanup_ok = None, True
            factory.return_value.run_reproducer.return_value = {
                'status': 'cleanup_unconfirmed', 'cleanup_ok': False,
                'input_integrity_ok': True, 'observation': 1}
            with self.assertRaisesRegex(BudgetExceeded, 'reproduction_cleanup_unconfirmed'):
                session.run_reproducer('rp-fixed')
        with self.assertRaisesRegex(EditRejected, 'session_closed'):
            session.read_source('map_exporter.py')
        self.assertTrue(session.closed)
        self.assertEqual(session.diagnostic_stop_reason, 'reproduction_cleanup_unconfirmed')
        self.assertEqual(session.evidence['E0002']['trust'], 'model_program_output')

    def test_execution_time_input_drift_stops_session_before_any_edit(self):
        session = self.session(enable_reproduction=True)
        with patch('repair_workbench.reproduction.ReproductionWorkspace') as factory:
            factory.return_value.blocked_reason, factory.return_value.cleanup_ok = None, True
            factory.return_value.run_reproducer.return_value = {
                'status': 'input_changed', 'cleanup_ok': True, 'input_integrity_ok': False}
            with self.assertRaisesRegex(BudgetExceeded, 'reproduction_inputs_changed'):
                session.run_reproducer('rp-fixed')
        self.assertTrue(session.closed)
        self.assertEqual(session.attempts, [])

    def test_invalid_options_are_strict(self):
        for kwargs in ({'enable_reproduction': 1}, {'max_repro_runs': True},
                       {'max_repro_programs': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.session(**kwargs)


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'requires existing local Docker image')
class DiagnosticReproductionGraphTests(unittest.TestCase):
    def test_native_graph_executes_program_in_container_without_accepting_its_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = passing_baseline(root)
            image = os.environ.get('REPAIR_TEST_IMAGE', 'repair-workbench-test:20261014-pinned')
            image_id = subprocess.check_output(['docker', 'image', 'inspect', image,
                '--format', '{{.Id}}'], text=True, timeout=15).strip()
            path = baseline / 'reports/result.json'
            path.write_text(json.dumps({**json.loads(path.read_text()), 'image_id': image_id}))
            session = DiagnosticSession(baseline, root / 'agent', {'map_exporter.py'},
                enable_reproduction=True, max_model_calls=8, deadline_seconds=90)
            model = ReproductionPolicy()
            result = run_agent(session, model, 'Reproduce the symptom; no trusted expected output supplied.')
            self.assertEqual(result['status'], 'inconclusive', result)
            self.assertEqual(result['stop_reason'], 'agent_finished', result)
            self.assertEqual(result['final_decision']['decision'], 'insufficient_evidence')
            self.assertEqual(session.tool_calls, 3)
            self.assertEqual(session.model_calls, 4)
            report = next(r for r in model.observations if r.get('kind') == 'reproduction_observation')
            self.assertEqual(report['trust'], 'model_program_output')
            self.assertEqual(report['observation']['observation']['claimed_status'], 'passed')
            self.assertTrue(report['observation']['cleanup_ok'])
            self.assertTrue(report['observation']['input_integrity_ok'])
            self.assertEqual(session.attempts, [])


if __name__ == '__main__':
    unittest.main()
