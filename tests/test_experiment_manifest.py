"""Offline checks for frozen plans, paired fairness and evaluator isolation."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from repair_workbench import experiment_manifest as plans


class ExperimentManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source.py'
        self.source.write_text('value = 1\n')
        self.lock = self.root / 'dependencies.lock'
        self.lock.write_text('package==1.0\n')
        self.config = {
            'model': {'id': 'test-model', 'region': 'test-region', 'parameters': {'temperature': 0}},
            'tools': ['read_source', 'submit_ref_edits'],
            'limits': {'max_total_tokens': 120000, 'max_model_calls': 24,
                       'max_tool_calls': 120, 'deadline_seconds': 900},
            'dataset': {'revision': 'frozen-revision'},
            'environment': {'image': 'sha256:' + 'a' * 64},
            'scoring': {'id': 'independent-v1'},
        }
        self.tasks = [self.task('case-1', split='dev'), self.task('case-2', split='test')]
        self.variants = [{'id': 'baseline', 'policy': 'on_demand'},
                         {'id': 'candidate', 'policy': 'issue_evidence'}]
        self.files = {'source': self.source, 'dependencies': self.lock}

    def task(self, identifier, *, split='test', cluster=None):
        return {'id': identifier, 'cluster_id': cluster or identifier, 'split': split,
                'public': {'repo': 'example/project', 'base_commit': 'a' * 40,
                           'problem_statement': 'Reported behavior is incorrect.'},
                'evaluator': {'gold_patch': 'PRIVATE GOLD', 'hidden_test': 'PRIVATE TEST',
                              'path': '/private/evaluator/fixture.json'}}

    def manifest(self, **kwargs):
        args = dict(config=self.config, tasks=self.tasks, variants=self.variants,
                    files=self.files, repeats=2, seed=19)
        args.update(kwargs)
        return plans.create_manifest(**args)

    def rehash(self, value):
        value['manifest_sha256'] = hashlib.sha256(plans.canonical_bytes(
            {key: item for key, item in value.items() if key != 'manifest_sha256'})).hexdigest()

    def test_roundtrip_binds_files_and_normalizes_mapping_and_selection_order(self):
        first = self.manifest()
        config = dict(reversed(list(self.config.items())))
        second = self.manifest(config=config, tasks=list(reversed(self.tasks)),
                               variants=list(reversed(self.variants)),
                               files=dict(reversed(list(self.files.items()))))
        self.assertEqual(first, second)
        destination = self.root / 'plan.json'
        plans.write_manifest(destination, first)
        self.assertEqual(plans.load_manifest(destination, current_config=config,
                                            expected_sha256=first['manifest_sha256']), first)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual({item['label'] for item in first['files']}, {'source', 'dependencies'})

    def test_every_task_repeat_has_adjacent_complete_variant_pair(self):
        plan = self.manifest(repeats=3)
        self.assertEqual(len(plan['schedule']), 12)
        self.assertEqual(len({row['run_id'] for row in plan['schedule']}), 12)
        observed = set()
        for index in range(0, 12, 2):
            a, b = plan['schedule'][index:index + 2]
            self.assertEqual((a['task_id'], a['repeat']), (b['task_id'], b['repeat']))
            self.assertEqual({a['variant_id'], b['variant_id']}, {'baseline', 'candidate'})
            observed.add((a['task_id'], a['repeat']))
        self.assertEqual(observed, {(task, repeat) for task in ('case-1', 'case-2')
                                    for repeat in (1, 2, 3)})
        self.assertNotEqual(plan['schedule'], self.manifest(repeats=3, seed=20)['schedule'])

    def test_public_projection_contains_neither_hidden_metadata_nor_plan_paths(self):
        plan = self.manifest(exposures=[{'task_id': 'case-2', 'actor': 'evaluator',
                                         'kind': 'qualification_read', 'note': 'PRIVATE NOTE'}])
        public = plans.agent_task(plan, 'case-2')
        self.assertEqual(set(public), {'id', 'repo', 'base_commit', 'problem_statement'})
        self.assertEqual(public['id'], 'case-2')
        self.assertNotIn('PRIVATE', json.dumps(public))
        self.assertNotIn(str(self.root), json.dumps(public))
        public['problem_statement'] = 'mutated'
        self.assertNotEqual(plans.agent_task(plan, 'case-2')['problem_statement'], 'mutated')
        with self.assertRaisesRegex(ValueError, 'unknown task'):
            plans.agent_task(plan, 'unknown')

    def test_unapproved_public_fields_and_extra_task_fields_are_rejected(self):
        for where in ('public', 'task'):
            tasks = copy.deepcopy(self.tasks)
            target = tasks[0]['public'] if where == 'public' else tasks[0]
            target['gold_patch'] = 'must never be projected'
            with self.subTest(where=where), self.assertRaises(ValueError):
                self.manifest(tasks=tasks)

    def test_same_size_file_drift_and_missing_file_fail(self):
        plan = self.manifest()
        self.source.write_text('value = 2\n')
        with self.assertRaisesRegex(ValueError, 'bound_file_drift'):
            plans.validate_manifest(plan)
        self.source.write_text('value = 1\n')
        self.lock.unlink()
        with self.assertRaises(FileNotFoundError):
            plans.validate_manifest(plan)

    def test_runtime_configuration_drift_and_original_digest_pin_fail_closed(self):
        plan = self.manifest()
        changed = copy.deepcopy(self.config)
        changed['limits']['max_total_tokens'] += 1
        with self.assertRaisesRegex(ValueError, 'runtime_configuration_drift'):
            plans.validate_manifest(plan, current_config=changed)
        replacement = self.manifest(config=changed)
        with self.assertRaisesRegex(ValueError, 'unexpected manifest digest'):
            plans.validate_manifest(replacement, expected_sha256=plan['manifest_sha256'])
        with self.assertRaises(ValueError):
            plans.validate_manifest(plan, expected_sha256=True)

    def test_changed_plan_without_rehash_is_rejected(self):
        plan = self.manifest()
        plan['tasks'][0]['public']['problem_statement'] = 'another task'
        with self.assertRaisesRegex(ValueError, 'manifest_hash_mismatch'):
            plans.validate_manifest(plan)

    def test_duplicate_task_id_and_cross_split_defect_cluster_are_rejected(self):
        cases = (
            [self.task('same', split='dev'), self.task('same', split='test')],
            [self.task('a', split='dev', cluster='same'), self.task('b', cluster='same')],
            [self.task('a', split='validation', cluster='same'), self.task('b', cluster='same')],
        )
        for tasks in cases:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                self.manifest(tasks=tasks)
        self.manifest(tasks=[self.task('a', split='dev', cluster='same'),
                             self.task('b', split='dev', cluster='same')])

    def test_developer_exposure_requires_dev_and_evaluator_qualification_does_not(self):
        for kind in ('root_cause', 'reference_patch', 'hidden_feedback', 'task_tuning', 'qualification_read'):
            event = {'task_id': 'case-2', 'actor': 'developer', 'kind': kind}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'developer-exposed'):
                self.manifest(exposures=[event])
            event['task_id'] = 'case-1'
            plans.validate_manifest(self.manifest(exposures=[event]))
        event = {'task_id': 'case-2', 'actor': 'evaluator', 'kind': 'qualification_read'}
        plans.validate_manifest(self.manifest(exposures=[event]))
        tasks = [self.task('case-2', split='validation')]
        with self.assertRaisesRegex(ValueError, 'developer-exposed'):
            self.manifest(tasks=tasks, exposures=[{**event, 'actor': 'developer'}])

    def test_unknown_or_mislabeled_exposures_cannot_bypass_split_checks(self):
        events = (
            {'task_id': 'unknown', 'actor': 'evaluator', 'kind': 'qualification_read'},
            {'task_id': 'case-2', 'actor': 'evaluator', 'kind': 'hidden_feedback'},
            {'task_id': 'case-2', 'actor': 'operator', 'kind': 'qualification_read'},
            {'task_id': 'case-2', 'actor': True, 'kind': 'qualification_read'},
        )
        for event in events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.manifest(exposures=[event])

    def test_per_variant_budget_model_and_tools_overrides_are_rejected(self):
        for override in ({'limits': {'max_model_calls': 50}}, {'model': 'stronger'},
                         {'tools': ['extra_tool']}, {'policy': {'max_model_calls': 50}}):
            variants = copy.deepcopy(self.variants)
            variants[1].update(override)
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.manifest(variants=variants)

    def test_rehashed_schedule_tampering_and_bool_fields_are_rejected(self):
        for field in ('schedule', 'schema_version', 'repeats', 'seed'):
            plan = self.manifest()
            if field == 'schedule':
                plan[field].pop()
            else:
                plan[field] = True
            self.rehash(plan)
            with self.subTest(field=field), self.assertRaises(ValueError):
                plans.validate_manifest(plan)

    def test_numeric_limits_reject_bools_fractional_counts_and_nonfinite_values(self):
        for field, value in (('max_total_tokens', True), ('max_model_calls', 1.5),
                             ('max_tool_calls', '120'), ('max_tool_calls', 0),
                             ('deadline_seconds', False), ('deadline_seconds', float('nan')),
                             ('deadline_seconds', float('inf')), ('deadline_seconds', -1)):
            config = copy.deepcopy(self.config)
            config['limits'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.manifest(config=config)
        config = copy.deepcopy(self.config)
        config['limits']['deadline_seconds'] = 0.5
        self.manifest(config=config)
        for options in ({'repeats': True}, {'seed': False}, {'repeats': 1.0}, {'seed': -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.manifest(**options)

    def test_rehashed_schedule_rejects_bool_and_float_repeat_aliases(self):
        for value in (True, 1.0):
            plan = self.manifest()
            plan['schedule'][0]['repeat'] = value
            self.rehash(plan)
            with self.subTest(value=value), self.assertRaises(ValueError):
                plans.validate_manifest(plan)

    def test_canonical_json_does_not_coerce_non_string_keys_or_python_objects(self):
        for value in ({1: 'not a string key'}, {'x': {1, 2}}, {'x': (1, 2)}, {'x': float('nan')}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plans.canonical_bytes(value)

    def test_creation_and_projection_do_not_share_mutable_inputs(self):
        plan = self.manifest()
        self.config['model']['id'] = 'changed-after-freeze'
        self.tasks[0]['evaluator']['gold_patch'] = 'changed-after-freeze'
        self.assertEqual(plan['config']['model']['id'], 'test-model')
        self.assertEqual(plan['tasks'][0]['evaluator']['gold_patch'], 'PRIVATE GOLD')

    def test_exclusive_write_preserves_existing_record(self):
        destination = self.root / 'plan.json'
        destination.write_text('original evidence')
        with self.assertRaises(FileExistsError):
            plans.write_manifest(destination, self.manifest())
        self.assertEqual(destination.read_text(), 'original evidence')

    def test_symlink_binding_substitution_and_manifest_destination_are_rejected(self):
        alias = self.root / 'source-alias.py'
        alias.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.manifest(files={'alias': alias})
        plan = self.manifest()
        moved = self.root / 'moved.py'
        self.source.rename(moved)
        self.source.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            plans.validate_manifest(plan)
        self.source.unlink()
        moved.rename(self.source)
        directory_alias = self.root / 'directory-alias'
        directory_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            plans.write_manifest(directory_alias / 'plan.json', plan)

    def test_duplicate_json_keys_and_rehashed_invalid_receipts_are_rejected(self):
        path = self.root / 'duplicate.json'
        path.write_text('{"schema_version": 1, "schema_version": 1}')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
            plans.load_manifest(path)
        plan = self.manifest()
        plan['files'][0]['size'] = True
        self.rehash(plan)
        with self.assertRaises(ValueError):
            plans.validate_manifest(plan)

    def test_empty_required_inputs_and_duplicate_variants_are_rejected(self):
        for options in ({'tasks': []}, {'files': {}}, {'variants': []},
                        {'variants': [self.variants[0], self.variants[0]]},
                        {'files': {'a': self.source, 'b': self.source}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.manifest(**options)


if __name__ == '__main__':
    unittest.main()
