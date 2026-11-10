"""Public-baseline reproduction in the SWE graph; no provider or oracle calls."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from test_agent import ScriptedModel, call
from test_swebench_adapter import fixture_module
from repair_workbench import swebench_adapter as adapter
from repair_workbench.agent import run_agent
from repair_workbench.reproduction import ReproductionWorkspace
from repair_workbench.session import BudgetExceeded


IMAGE = 'sha256:' + 'a' * 64
PROGRAM = 'def run():\n    from calculator import add\n    return {"observed": add(2, 3)}\n'


class ReproduceThenSubmit(ScriptedModel):
    payload: dict = {}
    observation: dict = {}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.calls == 0:
            self.payload = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
            response = call('propose_reproducer', {'code': PROGRAM}, 'propose')
        else:
            receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            if self.calls == 1:
                response = call('run_reproducer', {'reproducer_ref': receipt['reproducer_ref']}, 'run')
            elif self.calls == 2:
                self.observation = receipt
                response = call('search_source', {'query': 'return a - b'}, 'search')
            else:
                response = call('submit_ref_edits', {'edits': [{
                    'source_ref': receipt['matches'][0]['source_ref'],
                    'old_text': 'return a - b', 'new_text': 'return a + b'}],
                    'diagnosis': 'Public baseline observation motivates addition.'}, 'submit')
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class SWEReproductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = fixture_module.prepare_fixture(self.root / 'fixture')
        self.workspace = self.root / 'fixture/agent-workspace'

    def session(self, *, enabled=True, **kwargs):
        return adapter.SWEbenchSession(self.workspace, self.root / 'agent',
            enable_reproduction=enabled, reproduction_image=IMAGE if enabled else None, **kwargs)

    @contextmanager
    def container(self, *, cleanup_ok=True, mutate=None, packet=None, sleeping=False):
        """Run a fixed JSON-only child, never repository or proposed Python code."""
        actual_popen, actual_run = subprocess.Popen, subprocess.run
        launches = []
        payload = packet or {'schema_version': 1, 'observation': {'claim': 'passed'}}
        def launch(args, *positional, **kwargs):
            if args[0] != 'docker':
                return actual_popen(args, *positional, **kwargs)
            launches.append(args)
            if mutate is not None:
                mutate()
            script = 'import time; time.sleep(60)' if sleeping else 'print(' + repr(json.dumps(payload)) + ')'
            return actual_popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        def cleanup(args, **kwargs):
            if args[0] != 'docker':
                return actual_run(args, **kwargs)
            missing = cleanup_ok and args[1] == 'inspect'
            return subprocess.CompletedProcess(args, 1 if missing else 0, '',
                                               'Error: No such object' if missing else '')
        with ExitStack() as stack:
            stack.enter_context(patch('repair_workbench.reproduction.resolve_image', return_value=IMAGE))
            stack.enter_context(patch('repair_workbench.reproduction.subprocess.Popen', side_effect=launch))
            stack.enter_context(patch('repair_workbench.reproduction.subprocess.run', side_effect=cleanup))
            yield launches

    def test_default_graph_and_initial_context_do_not_enable_reproduction(self):
        session = self.session(enabled=False)
        model = ScriptedModel(responses=[AIMessage(content='insufficient evidence')])
        run_agent(session, model, self.row['problem_statement'])
        self.assertNotIn('propose_reproducer', model.exposed)
        self.assertNotIn('run_reproducer', model.exposed)
        self.assertNotIn('swe_reproduction', json.loads((session.output / 'result.json').read_text()))
        self.assertFalse((session.output / 'reproduction').exists())

    def test_real_graph_uses_untrusted_public_observation_then_closes_on_submission(self):
        session, model = self.session(), ReproduceThenSubmit()
        with self.container(packet={'schema_version': 1, 'observation': {
                'claim': 'passed', 'trust': 'official', 'observed': -1}}) as launches, \
             patch.object(adapter, 'evaluate') as official:
            result = run_agent(session, model, self.row['problem_statement'])
        official.assert_not_called()
        self.assertEqual(result['status'], 'submitted', result)
        self.assertEqual(result['evaluation_status'], 'not_run')
        self.assertEqual(result['model_calls'], 4)
        self.assertEqual(result['tool_calls'], 4)
        self.assertEqual(model.observation['trust'], 'model_program_output')
        self.assertEqual(model.observation['observation']['claim'], 'passed')
        self.assertFalse(model.payload['initial_context']['tests_executed'])
        self.assertEqual(model.payload['initial_context']['reproduction']['target'],
                         'immutable public baseline only')
        self.assertNotIn('test_patch', json.dumps(model.payload))
        events = [json.loads(line) for line in (session.output / 'events.jsonl').read_text().splitlines()]
        model_observations = [event for event in events if event['event'] == 'model_finished']
        self.assertEqual(model_observations[0]['tools'], ['propose_reproducer'])
        self.assertEqual(model_observations[1]['tools'], ['run_reproducer'])
        command = launches[0]
        self.assertEqual(command[command.index('--entrypoint') + 1],
                         '/opt/miniconda3/envs/testbed/bin/python')
        self.assertIn('/testbed:rw,nosuid,nodev,size=1m', command)
        mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all(mount.endswith(',readonly') for mount in mounts))
        self.assertFalse(any('evaluator' in mount for mount in mounts))
        self.assertIn('return a - b', (session.baseline / 'workspace/calculator.py').read_text())
        with self.assertRaisesRegex(BudgetExceeded, 'candidate_submitted'):
            session.run_reproducer('unknown')

    def test_proposals_and_invalid_references_share_root_tool_allowance(self):
        session = self.session()
        with self.container() as launches:
            with self.assertRaisesRegex(ValueError, 'unknown_reproducer_ref'):
                session.run_reproducer('../evaluator')
            ref = session.propose_reproducer(PROGRAM)['reproducer_ref']
            self.assertEqual(session.tool_calls, 2)
            session.tool_calls = 60
            with self.assertRaisesRegex(BudgetExceeded, 'tool_call_limit'):
                session.run_reproducer(ref)
        self.assertEqual(launches, [])
        self.assertEqual(session.reproduction_workspace.runs, 0)

    def test_cleanup_unknown_blocks_models_and_all_other_session_tools(self):
        session = self.session()
        with self.container(cleanup_ok=False):
            ref = session.propose_reproducer(PROGRAM)['reproducer_ref']
            with self.assertRaisesRegex(BudgetExceeded, 'reproduction_cleanup_unconfirmed'):
                session.run_reproducer(ref)
        for action in (session.reserve_model, session.list_sources,
                       lambda: session.propose_reproducer(PROGRAM)):
            with self.assertRaisesRegex(BudgetExceeded, 'reproduction_cleanup_unconfirmed'):
                action()
        result = json.loads((session.output / 'result.json').read_text())
        self.assertFalse(result['swe_reproduction']['cleanup_ok'])
        self.assertTrue((session.output / 'reproduction/run-01/result.json').is_file())

    def test_source_drift_during_reproduction_blocks_further_agent_work(self):
        session = self.session()
        target = session.baseline / 'workspace/calculator.py'
        with self.container(mutate=lambda: target.write_text('changed = True\n')):
            ref = session.propose_reproducer(PROGRAM)['reproducer_ref']
            with self.assertRaisesRegex(BudgetExceeded, 'reproduction_inputs_changed'):
                session.run_reproducer(ref)
        with self.assertRaisesRegex(BudgetExceeded, 'reproduction_inputs_changed'):
            session.reserve_model()
        receipt = json.loads((session.output / 'reproduction/run-01/result.json').read_text())
        self.assertTrue(receipt['cleanup_ok'])
        self.assertFalse(receipt['input_integrity_ok'])

    def test_program_drift_before_launch_blocks_further_model_requests(self):
        session = self.session()
        with self.container() as launches:
            ref = session.propose_reproducer(PROGRAM)['reproducer_ref']
            (session.reproduction_workspace.output / ref / 'program.py').write_text('def run():\n    return 0\n')
            with self.assertRaises(ValueError):
                session.run_reproducer(ref)
            with self.assertRaisesRegex(BudgetExceeded, 'reproduction_inputs_changed'):
                session.reserve_model()
        self.assertFalse(launches)

    def test_cleanup_interrupted_without_receipt_cannot_be_reported_as_confirmed(self):
        session = self.session()
        with self.container():
            ref = session.propose_reproducer(PROGRAM)['reproducer_ref']
            with patch('repair_workbench.reproduction.subprocess.run', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    session.run_reproducer(ref)
        result = session.save('inconclusive')
        self.assertFalse(result['swe_reproduction']['cleanup_ok'])
        with self.assertRaisesRegex(BudgetExceeded, 'reproduction_cleanup_unconfirmed'):
            session.reserve_model()

    def test_cancellation_stops_running_program_and_records_cleanup(self):
        launched = []
        session = self.session(cancellation_check=lambda: bool(launched))
        with self.container(sleeping=True) as launches:
            ref = session.propose_reproducer(PROGRAM)['reproducer_ref']
            # The callback becomes true only after the mocked Docker worker starts.
            launched = launches
            with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
                session.run_reproducer(ref)
        receipt = json.loads((session.output / 'reproduction/run-01/result.json').read_text())
        self.assertEqual(receipt['status'], 'stopped')
        self.assertTrue(receipt['cleanup_ok'])
        self.assertTrue(receipt['input_integrity_ok'])

    def test_graph_cancel_cannot_overwrite_unconfirmed_cleanup_in_final_receipt(self):
        launched = []
        session = self.session(cancellation_check=lambda: bool(launched))
        with self.container(sleeping=True, cleanup_ok=False) as launches:
            launched = launches
            result = run_agent(session, ReproduceThenSubmit(), self.row['problem_statement'])
        self.assertEqual(result['stop_reason'], 'cancel_requested')
        self.assertEqual(result['model_calls'], 2)
        self.assertFalse(result['swe_reproduction']['cleanup_ok'])
        self.assertEqual(result['swe_reproduction']['stop_reason'], 'reproduction_cleanup_unconfirmed')
        saved = json.loads((session.output / 'result.json').read_text())
        self.assertFalse(saved['swe_reproduction']['cleanup_ok'])
        self.assertFalse((session.output / 'attempt-01').exists())

    def test_local_program_limit_does_not_reset_root_budget(self):
        session = self.session(max_repro_programs=1)
        with self.container():
            session.propose_reproducer(PROGRAM)
            with self.assertRaisesRegex(ValueError, 'reproduction_program_limit'):
                session.propose_reproducer(PROGRAM)
        self.assertEqual(session.tool_calls, 2)
        self.assertEqual(session.reproduction_workspace.proposals, 1)

    def test_invalid_schema_is_counted_as_reproduction_rejection(self):
        session = self.session()
        model = ScriptedModel(responses=[call('run_reproducer', {
            'reproducer_ref': 'not-issued', 'timeout_seconds': 'bad-timeout'}, 'invalid'),
            AIMessage(content='insufficient evidence')])
        run_agent(session, model, self.row['problem_statement'])
        stats = session.tool_statistics['by_tool']['run_reproducer']
        self.assertEqual(stats['calls'], 1)
        self.assertEqual(stats['schema_rejections'], 1)
        events = (session.output / 'events.jsonl').read_text()
        self.assertNotIn('bad-timeout', events)
        self.assertNotIn('not-issued', events)

    def test_controls_require_strict_boolean_counts_and_immutable_image(self):
        for kwargs in ({'enable_reproduction': 1}, {'max_repro_programs': True},
                       {'max_repro_runs': 0}, {'enable_reproduction': True},
                       {'enable_reproduction': True, 'reproduction_image': 'mutable:tag'},
                       {'reproduction_image': IMAGE}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                adapter.SWEbenchSession(self.workspace, self.root / 'agent', **kwargs)
        self.assertFalse((self.root / 'agent').exists())

    def test_qualified_entry_owns_image_and_does_not_accept_caller_override(self):
        checked = {'image_id': IMAGE, 'qualification_sha256': 'b' * 64,
                   'execution_policy': {'network': 'none'}, 'task': self.row}
        with patch.object(adapter, 'validate_qualification', return_value=checked), \
             patch.object(adapter, 'SWEbenchSession') as session_type, \
             patch('repair_workbench.agent.run_agent', return_value={'status': 'inconclusive'}):
            adapter.run_qualified_agent(self.workspace, self.root / 'agent', object(),
                self.root / 'qualification', enable_reproduction=True)
            self.assertEqual(session_type.call_args.kwargs['reproduction_image'], IMAGE)
            self.assertTrue(session_type.call_args.kwargs['enable_reproduction'])
            with self.assertRaisesRegex(ValueError, 'owned by environment qualification'):
                adapter.run_qualified_agent(self.workspace, self.root / 'other', object(),
                    self.root / 'qualification', enable_reproduction=True, reproduction_image=IMAGE)

    def test_runtime_profile_is_version_bound_and_not_model_selected(self):
        baseline = self.workspace / 'baseline'
        with patch('repair_workbench.reproduction.resolve_image', return_value=IMAGE):
            plain = ReproductionWorkspace(baseline, self.root / 'plain', IMAGE)
            swe = ReproductionWorkspace(baseline, self.root / 'swe', IMAGE,
                                        runtime_profile='swebench-testbed-v1')
            first = plain.propose_reproducer(PROGRAM)
            second = swe.propose_reproducer(PROGRAM)
            self.assertNotEqual(first['reproducer_ref'], second['reproducer_ref'])
            self.assertEqual(first['runtime_profile'], 'standalone-v1')
            self.assertEqual(second['runtime_profile'], 'swebench-testbed-v1')
            with self.assertRaisesRegex(ValueError, 'runtime profile'):
                ReproductionWorkspace(baseline, self.root / 'bad', IMAGE,
                                      runtime_profile='/host/python')


if __name__ == '__main__':
    unittest.main()
