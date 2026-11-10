"""Synthetic local wiring tests; no real LLM, Docker score or SWE-bench result."""
import copy
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from repair_workbench import swebench_adapter as adapter
from repair_workbench import swebench_live_evaluation as live
from repair_workbench.experiment_manifest import create_manifest
from repair_workbench.model_transport import ManagedTongyi
from repair_workbench.evaluation_budget import _atomic_write
from repair_workbench.evaluation_batch import FrozenEvaluationBatch


spec = importlib.util.spec_from_file_location('live_swe_fixture',
    Path(__file__).resolve().parents[1] / 'scripts/qualify_swebench_fixture.py')
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
IMAGE = 'sha256:' + 'a' * 64
POLICY = {'network': 'none', 'cpus': 2, 'memory': '2g', 'capabilities': 'dropped', 'auto_pull': False}


class SWEBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = self.root / 'fixture'
        self.row = fixture_module.prepare_fixture(self.fixture)
        self.workspace = self.fixture / 'agent-workspace'
        self.dataset = self.fixture / 'evaluator-dataset'
        self.qualification = self.root / 'qualification'
        self.write_qualification()
        self.config = {'model': {'id': 'offline-model', 'region': 'cn-beijing',
            'input_token_limit': 1000, 'output_token_limit': 100},
            'tools': ['read_source', 'list_sources', 'search_source', 'source_outline',
                      'read_symbol', 'submit_ref_edits', 'read_file', 'write_todos'],
            'limits': {'run_tokens': 10000, 'batch_tokens': 40000, 'requests_per_run': 4,
                       'batch_requests': 16, 'run_seconds': 60, 'batch_seconds': 600,
                       'final_evaluation_seconds': 60, 'max_attempts': 3, 'max_tool_calls': 60},
            'swebench': {'harness_python': '/controlled/harness/python', 'network_policy': 'none',
                        'evaluation_timeout_seconds': 30, 'model_label': 'synthetic-offline'}}
        self.config['implementation'] = live.source_fingerprint()
        self.tasks = [self.task(self.row['instance_id'])]
        self.requests = []
        self.scores = []

    def task(self, identifier):
        return {'id': identifier, 'cluster_id': identifier, 'split': 'dev',
            'public': {key: self.row[key] for key in ('repo', 'base_commit', 'problem_statement')},
            'evaluator': {'dataset': str(self.dataset), 'workspace': str(self.workspace),
                          'qualification': str(self.qualification)}}

    def write_qualification(self, qualified=True):
        self.qualification.mkdir(exist_ok=True)
        controls = {}
        for role, status in (('negative', 'unresolved'), ('gold', 'resolved')):
            directory = self.qualification / role
            directory.mkdir(exist_ok=True)
            (directory / 'source-binding.json').write_text(json.dumps({
                'upstream_commit': self.row['base_commit'], 'content_match': True, 'dirty_returncode': 0}))
            controls[role] = {'status': status, 'instance_id': self.row['instance_id'],
                             'cleanup_ok': True, 'image_id': IMAGE, 'execution_policy': POLICY}
        (self.qualification / 'qualification.json').write_text(json.dumps({'qualified': qualified, **controls}))

    def plan(self, *, skip_binding=None, seed=4):
        paths = sorted({str(path) for task in self.tasks for path in live.required_binding_paths(task)})
        files = {f'file-{index}': path for index, path in enumerate(paths) if path != skip_binding}
        return create_manifest(self.config, self.tasks,
            [{'id': 'baseline', 'policy': 'on_demand'}, {'id': 'candidate', 'policy': 'issue_evidence'}],
            files, seed=seed)

    def provider(self, messages, *args, **kwargs):
        self.requests.append(messages)
        observation = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
        if observation is None:
            tool = {'name': 'search_source', 'args': {'query': 'return a - b'}, 'id': 'search'}
        else:
            match = json.loads(observation.content)['matches'][0]
            tool = {'name': 'submit_ref_edits', 'args': {'edits': [{
                'source_ref': match['source_ref'], 'old_text': 'return a - b', 'new_text': 'return a + b'}],
                'diagnosis': 'Use addition required by the public behavior.'}, 'id': 'submit'}
        return AIMessage(content='', tool_calls=[tool], usage_metadata={
            'input_tokens': 50, 'output_tokens': 10, 'total_tokens': 60})

    def score(self, row, prediction, output, harness_python, **kwargs):
        self.scores.append((row, prediction, kwargs))
        self.assertIn('+    return a + b', prediction['model_patch'])
        self.assertEqual(kwargs['workspace'], self.workspace)
        self.assertEqual(kwargs['network_policy'], 'none')
        output.mkdir()
        result = {'status': 'resolved', 'reason': 'stub_official_score',
                  'cleanup_ok': True, 'image_id': IMAGE, 'execution_policy': POLICY}
        _atomic_write(output / 'result.json', result, create=True)
        return result

    def run_plan(self, plan=None, *, provider=None, score=None, image=IMAGE, current_config=None):
        plan = plan or self.plan()
        with ExitStack() as stack:
            stack.enter_context(patch.object(live, '_harness_preflight'))
            stack.enter_context(patch.object(adapter, 'image_identity', return_value=image))
            self.provider_mock = stack.enter_context(patch.object(ManagedTongyi, 'invoke_controlled',
                                                                  side_effect=provider or self.provider))
            self.score_mock = stack.enter_context(patch.object(adapter, 'evaluate', side_effect=score or self.score))
            return live.run_swebench_batch(plan, self.root / 'batch',
                expected_sha256=plan['manifest_sha256'], current_config=current_config or (lambda: copy.deepcopy(self.config)),
                api_key='offline-test-only-key')

    def test_source_and_score_polling_use_liveness_without_rehashing_every_tick(self):
        reads = []
        def current():
            reads.append(1)
            return copy.deepcopy(self.config)
        def checked_score(*args, **kwargs):
            before = len(reads)
            for _ in range(100):
                kwargs['execution_control'].check()
            self.assertEqual(len(reads), before)
            return self.score(*args, **kwargs)
        original = adapter.run_qualified_agent
        def checked_agent(*args, **kwargs):
            before = len(reads)
            for _ in range(100):
                self.assertFalse(kwargs['cancellation_check']())
            self.assertEqual(len(reads), before)
            return original(*args, **kwargs)
        with patch.object(adapter, 'run_qualified_agent', side_effect=checked_agent):
            report = self.run_plan(score=checked_score, current_config=current)
        self.assertEqual([row['outcome'] for row in report['rows']], ['passed', 'passed'])

    def test_drift_after_agent_without_submission_stops_before_next_variant(self):
        def changed_agent(*args, **kwargs):
            self.qualification.joinpath('qualification.json').write_text('{}')
            return {'status': 'inconclusive', 'stop_reason': 'offline-no-submission'}
        with patch.object(adapter, 'run_qualified_agent', side_effect=changed_agent):
            report = self.run_plan()
        self.provider_mock.assert_not_called()
        self.assertEqual(report['stop_reason'], 'ValueError')
        self.assertEqual([row['state'] for row in report['rows']], ['error', 'not_run'])

    def test_local_agent_deadline_keeps_cleanup_receipt_and_next_variant(self):
        plan = self.plan()
        now = [0]
        batch = FrozenEvaluationBatch(plan, self.root / 'batch',
            expected_sha256=plan['manifest_sha256'], current_config=lambda: copy.deepcopy(self.config),
            clock=lambda: now[0])
        def expired_agent(*args, **kwargs):
            now[0] += self.config['limits']['run_seconds'] + 1
            return {'status': 'inconclusive', 'stop_reason': 'run_deadline'}
        with patch.object(live, '_harness_preflight'), \
             patch.object(adapter, 'image_identity', return_value=IMAGE), \
             patch.object(adapter, 'run_qualified_agent', side_effect=expired_agent), \
             patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            report = batch.run(live.SWEbenchExecutor(batch, 'offline-test-only-key'))
        provider.assert_not_called()
        self.assertIsNone(report['stop_reason'])
        self.assertEqual([row['state'] for row in report['rows']], ['stopped', 'stopped'])
        self.assertTrue(all(row['outcome'] == 'run_deadline' for row in report['rows']))

    def test_post_score_drift_keeps_cleanup_but_cannot_record_a_pass(self):
        def changed_score(*args, **kwargs):
            result = self.score(*args, **kwargs)
            self.qualification.joinpath('qualification.json').write_text('{}')
            return result
        report = self.run_plan(score=changed_score)
        self.assertEqual(report['stop_reason'], 'ValueError')
        self.assertEqual([row['state'] for row in report['rows']], ['error', 'not_run'])
        run = self.root / 'batch' / report['rows'][0]['run_id']
        receipt = json.loads((run / 'swe-executor-receipt.json').read_text())
        self.assertTrue(receipt['cleanup_ok'])
        self.assertTrue((run / 'official-evaluation/result.json').is_file())

    def test_export_crossing_run_deadline_cannot_refresh_as_final_scoring(self):
        plan = self.plan()
        now = [0]
        original = adapter.export_prediction
        def export(*args, **kwargs):
            prediction = original(*args, **kwargs)
            now[0] += self.config['limits']['run_seconds'] + 1
            return prediction
        with patch.object(live, 'FrozenEvaluationBatch', side_effect=lambda *a, **k:
                FrozenEvaluationBatch(*a, **k, clock=lambda: now[0])), \
             patch.object(adapter, 'export_prediction', side_effect=export):
            report = self.run_plan(plan)
        self.score_mock.assert_not_called()
        self.assertEqual(report['stop_reason'], 'run_deadline')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_real_source_refs_export_and_independent_verdict_use_one_shared_ledger(self):
        report = self.run_plan()
        self.assertEqual([row['outcome'] for row in report['rows']], ['passed', 'passed'], report)
        self.assertEqual(self.provider_mock.call_count, 4)
        self.assertEqual(self.score_mock.call_count, 2)
        self.assertEqual(report['budget']['charged_or_reserved_tokens'], 240)
        for row in report['rows']:
            run = self.root / 'batch' / row['run_id']
            agent = json.loads((run / 'agent/result.json').read_text())
            self.assertEqual(agent['status'], 'submitted')
            self.assertEqual(agent['evaluation_status'], 'not_run')
            self.assertTrue((run / 'predictions.jsonl').is_file())
            self.assertTrue((run / 'official-evaluation/result.json').is_file())
            self.assertTrue(json.loads((run / 'swe-executor-receipt.json').read_text())['cleanup_ok'])
        for messages in self.requests:
            initial = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
            self.assertNotIn('evaluator', initial)
            self.assertNotIn('gold_patch', json.dumps(initial))
            self.assertNotIn('PASS_TO_PASS', json.dumps(initial))

    def test_false_qualification_stops_before_any_model_and_preserves_rows(self):
        self.write_qualification(qualified=False)
        report = self.run_plan()
        self.provider_mock.assert_not_called()
        self.score_mock.assert_not_called()
        self.assertEqual([row['state'] for row in report['rows']], ['error', 'not_run'])
        self.assertEqual(report['budget']['calls'], 0)

    def test_all_tasks_are_preflighted_even_when_invalid_task_would_run_later(self):
        other = self.task('workbench__fixture-2')
        self.tasks.append(other)  # Same workspace describes fixture-1: binding must reject fixture-2.
        directory = self.dataset / other['id']
        directory.mkdir()
        # Bind local placeholders; the workspace/qualification mismatch must
        # reject this task before its dataset record can be used.
        for name in ('public.json', 'evaluator.json'):
            (directory / name).write_text('{}')
        plan = self.plan(seed=0)
        self.assertEqual(plan['schedule'][0]['task_id'], self.row['instance_id'])
        report = self.run_plan(plan)
        self.provider_mock.assert_not_called()
        self.assertEqual(len(report['rows']), 4)
        self.assertEqual(sum(row['state'] == 'not_run' for row in report['rows']), 3)

    def test_unbound_receipt_and_changed_image_stop_before_inference(self):
        plan = self.plan(skip_binding=str(self.qualification / 'qualification.json'))
        report = self.run_plan(plan)
        self.provider_mock.assert_not_called()
        self.assertEqual(report['stop_reason'], 'ValueError')

    def test_image_drift_stops_before_inference(self):
        report = self.run_plan(image='sha256:' + 'b' * 64)
        self.provider_mock.assert_not_called()
        self.assertEqual(report['stop_reason'], 'ValueError')

    def test_official_unresolved_is_failure_not_submitted_success(self):
        def score(*args, **kwargs):
            return {'status': 'unresolved', 'cleanup_ok': True, 'reason': 'stub_rejection',
                    'image_id': IMAGE, 'execution_policy': POLICY}
        report = self.run_plan(score=score)
        self.assertEqual([row['outcome'] for row in report['rows']], ['failed', 'failed'])
        self.assertEqual(self.score_mock.call_count, 2)

    def test_no_patch_does_not_call_official_evaluator(self):
        def provider(*args, **kwargs):
            return AIMessage(content='Insufficient evidence.', usage_metadata={
                'input_tokens': 50, 'output_tokens': 10, 'total_tokens': 60})
        report = self.run_plan(provider=provider)
        self.assertEqual([row['outcome'] for row in report['rows']], ['failed', 'failed'])
        self.score_mock.assert_not_called()
        self.assertTrue(all(row['result']['reason'] == 'no_final_submission' for row in report['rows']))

    def test_unknown_usage_blocks_scoring_and_next_variant(self):
        report = self.run_plan(provider=lambda *a, **k: AIMessage(content='missing usage'))
        self.assertEqual(self.provider_mock.call_count, 1)
        self.score_mock.assert_not_called()
        self.assertEqual(report['rows'][1]['state'], 'not_run')
        self.assertFalse(report['budget']['usage_complete'])
        self.assertEqual(report['budget']['charged_or_reserved_tokens'], 1100)

    def test_unconfirmed_official_cleanup_stops_the_batch(self):
        report = self.run_plan(score=lambda *a, **k: {'status': 'resolved', 'cleanup_ok': False})
        self.assertEqual(self.score_mock.call_count, 1)
        self.assertEqual(report['stop_reason'], 'container_cleanup_unconfirmed')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_runtime_cannot_claim_an_unsupported_tool_limit(self):
        self.config['limits']['max_tool_calls'] = 120
        report = self.run_plan()
        self.provider_mock.assert_not_called()
        self.assertEqual(report['stop_reason'], 'ValueError')

    def test_environment_change_during_agent_run_cannot_be_scored_as_success(self):
        def score(*args, **kwargs):
            return {'status': 'resolved', 'cleanup_ok': True,
                    'image_id': 'sha256:' + 'b' * 64, 'execution_policy': POLICY}
        report = self.run_plan(score=score)
        self.assertEqual(report['stop_reason'], 'ValueError')
        self.assertEqual(report['rows'][0]['state'], 'error')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_submitted_label_without_verified_artifacts_cannot_reach_scoring(self):
        with patch.object(adapter, 'run_qualified_agent', return_value={'status': 'submitted'}):
            report = self.run_plan()
        self.score_mock.assert_not_called()
        self.assertEqual(report['rows'][0]['state'], 'error')
        self.assertEqual(report['rows'][1]['state'], 'not_run')

    def test_missing_official_report_is_inconclusive_even_after_valid_submission(self):
        report = self.run_plan(score=lambda *a, **k: {
            'status': 'inconclusive', 'cleanup_ok': True, 'reason': 'official_report_missing'})
        self.assertEqual([row['outcome'] for row in report['rows']], ['inconclusive', 'inconclusive'])
        self.assertEqual(self.score_mock.call_count, 2)


if __name__ == '__main__':
    unittest.main()
