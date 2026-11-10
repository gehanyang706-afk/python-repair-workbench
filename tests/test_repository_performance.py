"""Independent final acceptance boundaries, with explicit offline runner receipts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_agent import fixture
from test_workload_contract import case, contract
from repair_workbench.candidate import create_candidate
from repair_workbench import repository_performance as final
from repair_workbench.snapshot import snapshot_id, verify_snapshot
from repair_workbench.workload_contract import WorkloadSpec
from repair_workbench.session import BudgetExceeded


class RepositoryPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = fixture(self.root)
        source = (self.base / 'workspace/pricing.py').read_text()
        self.candidate = self.root / 'submitted'
        create_candidate(self.base, self.candidate, {'pricing.py': source + '\n# proposed source change\n'}, {'pricing.py'})
        (self.candidate / 'reports').mkdir()
        (self.candidate / 'reports/claimed-passed.json').write_text('{"passed":true}')
        self.destination = self.root / 'final'
        self.jobs, self.test_calls = [], []

    def spec(self, change=None):
        data = contract()
        adapters, checkers = final.registered_implementations()
        data.update(base_commit=verify_snapshot(self.base)['commit'], workload_id='sql-comments-v3',
                    adapter_sha256=adapters['sql-comments-v3'])
        data['measurement'].update(warmups=1, repeats=3)
        data['semantics'].update(output_comparison='canonical_json', checker_id='canonical-output-v1',
            checker_sha256=checkers['canonical-output-v1'], metamorphic_checks=[])
        data['original_tests'] = {'suite_sha256': final.original_suite_sha256(verify_snapshot(self.base)),
            'nodeids': ['tests/test_pricing.py::test_boundary'], 'expected_xfail': []}
        data['development_inputs'] = [case('dev', {'size': 1, 'kind': 'line_comments'})]
        data['final_inputs'] = [case('final', {'size': 2, 'kind': 'line_comments'}),
                                case('guard', {'size': 3, 'kind': 'quoted_markers'}, 'guardrail')]
        if change:
            change(data)
        return WorkloadSpec(data, adapters=adapters, checkers=checkers)

    def run_suite(self, snapshot, image, timeout, **kwargs):
        self.test_calls.append((snapshot, kwargs))
        self.assertTrue(kwargs['collect_nodeids'])
        self.assertFalse((snapshot / 'reports/claimed-passed.json').exists())
        manifest = verify_snapshot(snapshot)
        return {'status': 'tests_passed', 'cleanup_ok': True, 'image_id': image,
                'snapshot_id': snapshot_id(manifest),
                'test_inventory': {'outcomes': {'tests/test_pricing.py::test_boundary': 'passed'}}}

    def measurement(self, snapshot, destination, image, job, **kwargs):
        self.jobs.append((snapshot.name, copy.deepcopy(job)))
        baseline = snapshot.name == 'baseline'
        value = 1.0 if baseline else .5
        if job['probe'] == 'memory': value = 100
        report = {'schema_version': 1, 'workload_id': job['workload_id'], 'probe': job['probe'],
                  'input_sha256': job['input_sha256'], 'samples': [value] * job['repeats'],
                  'output_sha256': ['1' * 64] * job['repeats'], 'output_lengths': [0] * job['repeats'],
                  'environment': {'python': '3.13', 'implementation': 'CPython', 'system': 'Linux',
                                  'machine': 'aarch64', 'clock': 'perf_counter_ns'}}
        return {'status': 'ok', 'cleanup_ok': True, 'input_integrity_ok': True,
                'snapshot_ref': snapshot_id(verify_snapshot(snapshot)), 'image_id': image,
                'adapter_sha256': final.registered_implementations()[0][job['workload_id']],
                'measurement': report}

    def run_final(self, *, tests=None, measure=None, spec=None, control=None):
        with patch.object(final, 'run_tests', side_effect=tests or self.run_suite), \
             patch.object(final, 'run_repository_measurement', side_effect=measure or self.measurement):
            return final.verify_repository_candidate(self.base, self.candidate, self.destination,
                {'pricing.py'}, spec or self.spec(), execution_control=control)

    def test_rebuild_from_base_drops_claims_and_executes_exact_ABBA_final_inputs(self):
        result = self.run_final()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['candidate_ref'], snapshot_id(verify_snapshot(self.candidate)))
        self.assertEqual(len(self.test_calls), 2)
        self.assertEqual(len(self.jobs), 16)
        self.assertTrue(all(row['state'] == 'completed' for row in result['schedule']))
        for start in range(0, 16, 4):
            self.assertEqual([r for r, j in self.jobs[start:start+4]], ['baseline', 'candidate', 'candidate', 'baseline'])
        self.assertEqual({j['input']['size'] for r, j in self.jobs}, {2, 3})
        self.assertNotIn(1, [j['input']['size'] for r, j in self.jobs])
        self.assertTrue((self.candidate / 'reports/claimed-passed.json').exists())
        self.assertFalse((self.destination / 'candidate/reports/claimed-passed.json').exists())
        self.assertEqual(json.loads((self.destination / 'result.json').read_text())['status'], 'passed')

    def test_test_execution_error_with_complete_outcomes_is_not_acceptance(self):
        def bad(*args, **kwargs):
            return {**self.run_suite(*args, **kwargs), 'status': 'execution_error'}
        result = self.run_final(tests=bad)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(len(self.jobs), 0)
        self.assertEqual(len(self.test_calls), 1)

    def test_missing_candidate_test_or_unknown_cleanup_stops_before_measurement(self):
        def missing(snapshot, *args, **kwargs):
            result = self.run_suite(snapshot, *args, **kwargs)
            if snapshot.name == 'candidate': result['test_inventory']['outcomes'] = {}
            return result
        result = self.run_final(tests=missing)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'candidate_original_tests_rejected')
        self.assertFalse(self.jobs)

    def test_cleaned_measurement_with_missing_input_integrity_cannot_be_used(self):
        def uncertain(*args, **kwargs):
            return {**self.measurement(*args, **kwargs), 'input_integrity_ok': False}
        result = self.run_final(measure=uncertain)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(result['measurements'], [])
        self.assertEqual(sum(row['state'] == 'not_run' for row in result['schedule']), 15)

    def test_unknown_container_cleanup_cannot_start_the_next_test_or_probe(self):
        def uncertain(*args, **kwargs):
            return {**self.run_suite(*args, **kwargs), 'cleanup_ok': False}
        result = self.run_final(tests=uncertain)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(len(self.test_calls), 1)
        self.assertFalse(self.jobs)

    def test_contract_file_drift_during_independent_tests_invalidates_stage(self):
        def changed(*args, **kwargs):
            result = self.run_suite(*args, **kwargs)
            (self.destination / 'contract.json').write_text('{}')
            return result
        result = self.run_final(tests=changed)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertFalse(self.jobs)

    def test_drift_during_scoring_cannot_publish_a_pass(self):
        original = final.score_final_measurements
        def changed(*args, **kwargs):
            verdict = original(*args, **kwargs)
            manifest = self.destination / 'candidate/manifest.json'
            manifest.write_text(manifest.read_text() + ' ')
            return verdict
        with patch.object(final, 'score_final_measurements', side_effect=changed):
            result = self.run_final()
        self.assertEqual(result['status'], 'inconclusive')
        self.assertNotIn('verdict', result)

    def test_changed_single_candidate_output_sample_is_a_semantic_failure(self):
        def changed(snapshot, *args, **kwargs):
            result = self.measurement(snapshot, *args, **kwargs)
            if snapshot.name == 'candidate': result['measurement']['output_sha256'][1] = '2' * 64
            return result
        result = self.run_final(measure=changed)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'candidate_output_not_deterministic')
        self.assertEqual(len(self.jobs), 2)

    def test_clean_source_drift_after_test_invalidates_final_stage(self):
        def changed(snapshot, *args, **kwargs):
            result = self.run_suite(snapshot, *args, **kwargs)
            (snapshot / 'workspace/pricing.py').write_text('# drift\n')
            return result
        result = self.run_final(tests=changed)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['reason'], 'final_evaluation_error')
        self.assertFalse(self.jobs)

    def test_unimplemented_resource_protocol_is_rejected_before_any_output(self):
        spec = self.spec(lambda d: d['measurement'].update(memory_metric='process_peak_rss_bytes'))
        with self.assertRaisesRegex(ValueError, 'not_supported'):
            self.run_final(spec=spec)
        self.assertFalse(self.destination.exists())

    def test_wrong_suite_hash_is_rejected_before_tests_or_output(self):
        spec = self.spec(lambda d: d['original_tests'].update(suite_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'suite_mismatch'):
            self.run_final(spec=spec)
        self.assertFalse(self.destination.exists())

    def test_source_reconstruction_preserves_crlf_bytes_and_submitted_identity(self):
        other = self.root / 'submitted-crlf'
        source = (self.base / 'workspace/pricing.py').read_bytes().decode().replace('\n', '\r\n')
        create_candidate(self.base, other, {'pricing.py': source}, {'pricing.py'})
        self.candidate = other
        result = self.run_final()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual((self.destination / 'candidate/workspace/pricing.py').read_bytes(), source.encode())

    def test_root_cancel_is_saved_and_propagates_without_more_physical_runs(self):
        class Control:
            def check(self): raise BudgetExceeded('cancel_requested')
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            self.run_final(control=Control())
        result = json.loads((self.destination / 'result.json').read_text())
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(self.jobs), 0)


if __name__ == '__main__':
    unittest.main()
