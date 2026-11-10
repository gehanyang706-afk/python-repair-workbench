"""Contract validation does not run target code or claim final evaluation wiring."""
import copy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest

from repair_workbench.workload_contract import WorkloadSpec, input_sha256


ADAPTERS = {'sql-format-dev-v1': 'a' * 64}
CHECKERS = {'sql-exact-output': 'b' * 64, 'sql-idempotence': 'c' * 64}


def case(identity, value, role='target'):
    return {'case_id': identity, 'role': role, 'input': value, 'sha256': input_sha256(value)}


def contract(goal='speed'):
    return {'schema_version': 1, 'task_id': 'sql-history-dev', 'repo': 'andialbrecht/sqlparse',
            'base_commit': 'd' * 40, 'workload_id': 'sql-format-dev-v1', 'workload_version': 'v1',
            'adapter_sha256': 'a' * 64, 'goal': goal,
            'thresholds': {'min_speedup': 0.15 if goal == 'speed' else 0,
                'min_memory_reduction': 0.2 if goal == 'memory' else 0,
                'max_time_ratio': 1.1, 'max_memory_ratio': 1.25, 'max_relative_noise': 0.2},
            'measurement': {'order': ['baseline', 'candidate', 'candidate', 'baseline'],
                'warmups': 2, 'repeats': 7, 'timeout_seconds': 30, 'image_id': 'sha256:' + 'e' * 64,
                'cpus': 1, 'memory_limit_bytes': 268435456,
                'time_metric': 'unprofiled_wall_seconds', 'memory_metric': 'python_traced_peak_bytes'},
            'semantics': {'strategy': 'baseline_equivalence', 'baseline_behavior_confirmed': True,
                'output_comparison': 'exact_bytes', 'contract_reference': 'docs/api.md at pinned base commit',
                'checker_id': 'sql-exact-output', 'checker_sha256': 'b' * 64, 'reference_sha256': None,
                'metamorphic_checks': [{'checker_id': 'sql-idempotence', 'checker_sha256': 'c' * 64,
                                        'relation': 'idempotence', 'version': 'v1'}]},
            'original_tests': {'suite_sha256': 'f' * 64,
                'nodeids': ['tests/test_format.py::test_comments', 'tests/test_format.py::test_known_bug'],
                'expected_xfail': ['tests/test_format.py::test_known_bug']},
            'development_inputs': [case('dev-1', {'sql': '-- public\nSELECT 1', 'repeats': 1000})],
            'final_inputs': [case('final-1', {'sql': '-- FINAL_INPUT_SENTINEL\nSELECT 2', 'repeats': 1700})]}


def make(data=None, **kwargs):
    return WorkloadSpec(contract() if data is None else data,
                        adapters=kwargs.get('adapters', ADAPTERS), checkers=kwargs.get('checkers', CHECKERS))


