import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('pilot_report',
    Path(__file__).resolve().parents[1] / 'scripts/report_public_pilot.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class PublicPilotReportTests(unittest.TestCase):
    def row(self, variant, outcome='passed', stop='agent_finished', repeat=1):
        return {'id': variant + str(repeat), 'variant': variant, 'task': 'bug', 'repeat': repeat,
                'state': 'completed', 'outcome': outcome,
                'agent': {'stop_reason': stop, 'attempts': [{'status': 'passed'}]}}

    def call(self, row, tokens, state='settled'):
        return {'run_id': row['id'], 'state': state, 'input_tokens': tokens if state == 'settled' else None,
                'output_tokens': 0 if state == 'settled' else None, 'charged_tokens': tokens}

    def test_failed_low_cost_run_does_not_become_success_cost_saving(self):
        b, c = self.row('on_demand'), self.row('failure_context', 'failed')
        result = report.analyze({'rows': [b, c], 'stop_reason': None},
                                {'calls': [self.call(b, 100), self.call(c, 10)]})
        self.assertEqual(result['comparable_pairs'], 1)
        self.assertEqual(result['common_passed_pairs'], 0)
        self.assertIsNone(result['common_passed_token_reduction_fraction'])
        self.assertIsNone(result['paired']['failure_context']['tokens_per_independent_success'])

    def test_global_budget_truncation_is_excluded_from_paired_comparison(self):
        b, c = self.row('on_demand'), self.row('failure_context', 'batch_token_limit', 'batch_token_limit')
        result = report.analyze({'rows': [b, c], 'stop_reason': 'batch_token_limit'},
                                {'calls': [self.call(b, 100)]})
        self.assertEqual(result['planned_pairs'], 1)
        self.assertEqual(result['comparable_pairs'], 0)
        self.assertIsNone(result['paired_token_reduction_fraction'])

    def test_unknown_usage_remains_unknown(self):
        b, c = self.row('on_demand'), self.row('failure_context')
        result = report.analyze({'rows': [b, c], 'stop_reason': None},
                                {'calls': [self.call(b, 100), self.call(c, 34816, 'usage_unknown')]})
        self.assertIsNone(result['row_usage'][c['id']])
        self.assertIsNone(result['paired_token_reduction_fraction'])

    def test_repeats_are_not_counted_as_distinct_bugs(self):
        rows = [self.row(v, repeat=i) for i in (1, 2, 3) for v in report.VARIANTS]
        result = report.analyze({'rows': rows, 'stop_reason': None},
                                {'calls': [self.call(r, 10) for r in rows]})
        self.assertEqual(result['comparable_pairs'], 3)
        self.assertEqual(result['paired']['on_demand']['distinct_bugs'], 1)


if __name__ == '__main__':
    unittest.main()
