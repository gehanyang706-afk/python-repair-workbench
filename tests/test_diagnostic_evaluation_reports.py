"""Offline report accounting with synthetic rows, not Agent performance results."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from repair_workbench import diagnostic_evaluation as evaluation


DATASET = Path(__file__).resolve().parents[1] / 'benchmarks/ugc-diagnostics-dev-v1'
IMAGE = 'sha256:' + 'a' * 64
INTEGER_METRICS = ('model_wrapper_calls', 'reported_usage_calls', 'missing_usage_calls',
                   'tools_admitted', 'tools_proposed', 'exploratory_probe_runs',
                   'reused_observations', 'assessed_tool_rounds', 'duplicate_only_rounds',
                   'candidate_attempts')


class DiagnosticEvaluationReportTests(unittest.TestCase):
    def setUp(self):
        with patch.object(evaluation, '_image_id', return_value=IMAGE):
            self.plan = evaluation.create_plan(
                DATASET, 'local:test', tasks=['case-01', 'case-04'], repeats=2, seed=19)
        self.rows = []
        for item in self.plan['schedule']:
            calls = 4 if item['condition'] == 'adaptive' else 5
            metrics = {
                'model_wrapper_calls': calls, 'reported_usage_calls': 0,
                'missing_usage_calls': calls, 'all_calls_total_tokens': None,
                'tools_admitted': calls - 1, 'tools_proposed': calls - 1,
                'exploratory_probe_runs': 1 if item['condition'] == 'adaptive' else 3,
                'reused_observations': 0, 'assessed_tool_rounds': calls - 1,
                'duplicate_only_rounds': 0, 'candidate_attempts': 0,
                'first_candidate_seconds': None, 'stop_reason': 'agent_finished',
                'diagnostic_stop_reason': None, 'decision_proposal': 'insufficient_evidence',
            }
            self.rows.append({**item, 'state': 'completed', 'started': True, 'audit_status': 'passed',
                              'baseline_id': ('a' if item['task_id'] == 'case-01' else 'b') * 64,
                              'metrics': metrics})

    def test_repeats_preserve_distinct_task_and_planned_pair_denominators(self):
        summary = evaluation.summarize(self.plan, self.rows)
        self.assertEqual(summary['distinct_tasks'], 2)
        self.assertEqual(summary['repeats'], 2)
        self.assertEqual(summary['planned_runs'], 8)
        self.assertEqual(summary['planned_pairs'], 4)
        self.assertEqual(summary['comparable_pairs'], 4)
        self.assertEqual(summary['started_runs'], 8)
        self.assertEqual(summary['audited_runs'], 8)
        self.assertEqual(summary['states'], {'completed': 8})

    def test_missing_duplicate_unknown_or_mislabelled_rows_are_rejected(self):
        variants = []
        variants.append(self.rows[:-1])
        duplicate = copy.deepcopy(self.rows)
        duplicate[-1] = copy.deepcopy(duplicate[0])
        variants.append(duplicate)
        unknown = copy.deepcopy(self.rows)
        unknown[0]['run_id'] = 'unplanned-run'
        variants.append(unknown)
        mismatched = copy.deepcopy(self.rows)
        mismatched[0]['repeat'] = 99
        variants.append(mismatched)
        for rows in variants:
            for function in (evaluation.summarize, evaluation.build_pairs):
                with self.subTest(function=function.__name__, rows=len(rows)), self.assertRaises(ValueError):
                    function(self.plan, rows)

    def test_not_run_and_invalid_rows_stay_in_the_full_planned_denominator(self):
        rows = copy.deepcopy(self.rows)
        one_pair = [row for row in rows if row['task_id'] == 'case-01' and row['repeat'] == 1]
        one_pair[0].update(state='not_run', started=False, audit_status=None, metrics={})
        one_pair[0].pop('baseline_id')
        one_pair[1].update(state='invalid', started=True, audit_status='failed', metrics={})
        one_pair[1].pop('baseline_id')
        summary = evaluation.summarize(self.plan, rows)
        self.assertEqual(summary['planned_runs'], 8)
        self.assertEqual(summary['planned_pairs'], 4)
        self.assertEqual(summary['audited_runs'], 6)
        self.assertEqual(summary['comparable_pairs'], 3)
        self.assertEqual(summary['started_runs'], 7)
        self.assertEqual(sum(summary['states'].values()), 8)
        self.assertEqual(summary['states']['not_run'], 1)
        self.assertEqual(summary['states']['invalid'], 1)
        self.assertIsNone(summary['all_calls_total_tokens'])
        self.assertFalse(summary['usage_complete_all_planned_runs'])

    def test_stopped_audited_side_and_failed_audit_are_not_complete_pairs(self):
        for changes in ({'state': 'stopped'}, {'state': 'cancelled'}, {'audit_status': 'failed'}):
            rows = copy.deepcopy(self.rows)
            rows[0].update(changes)
            pairs = evaluation.build_pairs(self.plan, rows)
            self.assertEqual(len(pairs), 4)
            incomplete = [pair for pair in pairs if not pair['comparable']]
            self.assertEqual(len(incomplete), 1)
            self.assertIsNone(incomplete[0]['delta_adaptive_minus_fixed'])

    def test_baseline_mismatch_is_not_a_comparable_pair(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['baseline_id'] = 'c' * 64
        pairs = evaluation.build_pairs(self.plan, rows)
        self.assertEqual(len(pairs), 4)
        self.assertEqual(sum(pair['comparable'] for pair in pairs), 3)
        self.assertIsNone(next(pair for pair in pairs if not pair['comparable'])['delta_adaptive_minus_fixed'])

    def test_unknown_usage_is_null_instead_of_zero_even_when_all_runs_are_audited(self):
        summary = evaluation.summarize(self.plan, self.rows)
        expected = sum(row['metrics']['model_wrapper_calls'] for row in self.rows)
        self.assertEqual(summary['model_wrapper_calls_for_audited_runs'], expected)
        self.assertEqual(summary['missing_usage_calls_for_audited_runs'], expected)
        self.assertIsNone(summary['all_calls_total_tokens'])
        self.assertFalse(summary['usage_complete_all_planned_runs'])

    def test_complete_usage_can_sum_but_one_missing_call_makes_all_run_total_unknown(self):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            metric = row['metrics']
            metric.update(reported_usage_calls=metric['model_wrapper_calls'], missing_usage_calls=0,
                          all_calls_total_tokens=100)
        complete = evaluation.summarize(self.plan, rows)
        self.assertEqual(complete['all_calls_total_tokens'], 800)
        self.assertTrue(complete['usage_complete_all_planned_runs'])
        rows[0]['metrics'].update(reported_usage_calls=rows[0]['metrics']['model_wrapper_calls'] - 1,
                                  missing_usage_calls=1, all_calls_total_tokens=None)
        incomplete = evaluation.summarize(self.plan, rows)
        self.assertIsNone(incomplete['all_calls_total_tokens'])
        self.assertFalse(incomplete['usage_complete_all_planned_runs'])

    def test_unknown_usage_relabelled_zero_or_inconsistent_call_counts_are_rejected(self):
        variants = (
            {'all_calls_total_tokens': 0},
            {'reported_usage_calls': 1},
            {'missing_usage_calls': -1},
            {'reported_usage_calls': True},
        )
        for mutation in variants:
            rows = copy.deepcopy(self.rows)
            rows[0]['metrics'].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                evaluation.summarize(self.plan, rows)

    def test_audited_integer_metrics_and_baseline_identity_are_strict(self):
        for key in INTEGER_METRICS:
            for invalid in (True, -1, '1'):
                rows = copy.deepcopy(self.rows)
                rows[0]['metrics'][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    evaluation.build_pairs(self.plan, rows)
        rows = copy.deepcopy(self.rows)
        rows[0]['baseline_id'] = 'not-a-snapshot'
        with self.assertRaises(ValueError):
            evaluation.summarize(self.plan, rows)

    def test_audited_subset_totals_are_explicitly_scoped_and_never_whole_batch_claims(self):
        rows = copy.deepcopy(self.rows)
        excluded = rows[0]
        excluded.update(state='invalid', audit_status='failed')
        excluded['metrics']['model_wrapper_calls'] = 9999
        summary = evaluation.summarize(self.plan, rows)
        expected = sum(row['metrics']['model_wrapper_calls'] for row in rows[1:])
        self.assertEqual(summary['model_wrapper_calls_for_audited_runs'], expected)
        self.assertEqual(summary['audited_runs'], 7)
        self.assertEqual(summary['planned_runs'], 8)
        self.assertNotIn('model_wrapper_calls', summary)
        self.assertIsNone(summary['all_calls_total_tokens'])

    def test_health_no_change_and_abstention_proposals_never_become_success_or_accuracy(self):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            if row['task_id'] == 'case-04':
                row['metrics']['decision_proposal'] = 'no_change'
        summary = evaluation.summarize(self.plan, rows)
        self.assertEqual(summary['decision_proposals'], {'no_change': 4, 'insufficient_evidence': 4})
        self.assertIsNone(summary['optimization_success_rate'])
        self.assertIsNone(summary['health_decision_accuracy'])
        self.assertFalse(summary['real_llm_performance_claim_allowed'])
        self.assertFalse(summary['cost_savings_claim_allowed'])
        self.assertEqual(summary['real_model_api_calls'], 0)

    def test_different_control_flow_counts_are_descriptive_and_never_token_savings(self):
        pairs = evaluation.build_pairs(self.plan, self.rows)
        for pair in pairs:
            self.assertTrue(pair['comparable'])
            self.assertEqual(pair['delta_adaptive_minus_fixed'], {
                'model_wrapper_calls': -1, 'tools_admitted': -1, 'exploratory_probe_runs': -2,
            })
            self.assertFalse(pair['token_savings_claim_allowed'])


if __name__ == '__main__':
    unittest.main()