class WorkloadContractTests(unittest.TestCase):
    def test_speed_and_memory_goals_are_independently_explicit(self):
        for goal in ('speed', 'memory'):
            with self.subTest(goal=goal):
                spec = make(contract(goal))
                self.assertEqual(spec.as_dict()['goal'], goal)
                self.assertEqual(len(spec.sha256), 64)
        bad = contract('memory')
        bad['thresholds']['min_memory_reduction'] = 0
        with self.assertRaisesRegex(ValueError, 'primary goal'):
            make(bad)
        bad = contract('memory')
        bad['thresholds']['min_speedup'] = 0.15
        with self.assertRaisesRegex(ValueError, 'primary goal'):
            make(bad)

    def test_unregistered_adapter_or_checker_cannot_supply_acceptance_code(self):
        for field, value in [('adapter_sha256', '0' * 64), ('workload_id', 'import-arbitrary-module')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'trusted registry'):
                make({**contract(), field: value})
        for key, value in [('checker_id', 'unreviewed-checker'), ('checker_sha256', '0' * 64)]:
            data = contract(); data['semantics'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'trusted registry'):
                make(data)
        for section, field in [('semantics', 'python_code'), ('measurement', 'command'), ('thresholds', 'adaptive_threshold')]:
            data = contract(); data[section][field] = 'candidate-provided'
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, 'fields'):
                make(data)

    def test_split_checks_payload_identity_not_just_case_names(self):
        data = contract()
        same_input = copy.deepcopy(data['development_inputs'][0]['input'])
        data['final_inputs'] = [case('different-name', same_input)]
        with self.assertRaisesRegex(ValueError, 'overlap'):
            make(data)
        data['final_inputs'][0]['input']['sql'] = 'different input'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            make(data)
        data['final_inputs'] = [case('dev-1', {'genuinely_different': 7})]
        with self.assertRaisesRegex(ValueError, 'overlap'):
            make(data)

    def test_canonical_input_order_duplicates_and_input_versions_are_bound(self):
        self.assertEqual(input_sha256({'a': 1, 'b': 2}), input_sha256({'b': 2, 'a': 1}))
        data = contract()
        data['development_inputs'].append(case('dev-duplicate', data['development_inputs'][0]['input']))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            make(data)
        changed = contract(); changed['workload_version'] = 'v2'
        self.assertNotEqual(make().sha256, make(changed).sha256)

    def test_freezing_roundtrip_rejects_rewritten_hash_even_if_envelope_rehashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'contract.json'
            spec = make()
            expected = spec.write_frozen(path)
            loaded = WorkloadSpec.read_frozen(path, expected_sha256=expected, adapters=ADAPTERS, checkers=CHECKERS)
            self.assertEqual(loaded.as_dict(), spec.as_dict())
            with self.assertRaises(FileExistsError):
                spec.write_frozen(path)
            changed = spec.as_dict(); changed['thresholds']['min_speedup'] = 0.01
            replacement = make(changed)
            path.unlink(); replacement.write_frozen(path)
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                WorkloadSpec.read_frozen(path, expected_sha256=expected, adapters=ADAPTERS, checkers=CHECKERS)

    def test_caller_mutation_does_not_change_frozen_spec(self):
        data = contract(); spec = make(data); expected = spec.sha256
        data['final_inputs'][0]['input']['sql'] = 'tampered'
        copy_out = spec.as_dict(); copy_out['thresholds']['min_speedup'] = 0.001
        self.assertEqual(spec.sha256, expected)
        with self.assertRaises(FrozenInstanceError):
            spec._canonical = '{}'

    def test_development_projection_does_not_disclose_final_input_or_reference(self):
        spec = make()
        projected = spec.development_projection()
        text = json.dumps(projected)
        self.assertNotIn('FINAL_INPUT_SENTINEL', text)
        self.assertNotIn('final_inputs', projected)
        self.assertNotIn('checker_sha256', text)
        self.assertNotIn('reference_sha256', text)
        self.assertEqual(projected['development_inputs'], spec.as_dict()['development_inputs'])

    def test_expected_xfail_is_an_exact_status_allowlist_not_skipped_or_failed(self):
        spec = make()
        passed = {'tests/test_format.py::test_comments': 'passed', 'tests/test_format.py::test_known_bug': 'xfailed'}
        self.assertTrue(spec.original_test_result(passed)['accepted'])
        for status in ('passed', 'failed', 'error', 'skipped', 'xpassed', 'unknown'):
            with self.subTest(status=status):
                result = spec.original_test_result({**passed, 'tests/test_format.py::test_known_bug': status})
                self.assertFalse(result['accepted'])
        self.assertFalse(spec.original_test_result({'tests/test_format.py::test_comments': 'passed'})['accepted'])
        self.assertFalse(spec.original_test_result({**passed, 'tests/extra.py::test_new': 'passed'})['accepted'])

    def test_xfail_wildcards_unknown_tests_and_duplicate_nodeids_are_rejected(self):
        for values in [['tests/*.py::*'], ['tests/no.py::test_unknown'],
                       ['tests/test_format.py::test_known_bug'] * 2]:
            data = contract(); data['original_tests']['expected_xfail'] = values
            with self.subTest(values=values), self.assertRaises(ValueError):
                make(data)

    def test_baseline_equivalence_needs_confirmed_contract_and_known_comparison(self):
        for field, value in [('baseline_behavior_confirmed', False), ('output_comparison', 'anything_similar'),
                             ('contract_reference', ''), ('reference_sha256', 'a' * 64)]:
            data = contract(); data['semantics'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                make(data)
        data = contract(); data['semantics'].update(strategy='trusted_reference', reference_sha256='1' * 64,
                                                  baseline_behavior_confirmed=False)
        self.assertEqual(make(data).as_dict()['semantics']['strategy'], 'trusted_reference')

    def test_metamorphic_relation_must_be_explicit_registered_and_versioned(self):
        for field, value in [('relation', 'arbitrary_python_expression'), ('version', ''),
                             ('checker_sha256', '1' * 64)]:
            data = contract(); data['semantics']['metamorphic_checks'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                make(data)

    def test_numeric_bounds_versions_and_execution_metrics_are_strict(self):
        mutations = [('schema_version', True), ('base_commit', 'main'), ('goal', 'whatever')]
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                make({**contract(), key: value})
        for value in (True, float('nan'), float('inf'), -0.1, 10**1000):
            data = contract(); data['thresholds']['min_speedup'] = value
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                make(data)
        for key, value in [('image_id', 'python:latest'), ('time_metric', 'cProfile_total'),
                           ('memory_metric', 'gpu_memory'), ('repeats', True), ('warmups', 0),
                           ('order', ['baseline', 'candidate'])]:
            data = contract(); data['measurement'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                make(data)

    def test_json_objects_duplicates_symlinks_and_deep_inputs_fail_closed(self):
        data = contract(); data['development_inputs'][0]['input'] = lambda: None
        with self.assertRaises(ValueError):
            make(data)
        deep = []
        for _ in range(18):
            deep = [deep]
        with self.assertRaises(ValueError):
            input_sha256(deep)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'contract.json'; spec = make(); spec.write_frozen(path)
            content = path.read_text().replace('"format":"workload-contract-v1"',
                '"format":"workload-contract-v1","format":"workload-contract-v1"')
            path.write_text(content)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                WorkloadSpec.read_frozen(path, expected_sha256=spec.sha256, adapters=ADAPTERS, checkers=CHECKERS)
            linked = Path(tmp) / 'link.json'; linked.symlink_to(path)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                WorkloadSpec.read_frozen(linked, expected_sha256=spec.sha256, adapters=ADAPTERS, checkers=CHECKERS)


if __name__ == '__main__':
    unittest.main()
