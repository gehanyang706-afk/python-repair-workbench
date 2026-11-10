"""Exact original-test inventory contracts, separate from model success scores."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from repair_workbench.test_receipt_worker import NodeRecorder
from repair_workbench.runner import validate_node_report


def phase(when, outcome='passed', xfail=False):
    args = {'nodeid': 'tests/test_a.py::test_exact', 'when': when, 'outcome': outcome}
    if xfail: args['wasxfail'] = 'original marker'
    return SimpleNamespace(**args)


class TestReceiptTests(unittest.TestCase):
    def recorder(self, reports):
        recorder = NodeRecorder()
        recorder.pytest_collection_modifyitems(None, None, [SimpleNamespace(nodeid='tests/test_a.py::test_exact')])
        for report in reports: recorder.pytest_runtest_logreport(report)
        return recorder

    def test_original_xfail_is_distinct_from_skip_and_unexpected_pass(self):
        for middle, expected in [(phase('call', 'skipped', True), 'xfailed'),
                                 (phase('call', 'skipped'), 'skipped'),
                                 (phase('call', 'passed', True), 'xpassed'),
                                 (phase('call', 'passed'), 'passed'),
                                 (phase('call', 'failed'), 'failed')]:
            with self.subTest(expected=expected):
                recorder = self.recorder([phase('setup'), middle, phase('teardown')])
                self.assertEqual(recorder.outcomes(), {'tests/test_a.py::test_exact': expected})

    def test_setup_xfail_teardown_error_and_incomplete_phases_are_preserved(self):
        for reports, expected in [([phase('setup', 'skipped', True), phase('teardown')], 'xfailed'),
                ([phase('setup'), phase('call'), phase('teardown', 'failed')], 'error'),
                ([phase('setup'), phase('call')], 'incomplete'),
                ([phase('setup'), phase('call'), phase('call'), phase('teardown')], 'incomplete')]:
            with self.subTest(expected=expected):
                self.assertEqual(next(iter(self.recorder(reports).outcomes().values())), expected)

    def test_independent_junit_totals_and_inventory_must_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nodeids.json'
            data = {'schema_version': 1, 'exitcode': 0, 'collected': ['tests/a.py::test_a'],
                    'outcomes': {'tests/a.py::test_a': 'xfailed'}, 'collection_errors': [], 'subtest_counts': {}}
            path.write_text(json.dumps(data))
            counts = {'total': 1, 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 1}
            result = validate_node_report(path, 0, counts)
            self.assertEqual(result['status_counts']['xfailed'], 1)
            with self.assertRaisesRegex(ValueError, 'junit_mismatch'):
                validate_node_report(path, 0, {**counts, 'passed': 1})
            with self.assertRaises(ValueError): validate_node_report(path, 1, counts)
            data['outcomes']['tests/a.py::test_a'] = 'incomplete'; path.write_text(json.dumps(data))
            with self.assertRaises(ValueError): validate_node_report(path, 0, counts)

    def test_duplicate_inventory_or_collection_failure_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nodeids.json'
            counts = {'total': 1, 'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0}
            base = {'schema_version': 1, 'exitcode': 0, 'collected': ['tests/a.py::test_a'],
                    'outcomes': {'tests/a.py::test_a': 'passed'}, 'collection_errors': [], 'subtest_counts': {}}
            for update in [{'collected': ['tests/a.py::test_a'] * 2}, {'collection_errors': ['tests/a.py']},
                           {'outcomes': {'tests/a.py::test_other': 'passed'}}]:
                path.write_text(json.dumps({**base, **update}))
                with self.subTest(update=update), self.assertRaises(ValueError): validate_node_report(path, 0, counts)

    def test_subtest_reports_are_aggregated_without_erasing_a_failure(self):
        cls = type('SubtestReport', (SimpleNamespace,), {'__module__': '_pytest.subtests'})
        recorder = self.recorder([phase('setup'), phase('call'), phase('teardown')])
        recorder.pytest_runtest_logreport(cls(nodeid='tests/test_a.py::test_exact', when='call', outcome='passed'))
        self.assertEqual(recorder.outcomes()['tests/test_a.py::test_exact'], 'passed')
        recorder.pytest_runtest_logreport(cls(nodeid='tests/test_a.py::test_exact', when='call', outcome='failed'))
        self.assertEqual(recorder.outcomes()['tests/test_a.py::test_exact'], 'failed')


if __name__ == '__main__':
    unittest.main()
