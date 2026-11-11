"""Frozen F0/A1 wiring and qualification; all provider receipts are synthetic."""
import copy
import json
from unittest.mock import patch
import unittest

from langchain_core.messages import AIMessage, HumanMessage
import test_repository_preparation as helpers
from repair_workbench import repository_live_evaluation as live
from repair_workbench.experiment_manifest import create_manifest
from repair_workbench.model_transport import ManagedTongyi
from repair_workbench.session import BudgetExceeded
from repair_workbench.snapshot import snapshot_id, verify_snapshot


class RepositoryBatchTests(unittest.TestCase):
    def setUp(self):
        self.helper = helpers.RepositoryPreparationTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.root, self.base, self.spec = self.helper.root, self.helper.base, self.helper.spec
        self.data = self.spec.as_dict()
        self.image = self.data['measurement']['image_id']
        self.base_ref = snapshot_id(verify_snapshot(self.base))
        self.contract = self.root / 'contract.json'
        self.spec.write_frozen(self.contract)
        self.execution = self.root / 'control/positive/execution.json'
        self.positive = self.execution.parent / 'independent-evaluation/result.json'
        self.positive.parent.mkdir(parents=True)
        self.implementation = self.root / 'control/implementation.json'
        self.negative = self.root / 'negative.json'
        self.qualification = self.root / 'qualification.json'
        self.config = {'implementation': live.source_fingerprint(),
            'model': {'id': 'offline-model', 'region': 'cn-beijing', 'input_token_limit': 1000, 'output_token_limit': 100},
            'tools': sorted(live._TOOLS), 'repository_diagnostic': {'enable_reproduction': False, 'test_pids_limit': 64},
            'limits': {'batch_tokens': 20000, 'run_tokens': 5000, 'requests_per_run': 4, 'batch_requests': 8,
                'run_seconds': 60, 'batch_seconds': 600, 'max_tool_calls': 60, 'max_probe_runs': 6,
                'final_evaluation_seconds': 120}}
        self.implementation.write_text(json.dumps(self.config['implementation']))
        self.write_controls()
        self.task = {'id': self.data['task_id'], 'cluster_id': 'historical-development', 'split': 'dev',
            'public': {'repo': self.data['repo'], 'base_commit': self.data['base_commit'],
                       'problem_statement': 'A public slow workload.'},
            'evaluator': {'baseline': str(self.base), 'contract': str(self.contract),
                          'contract_sha256': self.spec.sha256, 'qualification': str(self.qualification)}}
        self.strategies = []

    def write_controls(self, *, positive_status='passed', negative_status='failed'):
        environment = {'status': 'tests_passed', 'cleanup_ok': True, 'image_id': self.image,
            'test_execution_policy': {'pids_limit': 64, 'memory_bytes': 268435456,
                                      'cpus': 1, 'network': 'none', 'auto_pull': False},
            'test_inventory': {'outcomes': {key: 'passed' for key in self.data['original_tests']['nodeids']}}}
        planned = 8 * len(self.data['final_inputs'])
        schedule = [{'case_id': case['case_id'], 'probe': probe, 'position': position, 'role': role}
                    for case in self.data['final_inputs'] for probe in ('benchmark', 'memory')
                    for position, role in enumerate(self.data['measurement']['order'])]
        identity = {'contract_sha256': self.spec.sha256, 'baseline_ref': self.base_ref,
                    'test_pids_limit': 64, 'planned_measurement_runs': planned}
        self.execution.write_text(json.dumps({'status': positive_status, 'agent_status': 'submitted',
            'agent_finished_before_final_evaluation': True, 'independent_evaluation_started': True,
            'cleanup_ok': True, 'independent_verdict': 'independent-evaluation/result.json'}))
        self.positive.write_text(json.dumps({**identity, 'status': positive_status,
            'tests': {'baseline': environment, 'candidate': environment},
            'schedule': [{**row, 'state': 'completed'} for row in schedule],
            'runs': [{**row, 'result': {'status': 'ok', 'image_id': self.image, 'cleanup_ok': True,
                                        'input_integrity_ok': True}} for row in schedule]}))
        self.negative.write_text(json.dumps({**identity, 'status': negative_status,
            'reason': 'candidate_original_tests_rejected', 'runs': [],
            'tests': {'baseline': environment, 'candidate': {**environment, 'status': 'tests_failed',
                'test_inventory': {'outcomes': {key: 'failed' for key in self.data['original_tests']['nodeids']}}}},
            'schedule': [{**row, 'state': 'not_run'} for row in schedule]}))
        self.qualification.write_text(json.dumps({'schema_version': 1, 'contract_sha256': self.spec.sha256,
            'baseline_ref': self.base_ref, 'image_id': self.image,
            'implementation_sha256': live._digest(self.config['implementation']),
            'developer_exposed': True,
            'positive_execution': str(self.execution), 'positive_result': str(self.positive),
            'positive_implementation': str(self.implementation), 'negative_result': str(self.negative)}))

    def plan(self, omit=None):
        return create_manifest(self.config, [self.task],
            [{'id': 'F0', 'policy': 'fixed'}, {'id': 'A1', 'policy': 'adaptive'}],
            {f'artifact-{i}': p for i, p in enumerate(live.required_binding_paths(self.task)) if p != omit}, seed=11)

    def provider(self, messages, *args, **kwargs):
        self.assertEqual(messages[0].content, 'A public slow workload.')
        self.assertNotIn('FINAL_CASE_SENTINEL', str(messages))
        return AIMessage(content='synthetic response', usage_metadata={
            'input_tokens': 50, 'output_tokens': 10, 'total_tokens': 60})

    def orchestrate(self, spec, baseline, output, allowed, model, **kwargs):
        self.assertEqual(spec.sha256, self.spec.sha256)
        self.assertEqual(allowed, {'pricing.py'})
        self.strategies.append(kwargs['strategy'])
        model.invoke_controlled([HumanMessage(content=kwargs['problem_statement'])], object())
        kwargs['on_final_submission']('f' * 64)
        with self.assertRaisesRegex(BudgetExceeded, 'final_submission_closed_model_calls'):
            model.invoke_controlled([], object())
        return {'status': 'passed', 'agent_status': 'submitted', 'reason': 'synthetic independent receipt',
                'cleanup_ok': True}

    def run_batch(self, plan=None, *, execute=None):
        plan = plan or self.plan()
        with patch.object(live, 'resolve_image', return_value=self.image), \
             patch.object(ManagedTongyi, 'invoke_controlled', side_effect=self.provider) as provider, \
             patch.object(live, 'run_repository_diagnostic_task', side_effect=execute or self.orchestrate) as runner:
            result = live.run_repository_batch(plan, self.root / 'batch', expected_sha256=plan['manifest_sha256'],
                current_config=lambda: copy.deepcopy(self.config), api_key='offline-only')
        return result, provider, runner

    def test_complete_pair_shares_meter_and_cannot_request_after_sealing(self):
        result, provider, runner = self.run_batch()
        self.assertEqual([row['outcome'] for row in result['rows']], ['passed', 'passed'])
        self.assertEqual(set(self.strategies), {'fixed', 'adaptive'})
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(result['budget']['charged_or_reserved_tokens'], 120)

    def test_noisy_positive_stops_before_provider_or_agent(self):
        self.write_controls(positive_status='inconclusive')
        result, provider, runner = self.run_batch()
        provider.assert_not_called(); runner.assert_not_called()
        self.assertEqual(result['stop_reason'], 'ValueError')
        self.assertEqual([row['state'] for row in result['rows']], ['error', 'not_run'])

    def test_negative_control_cannot_be_omitted_or_treated_as_passed(self):
        self.write_controls(negative_status='passed')
        result, provider, runner = self.run_batch()
        provider.assert_not_called(); runner.assert_not_called()
        self.assertEqual(result['rows'][1]['state'], 'not_run')

    def test_raw_qualification_artifacts_must_be_bound(self):
        result, provider, runner = self.run_batch(self.plan(omit=self.negative))
        provider.assert_not_called(); runner.assert_not_called()
        self.assertEqual(result['stop_reason'], 'ValueError')

    def test_positive_control_cannot_borrow_new_source_fingerprint(self):
        self.implementation.write_text('{}')
        result, provider, runner = self.run_batch()
        provider.assert_not_called(); runner.assert_not_called()
        self.assertEqual(result['stop_reason'], 'ValueError')

    def test_developer_exposed_control_cannot_enter_final_split(self):
        self.task['split'] = 'test'
        result, provider, runner = self.run_batch()
        provider.assert_not_called(); runner.assert_not_called()
        self.assertEqual(result['stop_reason'], 'ValueError')

    def test_missing_cleanup_receipt_never_advances_next_variant(self):
        def uncertain(*args, **kwargs):
            return {'status': 'inconclusive', 'reason': 'cleanup unknown', 'cleanup_ok': False}
        result, provider, runner = self.run_batch(execute=uncertain)
        provider.assert_not_called()
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result['stop_reason'], 'container_cleanup_unconfirmed')
        self.assertEqual(result['rows'][1]['state'], 'not_run')

    def test_frozen_tool_names_must_match_explicit_reproduction_flag(self):
        self.config['repository_diagnostic']['enable_reproduction'] = True
        result, provider, runner = self.run_batch()
        provider.assert_not_called(); runner.assert_not_called()
        self.assertEqual(result['stop_reason'], 'ValueError')

    def test_preview_switch_and_tools_are_frozen_for_both_variants(self):
        self.config['repository_diagnostic']['enable_candidate_preview'] = True
        self.config['tools'] += sorted(live._PREVIEW_TOOLS)
        def enabled(*args, **kwargs):
            self.assertIs(kwargs['enable_candidate_preview'], True)
            return self.orchestrate(*args, **kwargs)
        result, provider, _ = self.run_batch(execute=enabled)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual([row['state'] for row in result['rows']], ['completed', 'completed'])


if __name__ == '__main__':
    unittest.main()
