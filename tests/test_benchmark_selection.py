"""Metadata selection preserves screening and cannot inspect repair answers."""
import copy
import json
import unittest

from repair_workbench.benchmark_selection import (
    create_candidate_pool, validate_candidate_pool, freeze_qualified_subset, candidate_pool_from_json)


class BenchmarkSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'instance_id': f'owner__repo{repo}-{case}',
                      'repo': f'owner/repo{repo}', 'base_commit': f'{repo * 10 + case:040x}',
                      'patch': 'PRIVATE_GOLD', 'test_patch': 'PRIVATE_TEST',
                      'FAIL_TO_PASS': ['PRIVATE_TEST_ID']}
                     for repo in range(3) for case in range(1, 5)]
        self.options = {'dataset_name': 'SWE-bench/SWE-bench_Lite', 'revision': 'a' * 40,
                        'source_split': 'test', 'seed': 12,
                        'targets': {'dev': 2, 'validation': 2, 'test': 5},
                        'minimum_repositories': {'dev': 1, 'validation': 1, 'test': 3}}

    def pool(self, **kwargs):
        return create_candidate_pool(self.rows, **{**self.options, **kwargs})

    def summaries(self, pool, qualified=True):
        return {r['id']: {'qualified': qualified, 'evidence_sha256': 'b' * 64,
                          'reason': 'qualified' if qualified else 'environment_failed'}
                for r in pool['candidates']}

    def freeze(self, pool, summaries=None):
        return freeze_qualified_subset(pool, self.summaries(pool) if summaries is None else summaries,
                                       inference_started=False, expected_pool_sha256=pool['pool_sha256'])

    def test_order_is_deterministic_repo_interleaved_and_answer_independent(self):
        first = self.pool()
        self.rows.reverse()
        for row in self.rows:
            row.update(patch='different', test_patch='different', FAIL_TO_PASS=['different'])
        self.assertEqual(first, self.pool())
        self.assertNotIn('PRIVATE', json.dumps(first))
        by_id = {r['id']: r for r in first['candidates']}
        self.assertEqual(len({by_id[i]['repo'] for i in first['candidate_order'][:3]}), 3)
        self.assertEqual(validate_candidate_pool(first), first)

    def test_same_snapshot_cluster_cannot_cross_splits_or_inflate_task_count(self):
        self.rows[1]['base_commit'] = self.rows[0]['base_commit']
        pool = self.pool()
        selection = self.freeze(pool)
        selected = selection['selected']
        self.assertEqual(len({r['cluster_id'] for r in selected}), len(selected))
        pair = {self.rows[0]['instance_id'], self.rows[1]['instance_id']}
        self.assertLessEqual(sum(r['id'] in pair for r in selected), 1)

    def test_developer_exposure_applies_to_entire_cluster_and_only_dev(self):
        self.rows[1]['base_commit'] = self.rows[0]['base_commit']
        exposed = self.rows[0]['instance_id']
        pool = self.pool(developer_exposed=[exposed])
        cluster = next(r['cluster_id'] for r in pool['candidates'] if r['id'] == exposed)
        self.assertTrue(all(r['dev_only'] for r in pool['candidates'] if r['cluster_id'] == cluster))
        selection = self.freeze(pool)
        self.assertEqual([r['split'] for r in selection['selected'] if r['cluster_id'] == cluster], ['dev'])

    def test_failed_missing_and_excluded_tasks_remain_in_screening(self):
        pool = self.pool(metadata={self.rows[0]['instance_id']: {'eligible': False, 'reason': 'unsupported_platform'}})
        summaries = self.summaries(pool)
        missing, failed = self.rows[1]['instance_id'], self.rows[2]['instance_id']
        del summaries[missing]
        summaries[failed]['qualified'] = False
        summaries[failed]['reason'] = 'environment_failed'
        selection = self.freeze(pool, summaries)
        states = {r['id']: r['status'] for r in selection['screening']}
        self.assertEqual(len(states), len(self.rows))
        self.assertEqual(states[self.rows[0]['instance_id']], 'excluded')
        self.assertEqual(states[missing], 'not_checked')
        self.assertEqual(states[failed], 'qualification_failed')
        self.assertEqual(selection['counts']['source_rows'], 12)

    def test_insufficient_qualification_reports_shortfall_without_filling(self):
        pool = self.pool()
        selection = self.freeze(pool, {})
        self.assertEqual(selection['selected'], [])
        self.assertEqual(selection['shortfall'], self.options['targets'])
        self.assertEqual(selection['repository_shortfall'], self.options['minimum_repositories'])

    def test_no_selection_after_inference_or_with_model_scores(self):
        pool = self.pool()
        for started in (True, 0, None):
            with self.subTest(started=started), self.assertRaises(ValueError):
                freeze_qualified_subset(pool, {}, inference_started=started,
                                        expected_pool_sha256=pool['pool_sha256'])
        summaries = self.summaries(pool)
        next(iter(summaries.values()))['model_passed'] = True
        with self.assertRaisesRegex(ValueError, 'qualification_summary'):
            self.freeze(pool, summaries)

    def test_digest_and_derived_order_cannot_be_changed(self):
        pool = self.pool()
        with self.assertRaisesRegex(ValueError, 'unexpected_pool'):
            freeze_qualified_subset(pool, {}, inference_started=False, expected_pool_sha256='c' * 64)
        changed = copy.deepcopy(pool)
        changed['candidate_order'].reverse()
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            validate_candidate_pool(changed)

    def test_duplicate_keys_bools_and_unknown_metadata_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_json_key'):
            candidate_pool_from_json('[{"id":"a","id":"b"}]', **self.options)
        for options in ({'seed': True}, {'targets': {'dev': True, 'validation': 2, 'test': 5}},
                        {'minimum_repositories': False}, {'minimum_repositories': {}},
                        {'metadata': {'unknown': {}}},
                        {'metadata': {self.rows[0]['instance_id']: {'model_score': 1}}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.pool(**options)


if __name__ == '__main__':
    unittest.main()
