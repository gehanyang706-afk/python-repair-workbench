"""Numerical and provenance contracts using synthetic independent receipts only."""
import copy
import unittest

from repair_workbench.performance_scoring import score_final_measurements
from repair_workbench.workload_contract import WorkloadSpec
from test_workload_contract import contract, case, ADAPTERS, CHECKERS


BASE, CANDIDATE = '1' * 64, '2' * 64


def fixture(goal='speed', time_ratio=.75, memory_ratio=1.0):
    data = contract(goal)
    data['semantics']['metamorphic_checks'] = []
    data['final_inputs'].append(case('final-guard', {'n': 3}, 'guardrail'))
    spec = WorkloadSpec(data, adapters=ADAPTERS, checkers=CHECKERS)
    tests = {'contract_sha256': spec.sha256, 'snapshot_ref': CANDIDATE,
        'image_id': data['measurement']['image_id'], 'suite_sha256': data['original_tests']['suite_sha256'],
        'producer': 'independent_original_test_runner', 'cleanup_ok': True,
        'outcomes': {node: 'xfailed' if node in data['original_tests']['expected_xfail'] else 'passed'
                     for node in data['original_tests']['nodeids']}}
    execution = {k: v for k, v in data['measurement'].items() if k != 'order'}
    execution['adapter_sha256'] = data['adapter_sha256']
    rows = []
    for item in data['final_inputs']:
        for probe in ('benchmark', 'memory'):
            for position, role in enumerate(data['measurement']['order']):
                baseline = 1.0 if probe == 'benchmark' else 1000000
                ratio = (time_ratio if probe == 'benchmark' else memory_ratio) if role == 'candidate' else 1
                value = baseline * ratio
                if probe == 'memory': value = int(value)
                rows.append({'contract_sha256': spec.sha256,
                    'snapshot_ref': BASE if role == 'baseline' else CANDIDATE,
                    'role': role, 'position': position, 'probe': probe, 'case_id': item['case_id'],
                    'input_sha256': item['sha256'], 'execution': copy.deepcopy(execution),
                    'environment': {'python': '3.11.9', 'implementation': 'CPython', 'system': 'Linux',
                                    'machine': 'aarch64', 'clock': 'perf_counter_ns'},
                    'cleanup_ok': True, 'producer': 'independent_registered_workload_runner',
                    'samples': [value] * execution['repeats'], 'output_sha256': '3' * 64, 'output_length': 17})
    return spec, tests, rows


def score(spec, tests, rows):
    return score_final_measurements(spec, baseline_ref=BASE, candidate_ref=CANDIDATE,
                                     original_test_receipt=tests, measurements=rows)


class FinalPerformanceScoringTests(unittest.TestCase):
    def test_speed_and_memory_targets_each_require_the_other_resource_guard(self):
        self.assertEqual(score(*fixture())['status'], 'passed')
        self.assertEqual(score(*fixture(memory_ratio=1.3))['reason'], 'joint_resource_targets_not_met')
        self.assertEqual(score(*fixture('memory', time_ratio=1.05, memory_ratio=.7))['status'], 'passed')
        self.assertEqual(score(*fixture('memory', time_ratio=1.2, memory_ratio=.7))['status'], 'failed')
        self.assertEqual(score(*fixture('memory', time_ratio=.5, memory_ratio=.9))['status'], 'failed')

    def test_guardrail_case_cannot_be_averaged_away_by_a_fast_target(self):
        spec, tests, rows = fixture(time_ratio=.5)
        for row in rows:
            if row['case_id'] == 'final-guard' and row['probe'] == 'benchmark' and row['role'] == 'candidate':
                row['samples'] = [2.0] * len(row['samples'])
        self.assertEqual(score(spec, tests, rows)['status'], 'failed')

    def test_semantic_regression_cannot_be_compensated_by_resource_improvement(self):
        spec, tests, rows = fixture(time_ratio=.1, memory_ratio=.1)
        next(r for r in rows if r['role'] == 'candidate')['output_sha256'] = '4' * 64
        self.assertEqual(score(spec, tests, rows)['reason'], 'semantic_regression')

    def test_baseline_output_instability_is_environment_inconclusive(self):
        spec, tests, rows = fixture()
        rows[0]['output_sha256'] = '4' * 64
        result = score(spec, tests, rows)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['reason'], 'baseline_output_not_deterministic')

    def test_noise_and_zero_primary_baseline_never_pass(self):
        spec, tests, rows = fixture()
        rows[0]['samples'][0] = 10.0
        self.assertEqual(score(spec, tests, rows)['status'], 'inconclusive')
        spec, tests, rows = fixture('memory', memory_ratio=.5)
        for row in rows:
            if row['probe'] == 'memory': row['samples'] = [0] * len(row['samples'])
        self.assertEqual(score(spec, tests, rows)['status'], 'inconclusive')

    def test_expected_xfail_must_keep_exact_test_identity_and_status(self):
        spec, tests, rows = fixture()
        xfail = spec.as_dict()['original_tests']['expected_xfail'][0]
        tests['outcomes'][xfail] = 'skipped'
        self.assertEqual(score(spec, tests, rows)['reason'], 'original_test_regression')

    def test_development_input_missing_blocks_and_reused_blocks_are_rejected(self):
        for mutation in ('development', 'missing', 'duplicate'):
            spec, tests, rows = fixture()
            if mutation == 'development': rows[0]['case_id'] = spec.as_dict()['development_inputs'][0]['case_id']
            elif mutation == 'missing': rows.pop()
            else: rows[0] = copy.deepcopy(rows[1])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                score(spec, tests, rows)

    def test_foreign_versions_model_programs_and_unclean_runs_cannot_pass(self):
        for field, value in [('snapshot_ref', '9' * 64), ('contract_sha256', '9' * 64),
                             ('input_sha256', '9' * 64), ('producer', 'model_program_output')]:
            spec, tests, rows = fixture(); rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): score(spec, tests, rows)
        spec, tests, rows = fixture(); rows[0]['cleanup_ok'] = False
        self.assertEqual(score(spec, tests, rows)['status'], 'inconclusive')

    def test_numeric_types_nonfinite_values_and_environment_drift_are_rejected(self):
        for value in (True, float('nan'), float('inf'), -1, 10**1000):
            spec, tests, rows = fixture(); rows[0]['samples'][0] = value
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError): score(spec, tests, rows)
        spec, tests, rows = fixture(); rows[0]['execution']['cpus'] = True
        with self.assertRaises(ValueError): score(spec, tests, rows)
        spec, tests, rows = fixture(); rows[0]['environment']['python'] = 'different'
        with self.assertRaises(ValueError): score(spec, tests, rows)

    def test_additional_oracle_is_required_instead_of_silently_ignoring_declared_checks(self):
        spec, tests, rows = fixture()
        data = spec.as_dict(); data['semantics'] = contract()['semantics']
        changed = WorkloadSpec(data, adapters=ADAPTERS, checkers=CHECKERS)
        tests['contract_sha256'] = changed.sha256
        result = score(changed, tests, [])
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['reason'], 'additional_semantic_oracle_receipts_required')


if __name__ == '__main__':
    unittest.main()
