"""Audit coverage/usage boundaries without asserting synthetic calls are real."""
import json
import unittest

from langchain_core.messages import AIMessage
import test_repository_live_evaluation as helpers
from scripts.audit_repository_batch import audit


class RepositoryBatchAuditTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.RepositoryBatchTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_preflight_stop_has_no_joint_rate_or_paired_token_delta(self):
        self.h.write_controls(positive_status='inconclusive')
        plan = self.h.plan()
        self.h.run_batch(plan)
        report = audit(self.h.root / 'batch', plan['manifest_sha256'])
        self.assertEqual(report['planned_pairs'], 1)
        self.assertTrue(all(v['joint_pass_rate'] is None for v in report['variants'].values()))
        self.assertIsNone(report['pairs'][0]['adaptive_minus_fixed_known_tokens'])
        self.assertTrue(all(row['agent_tools'] is None for row in report['rows']))

    def test_passed_row_needs_independent_artifact_and_then_keeps_paired_usage(self):
        plan = self.h.plan()
        report, _, _ = self.h.run_batch(plan)
        with self.assertRaisesRegex(ValueError, 'no_independent_acceptance'):
            audit(self.h.root / 'batch', plan['manifest_sha256'])
        for row in report['rows']:
            directory = self.h.root / 'batch' / row['run_id'] / 'repository'
            (directory / 'independent-evaluation').mkdir(parents=True)
            (directory / 'execution.json').write_text(json.dumps({'status': 'passed', 'cleanup_ok': True,
                'agent_finished_before_final_evaluation': True, 'independent_evaluation_started': True}))
            (directory / 'independent-evaluation/result.json').write_text('{"status":"passed","verdict":{"status":"passed"}}')
        summary = audit(self.h.root / 'batch', plan['manifest_sha256'])
        self.assertEqual(summary['pairs'][0]['adaptive_minus_fixed_known_tokens'], 0)
        self.assertTrue(all(v['joint_pass_rate'] == 1 for v in summary['variants'].values()))
        self.assertIn('synthetic versus real', summary['scope'])

    def test_unknown_usage_remains_reserved_and_is_not_zero_actual_usage(self):
        self.h.provider = lambda *args, **kwargs: AIMessage(content='no usage fixture')
        plan = self.h.plan()
        self.h.run_batch(plan)
        summary = audit(self.h.root / 'batch', plan['manifest_sha256'])
        started = next(row for row in summary['rows'] if row['ledger_entries'])
        self.assertEqual(started['settled_entries'], 0)
        self.assertEqual(started['unknown_or_reserved_entries'], 1)
        self.assertGreater(started['unknown_or_reserved_tokens'], 0)
        self.assertIsNone(summary['pairs'][0]['adaptive_minus_fixed_known_tokens'])


if __name__ == '__main__':
    unittest.main()
