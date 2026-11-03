"""Strict offline development plans and source preparation; no Docker or API."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repair_workbench import diagnostic_evaluation as evaluation
from repair_workbench.snapshot import snapshot_id, verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'benchmarks/ugc-diagnostics-dev-v1'
IMAGE = 'sha256:' + 'a' * 64
MANIFEST_SHA256 = '2302915b4d2fa0951b22fa9347eb0076c268c6191736ab84588b98249c1f7e14'


class DiagnosticEvaluationPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def copy_dataset(self):
        target = self.root / 'dataset'
        shutil.copytree(DATASET, target, ignore=shutil.ignore_patterns('__pycache__'))
        return target

    def plan(self, **kwargs):
        with patch.object(evaluation, '_image_id', return_value=IMAGE):
            return evaluation.create_plan(DATASET, 'local:test', **kwargs)

    def rehash(self, plan):
        material = {key: value for key, value in plan.items() if key != 'plan_sha256'}
        plan['plan_sha256'] = hashlib.sha256(json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode()).hexdigest()

    def test_only_pinned_public_synthetic_development_registry_is_accepted(self):
        manifest, digest = evaluation.load_dataset(DATASET)
        self.assertEqual(digest, MANIFEST_SHA256)
        self.assertEqual(manifest['split'], 'dev')
        self.assertTrue(manifest['public'])
        self.assertTrue(manifest['synthetic'])
        self.assertFalse(manifest['independent_evaluation'])
        self.assertEqual([task['id'] for task in manifest['tasks']],
                         ['case-01', 'case-02', 'case-03', 'case-04'])

    def test_changed_source_and_extra_source_file_are_rejected(self):
        dataset = self.copy_dataset()
        path = dataset / 'case-01/source/map_exporter.py'
        original = path.read_bytes()
        path.write_bytes(original + b'\n# changed source\n')
        with self.assertRaises(ValueError):
            evaluation.load_dataset(dataset)
        path.write_bytes(original)
        (path.parent / 'unexpected.py').write_text('raise RuntimeError("must never execute")\n')
        with self.assertRaises(ValueError):
            evaluation.load_dataset(dataset)

    def test_symlinked_file_and_source_directory_are_rejected(self):
        dataset = self.copy_dataset()
        path = dataset / 'case-01/source/map_exporter.py'
        original = path.read_bytes()
        outside = self.root / 'outside.py'
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            evaluation.load_dataset(dataset)
        path.unlink()
        path.write_bytes(original)
        source = path.parent
        moved = self.root / 'external-source'
        source.rename(moved)
        source.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            evaluation.load_dataset(dataset)

    def test_symlinked_dataset_ancestor_is_rejected(self):
        dataset = self.copy_dataset()
        alias = self.root / 'dataset-alias'
        alias.symlink_to(dataset, target_is_directory=True)
        with self.assertRaises(ValueError):
            evaluation.load_dataset(alias)

    def test_registry_metadata_changes_cannot_be_rebranded_as_independent_data(self):
        dataset = self.copy_dataset()
        path = dataset / 'manifest.json'
        original = json.loads(path.read_text())
        for changes in ({'independent_evaluation': True}, {'public': False}, {'split': 'test'},
                        {'task_type': 'official_benchmark'}, {'version': 2}):
            path.write_text(json.dumps({**original, **changes}))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evaluation.load_dataset(dataset)

    def test_unknown_duplicate_and_empty_task_selections_fail_before_image_inspection(self):
        for tasks in (['unknown'], ['case-01', 'case-01'], [], ['case-01', '../case-02']):
            with self.subTest(tasks=tasks), patch.object(evaluation, '_image_id') as image:
                with self.assertRaises(ValueError):
                    evaluation.create_plan(DATASET, 'local:test', tasks=tasks)
                image.assert_not_called()

    def test_invalid_types_nonfinite_values_and_limits_fail_before_image_inspection(self):
        options = (
            {'repeats': True}, {'repeats': '1'}, {'repeats': 1.0}, {'repeats': 0},
            {'seed': True}, {'seed': '0'}, {'transport': 'http'},
            {'max_model_calls': True}, {'max_model_calls': '12'}, {'max_model_calls': 0},
            {'max_model_calls': 31}, {'max_attempts': True}, {'max_attempts': 6},
            {'max_probe_runs': True}, {'max_probe_runs': 2}, {'max_probe_runs': 13},
            {'deadline_seconds': True}, {'deadline_seconds': float('nan')},
            {'test_timeout': False}, {'test_timeout': float('inf')}, {'test_timeout': 121},
            {'batch_timeout_seconds': '900'}, {'batch_timeout_seconds': 0},
        )
        for kwargs in options:
            with self.subTest(kwargs=kwargs), patch.object(evaluation, '_image_id') as image:
                with self.assertRaises(ValueError):
                    evaluation.create_plan(DATASET, 'local:test', **kwargs)
                image.assert_not_called()

    def test_plan_schedule_is_repeatable_and_both_conditions_share_all_limits(self):
        first = self.plan(tasks=['case-04', 'case-01'], repeats=2, seed=27, transport='mcp')
        second = self.plan(tasks=['case-04', 'case-01'], repeats=2, seed=27, transport='mcp')
        self.assertEqual(first, second)
        self.assertEqual(first['mode'], 'scripted_offline')
        self.assertEqual(first['policy_id'], 'diagnostic_probe_policy_v1')
        self.assertEqual(first['image_id'], IMAGE)
        self.assertEqual(first['dataset']['sha256'], MANIFEST_SHA256)
        self.assertFalse(first['dataset']['independent_evaluation'])
        self.assertEqual(first['tasks'], ['case-01', 'case-04'])
        self.assertEqual(first['conditions'], [{'id': 'adaptive', 'strategy': 'adaptive'},
                                              {'id': 'fixed', 'strategy': 'fixed'}])
        self.assertFalse(first['common']['use_skills'])
        self.assertFalse(first['common']['use_investigator'])
        self.assertEqual(first['common']['diagnostic_transport'], 'mcp')
        self.assertEqual(first['common']['max_model_calls'], 12)
        self.assertEqual(first['common']['max_attempts'], 3)
        self.assertEqual(first['common']['max_probe_runs'], 6)
        schedule = first['schedule']
        self.assertEqual(len(schedule), 8)
        self.assertEqual(len({row['run_id'] for row in schedule}), 8)
        self.assertEqual({(row['task_id'], row['repeat'], row['condition']) for row in schedule},
                         {(task, repeat, condition) for task in ('case-01', 'case-04')
                          for repeat in (1, 2) for condition in ('adaptive', 'fixed')})
        for index in range(0, len(schedule), 2):
            a, b = schedule[index:index + 2]
            self.assertEqual((a['task_id'], a['repeat']), (b['task_id'], b['repeat']))
            self.assertEqual({a['condition'], b['condition']}, {'adaptive', 'fixed'})
        self.assertEqual(first['ceilings']['model_wrapper_calls'], 8 * 12)
        evaluation.validate_plan(first, DATASET)

    def test_rehashing_cannot_change_mode_policy_conditions_or_scoring_contract(self):
        original = self.plan()
        mutations = (
            lambda plan: plan.update(mode='live_provider'),
            lambda plan: plan.update(policy_id='arbitrary:factory'),
            lambda plan: plan['conditions'][1].update(strategy='adaptive'),
            lambda plan: plan['conditions'][0].update(max_model_calls=24),
            lambda plan: plan['common'].update(use_investigator=True),
            lambda plan: plan['common'].update(minimum_speedup=.01),
            lambda plan: plan.update(scoring_contract='memory_only'),
            lambda plan: plan.update(task_text='A replacement task that exposes privileged information.'),
            lambda plan: plan['ceilings'].update(model_wrapper_calls=99999),
            lambda plan: plan.update(unexpected_provider_factory='external.module:create'),
        )
        for mutate in mutations:
            candidate = copy.deepcopy(original)
            mutate(candidate)
            self.rehash(candidate)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                evaluation.validate_plan(candidate, DATASET)

    def test_plan_hash_mismatch_and_runtime_source_drift_are_rejected(self):
        plan = self.plan()
        changed = copy.deepcopy(plan)
        changed['mode'] = 'live_provider'
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            evaluation.validate_plan(changed, DATASET)
        drifted = copy.deepcopy(plan['source'])
        path = next(iter(drifted['files']))
        drifted['files'][path] = '0' * 64
        with patch.object(evaluation, 'source_fingerprint', return_value=drifted), \
                patch.object(evaluation, '_image_id') as image:
            with self.assertRaisesRegex(ValueError, 'source_drift'):
                evaluation.validate_plan(plan, DATASET)
            image.assert_not_called()

    def test_prepare_only_copies_declared_visible_files_and_repeats_same_commit(self):
        manifest, _ = evaluation.load_dataset(DATASET)
        task = manifest['tasks'][0]
        with patch.object(evaluation, '_image_id') as image:
            first = evaluation.prepare_task(DATASET, task, self.root / 'prepared-one')
            second = evaluation.prepare_task(DATASET, task, self.root / 'prepared-two')
        image.assert_not_called()
        a, b = verify_snapshot(first), verify_snapshot(second)
        expected = {'map_exporter.py', 'tests/test_export.py', 'README.md'}
        self.assertEqual({item['path'] for item in a['files']}, expected)
        self.assertEqual({item['path'] for item in b['files']}, expected)
        self.assertEqual(a['commit'], b['commit'])
        self.assertEqual(snapshot_id(a), snapshot_id(b))
        self.assertNotIn('qualification.py', {item['path'] for item in a['files']})
        self.assertNotIn('manifest.json', {item['path'] for item in a['files']})

    def test_prepare_refuses_existing_output_and_tampered_case(self):
        dataset = self.copy_dataset()
        manifest, _ = evaluation.load_dataset(dataset)
        task = manifest['tasks'][0]
        destination = self.root / 'existing'
        destination.mkdir()
        (destination / 'keep.txt').write_text('preserve')
        with self.assertRaises((ValueError, FileExistsError)):
            evaluation.prepare_task(dataset, task, destination)
        self.assertEqual((destination / 'keep.txt').read_text(), 'preserve')
        (dataset / task['source_dir'] / 'map_exporter.py').write_text('changed')
        with self.assertRaises(ValueError):
            evaluation.prepare_task(dataset, task, self.root / 'new-output')

    def test_preparation_refuses_outputs_inside_the_dataset(self):
        dataset = self.copy_dataset()
        manifest, _ = evaluation.load_dataset(dataset)
        target = dataset / 'case-01/source/evaluation-output'
        with self.assertRaises(ValueError):
            evaluation.prepare_task(dataset, manifest['tasks'][0], target)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
