"""Integration of plan identity, provider reservations and independent verdicts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from repair_workbench.evaluation_batch import FrozenEvaluationBatch
from repair_workbench.experiment_manifest import create_manifest
from repair_workbench.model_transport import ManagedTongyi
from repair_workbench.session import BudgetExceeded


class EvaluationBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source.py'
        self.source.write_text('value=1\n')
        self.config = {'model': {'id': 'offline-model', 'region': 'cn-beijing',
            'input_token_limit': 100, 'output_token_limit': 100},
            'tools': ['read_source', 'submit_ref_edits'],
            'limits': {'batch_tokens': 2000, 'run_tokens': 1000,
                'requests_per_run': 4, 'batch_requests': 8,
                'run_seconds': 60, 'batch_seconds': 300}}
        self.manifest = self.plan()

    def plan(self):
        return create_manifest(self.config, [{'id': 'real-format-dev', 'cluster_id': 'issue-1',
            'split': 'dev', 'public': {'repo': 'sample/project', 'base_commit': 'a' * 40,
            'problem_statement': 'Public description'},
            'evaluator': {'hidden_patch': 'PRIVATE_ANSWER'}}],
            [{'id': 'a', 'policy': 'on_demand'}, {'id': 'b', 'policy': 'issue_evidence'}],
            {'source': self.source})

    def batch(self, **kwargs):
        return FrozenEvaluationBatch(self.manifest, self.root / 'batch',
            expected_sha256=self.manifest['manifest_sha256'],
            current_config=lambda: copy.deepcopy(self.config), **kwargs)

    def test_full_pair_links_visible_prompt_calls_candidates_and_independent_verdict(self):
        batch = self.batch()
        requests = []
        def provider(messages, *args, **kwargs):
            requests.append(str(messages))
            state = json.loads((batch.output / 'token-ledger.json').read_text())
            self.assertEqual(state['calls'][-1]['state'], 'reserved')
            return AIMessage(content='candidate', usage_metadata={
                'input_tokens': 50, 'output_tokens': 10, 'total_tokens': 60})
        def execute(context):
            self.assertNotIn('evaluator', context.task)
            context.emit('tool_receipt', call_id='read-1', snapshot='baseline',
                         proposed=1, executed=1, dispatched=0, cache_hit=True)
            model = context.metered_model('offline-private-value')
            model.invoke_controlled([HumanMessage(content=context.task['problem_statement'])], object())
            context.emit('candidate_submitted', candidate_sha256='b' * 64)
            context.emit('independent_verdict', report_ref='external-report.json', outcome='failed')
            return {'outcome': 'failed', 'cleanup_ok': True, 'report_ref': 'external-report.json'}
        with patch.object(ManagedTongyi, 'invoke_controlled', side_effect=provider):
            report = batch.run(execute)
        self.assertEqual([r['outcome'] for r in report['rows']], ['failed', 'failed'])
        self.assertEqual(report['budget']['charged_or_reserved_tokens'], 120)
        self.assertNotIn('PRIVATE_ANSWER', str(requests))
        for row in report['rows']:
            events = [json.loads(s) for s in (batch.output / row['run_id'] / 'events.jsonl').read_text().splitlines()]
            self.assertEqual([e['kind'] for e in events], ['run_started', 'tool_receipt',
                'candidate_submitted', 'independent_verdict', 'run_completed'])
            self.assertTrue(all(e['run_id'] == row['run_id'] for e in events))
        with self.assertRaisesRegex(RuntimeError, 'already_started'):
            batch.run(execute)

    def test_source_drift_midrun_prevents_provider_and_preserves_unstarted_rows(self):
        batch = self.batch()
        def execute(context):
            self.source.write_text('value=2\n')
            context.metered_model('offline-private-value').invoke_controlled([], object())
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            report = batch.run(execute)
        provider.assert_not_called()
        self.assertEqual(batch.ledger.state['calls'], [])
        self.assertEqual([r['state'] for r in report['rows']], ['error', 'not_run'])
        self.assertEqual(report['stop_reason'], 'ValueError')

    def test_frequent_execution_checks_do_not_rescan_but_model_gate_rejects_drift(self):
        batch = self.batch()
        checks = []
        batch._current_config = lambda: checks.append(1) or copy.deepcopy(self.config)
        def execute(context):
            control = context.execution_control()
            before = len(checks)
            for _ in range(1000):
                control.check()
            self.assertEqual(len(checks), before)
            self.source.write_text('value=2\n')
            control.check()  # Liveness is separate; no new inference can use drifted data.
            context.metered_model('offline-private-value').invoke_controlled([], object())
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            report = batch.run(execute)
        provider.assert_not_called()
        self.assertEqual(batch.ledger.state['calls'], [])
        self.assertEqual(report['stop_reason'], 'ValueError')
        self.assertEqual([r['state'] for r in report['rows']], ['error', 'not_run'])

    def test_execution_control_uses_current_phase_deadline(self):
        self.config['limits']['final_evaluation_seconds'] = 120
        self.manifest = self.plan()
        now = [0]
        batch = self.batch(clock=lambda: now[0])
        def execute(context):
            control = context.execution_control()
            now[0] += 50
            context.begin_evaluation('frozen-candidate')
            now[0] += 70
            control.check()  # Inference's 60s limit no longer governs final scoring.
            now[0] += 51
            control.check()
        report = batch.run(execute)
        self.assertEqual(report['stop_reason'], 'final_evaluation_deadline')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_execution_control_enforces_cancellation_and_unknown_usage(self):
        batch = self.batch()
        def execute(context):
            call = batch.ledger.reserve(context.run_id)
            batch.ledger.unknown(call, 'synthetic_unknown_receipt')
            context.execution_control().check()
        report = batch.run(execute)
        self.assertEqual(report['stop_reason'], 'batch_usage_unknown')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_execution_control_cancellation_is_immediate(self):
        cancelled = [False]
        batch = self.batch(cancelled=lambda: cancelled[0])
        def execute(context):
            cancelled[0] = True
            context.execution_control().check()
        report = batch.run(execute)
        self.assertEqual(report['stop_reason'], 'cancel_requested')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_unknown_provider_usage_stops_pair_without_treating_it_as_zero(self):
        batch = self.batch()
        def execute(context):
            context.metered_model('offline-private-value').invoke_controlled([], object())
        with patch.object(ManagedTongyi, 'invoke_controlled', return_value=AIMessage(content='unknown')) as provider:
            report = batch.run(execute)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(report['stop_reason'], 'batch_usage_unknown')
        self.assertEqual(report['budget']['charged_or_reserved_tokens'], 200)
        self.assertFalse(report['budget']['usage_complete'])
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_cancellation_before_start_saves_every_row(self):
        batch = self.batch(cancelled=lambda: True)
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            report = batch.run(lambda c: self.fail('executor should not start'))
        self.assertEqual(report['stop_reason'], 'cancel_requested')
        self.assertTrue(all(r['state'] == 'not_run' for r in report['rows']))
        provider.assert_not_called()

    def test_missing_cleanup_cannot_advance_to_the_next_variant(self):
        report = self.batch().run(lambda c: {'outcome': 'passed', 'cleanup_ok': False})
        self.assertEqual(report['stop_reason'], 'container_cleanup_unconfirmed')
        self.assertEqual([r['state'] for r in report['rows']], ['stopped', 'not_run'])

    def test_local_deadline_can_advance_only_after_a_cleanup_receipt(self):
        now = [0]
        batch = self.batch(clock=lambda: now[0])
        def execute(context):
            now[0] += 61
            return {'outcome': 'failed', 'cleanup_ok': True}
        report = batch.run(execute)
        self.assertIsNone(report['stop_reason'])
        self.assertTrue(all(r['outcome'] == 'run_deadline' for r in report['rows']))
        self.assertTrue(all(r['state'] == 'stopped' for r in report['rows']))

    def test_executor_exception_without_cleanup_stops_even_for_local_budget(self):
        def execute(context):
            raise BudgetExceeded('run_token_limit')
        report = self.batch().run(execute)
        self.assertEqual(report['stop_reason'], 'run_token_limit')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_final_scoring_has_separate_clock_and_cannot_restart_inference(self):
        self.config['limits']['final_evaluation_seconds'] = 120
        self.manifest = self.plan()
        now = [0]
        batch = self.batch(clock=lambda: now[0])
        def execute(context):
            now[0] += 50
            context.begin_evaluation('frozen-candidate')
            now[0] += 70
            self.assertEqual(context.remaining_seconds(), 50)
            with self.assertRaisesRegex(BudgetExceeded, 'closed_model'):
                context.metered_model('offline-private-value').invoke_controlled([], object())
            with self.assertRaisesRegex(RuntimeError, 'already_started'):
                context.begin_evaluation('another')
            return {'outcome': 'failed', 'cleanup_ok': True}
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            report = batch.run(execute)
        provider.assert_not_called()
        self.assertIsNone(report['stop_reason'])
        self.assertTrue(all(r['state'] == 'completed' for r in report['rows']))
        self.assertEqual(batch.ledger.state['calls'], [])


if __name__ == '__main__':
    unittest.main()
