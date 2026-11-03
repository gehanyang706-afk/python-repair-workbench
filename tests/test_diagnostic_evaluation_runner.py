"""Batch lifecycle, isolation and failure denominators; no real model requests."""
import json
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repair_workbench import diagnostic_evaluation as evaluation
from repair_workbench.snapshot import snapshot_id, verify_snapshot
from repair_workbench import probe_worker
from test_diagnostics import correct_export

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'benchmarks/ugc-diagnostics-dev-v1'
IMAGE = 'sha256:' + 'a' * 64


class DiagnosticEvaluationRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def plan(self, **kwargs):
        with patch.object(evaluation, '_image_id', return_value=IMAGE):
            return evaluation.create_plan(DATASET, 'local:fixture', tasks=['case-01', 'case-02'], **kwargs)

    def test_git_environment_cannot_redirect_preparation_into_another_repository(self):
        other = self.root / 'other'
        other.mkdir()
        clean = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        for args in (('init', '-q'), ('add', '--all'), ('commit', '--allow-empty', '-qm', 'original')):
            subprocess.run(['git', '-C', str(other), '-c', 'core.hooksPath=/dev/null',
                            '-c', 'commit.gpgsign=false', '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', *args], env=clean,
                           check=True, capture_output=True, timeout=30)
        def head():
            return subprocess.check_output(['git', '-C', str(other), 'rev-parse', 'HEAD'], env=clean)
        original = head()
        (other / 'keep.txt').write_text('uncommitted user content')
        task = evaluation.load_dataset(DATASET)[0]['tasks'][0]
        destination = self.root / 'prepared'
        with patch.dict(os.environ, {'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other)}):
            with self.assertRaisesRegex(ValueError, 'unsupported_git_environment'):
                evaluation.prepare_task(DATASET, task, destination)
        self.assertEqual(head(), original)
        self.assertEqual((other / 'keep.txt').read_text(), 'uncommitted user content')
        self.assertFalse(destination.exists())

    @staticmethod
    def fake_tests(baseline, image, timeout, **kwargs):
        reports = baseline / 'reports'
        reports.mkdir()
        result = {'snapshot_id': snapshot_id(verify_snapshot(baseline)), 'image_id': IMAGE,
                  'status': 'tests_passed', 'cleanup_ok': True, 'counts': {'passed': 1}}
        (reports / 'result.json').write_text(json.dumps(result))
        (reports / 'pytest.log').write_text('synthetic runner receipt')
        (reports / 'pytest.xml').write_text('<testsuite><testcase name="synthetic"/></testsuite>')
        return result

    @staticmethod
    def fake_verified(plan, row, directory, baseline, task):
        return {'audit': {'audit_status': 'passed'}, 'artifact_sha256': {},
                'baseline_id': snapshot_id(verify_snapshot(baseline)),
                'result_state': 'completed', 'candidate_status': 'inconclusive',
                'metrics': {'model_wrapper_calls': 2, 'reported_usage_calls': 0, 'missing_usage_calls': 2,
                            'all_calls_total_tokens': None, 'tools_admitted': 1, 'tools_proposed': 1,
                            'exploratory_probe_runs': 0, 'reused_observations': 0, 'assessed_tool_rounds': 1,
                            'duplicate_only_rounds': 0, 'candidate_attempts': 0, 'first_candidate_seconds': None,
                            'stop_reason': 'agent_finished', 'diagnostic_stop_reason': None,
                            'decision_proposal': 'insufficient_evidence'}}

    def run_mocked(self, plan, output, **overrides):
        try:
            import repair_workbench.agent
        except ImportError:
            self.skipTest('optional Agent dependencies required for runner imports')
        result = overrides.get('agent_result', {'state': 'completed', 'status': 'inconclusive', 'stop_reason': 'agent_finished'})
        with patch.object(evaluation, '_image_id', return_value=IMAGE), \
             patch.object(evaluation, 'run_tests', side_effect=self.fake_tests), \
             patch('repair_workbench.diagnosis.DiagnosticSession'), \
             patch('repair_workbench.agent.run_agent', return_value=result), \
             patch.object(evaluation, '_verify_run', **overrides.get('verify', {'side_effect': self.fake_verified})):
            return evaluation.run_plan(plan, DATASET, output)

    def test_full_inventory_and_paired_unknown_usage_are_written(self):
        plan = self.plan()
        output = self.root / 'run'
        record = self.run_mocked(plan, output)
        self.assertEqual(record['batch_state'], 'completed', record)
        self.assertEqual(record['summary']['planned_runs'], 4)
        self.assertEqual(record['summary']['comparable_pairs'], 2)
        self.assertIsNone(record['summary']['all_calls_total_tokens'])
        self.assertEqual(record['summary']['missing_usage_calls_for_audited_runs'], 8)
        self.assertEqual(record['summary']['states'], {'completed': 4})
        self.assertFalse(record['summary']['real_llm_performance_claim_allowed'])
        self.assertTrue((output / 'report.md').is_file())
        self.assertEqual(len((output / 'runs.csv').read_text().splitlines()), 5)
        self.assertEqual(json.loads((output / 'plan.json').read_text()), plan)

    def test_first_audit_failure_stops_batch_without_dropping_other_rows(self):
        record = self.run_mocked(self.plan(), self.root / 'invalid',
                                 verify={'side_effect': ValueError('run_probe_identity_mismatch')})
        self.assertEqual(record['batch_state'], 'stopped')
        self.assertEqual(record['stop_reason'], 'run_probe_identity_mismatch')
        self.assertEqual(record['summary']['planned_runs'], 4)
        self.assertEqual(record['summary']['states'], {'invalid': 1, 'not_run': 3})
        self.assertEqual(record['summary']['comparable_pairs'], 0)
        self.assertTrue((self.root / 'invalid/runs' / record['rows'][0]['run_id'] / 'provenance.json').is_file())
        self.assertTrue(all(row['metrics'] == {} for row in record['rows']))

    def test_session_absorbed_user_interrupt_stops_batch_before_next_factory(self):
        try:
            import repair_workbench.diagnostic_eval_policy
        except ImportError:
            self.skipTest('optional Agent dependencies required')
        with patch('repair_workbench.diagnostic_eval_policy.create_scripted_model') as factory:
            record = self.run_mocked(self.plan(), self.root / 'cancelled', agent_result={
                'state': 'stopped', 'status': 'inconclusive', 'stop_reason': 'cancel_requested'})
        factory.assert_called_once()
        self.assertEqual(record['stop_reason'], 'cancel_requested')
        self.assertEqual(record['batch_state'], 'stopped')
        self.assertEqual(record['summary']['states'], {'cancelled': 1, 'not_run': 3})
        self.assertEqual(record['rows'][0]['audit_status'], 'passed')

    def test_post_run_source_drift_invalidates_run_and_preserves_remainder(self):
        original = evaluation.validate_plan
        calls = []
        def validate(plan, dataset):
            calls.append(1)
            if len(calls) >= 3:
                raise ValueError('diagnostic_source_drift')
            return original(plan, dataset)
        plan = self.plan()
        with patch.object(evaluation, 'validate_plan', side_effect=validate):
            record = self.run_mocked(plan, self.root / 'drift')
        self.assertEqual(record['stop_reason'], 'diagnostic_source_drift')
        self.assertEqual(record['summary']['states'], {'invalid': 1, 'not_run': 3})

    def test_batch_deadline_before_first_run_preserves_all_planned_rows(self):
        record = self.run_mocked(self.plan(batch_timeout_seconds=.000001), self.root / 'expired')
        self.assertEqual(record['stop_reason'], 'diagnostic_batch_deadline')
        self.assertEqual(record['summary']['started_runs'], 0)
        self.assertEqual(record['summary']['states'], {'cancelled': 1, 'not_run': 3})
        self.assertIsNone(record['summary']['all_calls_total_tokens'])

    def test_existing_output_and_changed_plan_are_rejected_before_execution(self):
        output = self.root / 'existing'
        output.mkdir()
        with self.assertRaisesRegex(ValueError, 'output_exists'):
            evaluation.run_plan(self.plan(), DATASET, output)
        plan = self.plan()
        plan['mode'] = 'provider'
        with patch.object(evaluation, '_image_id') as image:
            with self.assertRaisesRegex(ValueError, 'plan_hash_mismatch'):
                evaluation.run_plan(plan, DATASET, self.root / 'changed')
            image.assert_not_called()

    def probe_archive(self):
        directory = self.root / 'probe'
        directory.mkdir()
        with patch.object(probe_worker, '_load_export', return_value=correct_export):
            worker = probe_worker.execute('cpu', Path('/workspace'), directory)
        report = {**copy.deepcopy(worker), 'status': 'ok', 'cleanup_ok': True,
                  'image_id': IMAGE, 'snapshot_id': 'b' * 64,
                  'harness_sha256': hashlib.sha256(Path(probe_worker.__file__).read_bytes()).hexdigest()}
        (directory / 'probe.log').write_text('')
        (directory / 'probe.json').write_text(json.dumps(worker))
        (directory / 'result.json').write_text(json.dumps(report))
        return directory, worker, report

    def test_probe_workload_harness_and_semantic_identity_are_rechecked(self):
        directory, worker, report = self.probe_archive()
        plan = self.plan()
        evaluation._check_probe(plan, directory, 'b' * 64)
        for field, value in (('harness_sha256', '0' * 64), ('workload_id', 'foreign-workload'),
                             ('workload_fingerprint', '0' * 64), ('image_id', 'sha256:' + 'e' * 64)):
            modified, raw = copy.deepcopy(report), copy.deepcopy(worker)
            modified[field] = value
            if field in raw:
                raw[field] = value
            (directory / 'result.json').write_text(json.dumps(modified))
            (directory / 'probe.json').write_text(json.dumps(raw))
            with self.subTest(field=field), self.assertRaises(ValueError):
                evaluation._check_probe(plan, directory, 'b' * 64)
        for payload in (report, worker):
            payload['summary']['scales']['large']['fingerprint'] = '0' * 64
        (directory / 'result.json').write_text(json.dumps(report))
        (directory / 'probe.json').write_text(json.dumps(worker))
        with self.assertRaisesRegex(ValueError, 'semantic_fingerprint_mismatch'):
            evaluation._check_probe(plan, directory, 'b' * 64)

    def test_probe_cannot_omit_original_profile_or_log(self):
        directory, _, _ = self.probe_archive()
        plan = self.plan()
        for name in ('probe.log', 'cpu-small.pstats', 'cpu-large.pstats'):
            original = (directory / name).read_bytes()
            (directory / name).unlink()
            with self.subTest(name=name), self.assertRaises(ValueError):
                evaluation._check_probe(plan, directory, 'b' * 64)
            (directory / name).write_bytes(original)


if __name__ == '__main__':
    unittest.main()
