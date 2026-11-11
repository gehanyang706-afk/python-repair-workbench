"""Public qualification contains no private final data; all runner use is offline."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_agent import fixture
from test_workload_contract import case, contract
from repair_workbench import repository_preparation as prep
from repair_workbench.repository_performance import registered_implementations, original_suite_sha256
from repair_workbench.snapshot import snapshot_id, verify_snapshot
from repair_workbench.workload_contract import WorkloadSpec
from repair_workbench.session import BudgetExceeded


class RepositoryPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = fixture(self.root)
        data = contract()
        adapters, checkers = registered_implementations()
        data.update(base_commit=verify_snapshot(self.base)['commit'], workload_id='sql-comments-v3',
                    adapter_sha256=adapters['sql-comments-v3'])
        data['measurement'].update(warmups=1, repeats=3)
        data['semantics'].update(output_comparison='canonical_json', checker_id='canonical-output-v1',
            checker_sha256=checkers['canonical-output-v1'], metamorphic_checks=[])
        data['original_tests'] = {'suite_sha256': original_suite_sha256(verify_snapshot(self.base)),
            'nodeids': ['tests/test_pricing.py::test_boundary'], 'expected_xfail': []}
        data['development_inputs'] = [case('public', {'size': 1, 'kind': 'line_comments'})]
        data['final_inputs'] = [case('FINAL_CASE_SENTINEL', {'size': 987, 'kind': 'quoted_markers'})]
        self.spec = WorkloadSpec(data, adapters=adapters, checkers=checkers)
        self.dest = self.root / 'prepared'

    def runner(self, clone, image, timeout, **kwargs):
        self.assertNotEqual(clone, self.base)
        self.assertTrue(kwargs['collect_nodeids'])
        self.assertFalse((clone / 'reports').exists())
        reports = clone / 'reports'; reports.mkdir()
        result = {'status': 'tests_passed', 'cleanup_ok': True, 'image_id': image,
            'snapshot_id': snapshot_id(verify_snapshot(clone)),
            'counts': {'total': 1, 'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0},
            'test_inventory': {'outcomes': {'tests/test_pricing.py::test_boundary': 'passed'}}}
        (reports / 'result.json').write_text(json.dumps(result))
        return result

    def test_fresh_public_baseline_and_receipt_bind_actual_runner_report(self):
        original = (self.base / 'reports/result.json').read_bytes()
        with patch.object(prep, 'run_tests', side_effect=self.runner):
            prepared = prep.prepare_repository_diagnostic_baseline(self.spec, self.base, self.dest)
        self.assertEqual((self.base / 'reports/result.json').read_bytes(), original)
        public = prepared['development_workload']
        self.assertEqual(public, self.spec.development_projection())
        self.assertNotIn('FINAL_CASE_SENTINEL', json.dumps(public))
        self.assertNotIn('final_inputs', public)
        receipt = prepared['baseline_qualification']
        raw = (prepared['baseline'] / 'reports/result.json').read_bytes()
        self.assertEqual(receipt['report_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(receipt['original_tests_accepted'])
        self.assertFalse((prepared['baseline'] / 'contract.json').exists())
        self.assertFalse((self.dest / 'contract.json').exists())

    def test_collection_or_cleanup_failure_cannot_publish_qualification(self):
        def bad(*args, **kwargs):
            return {**self.runner(*args, **kwargs), 'cleanup_ok': False}
        with patch.object(prep, 'run_tests', side_effect=bad):
            with self.assertRaisesRegex(ValueError, 'unqualified'):
                prep.prepare_repository_diagnostic_baseline(self.spec, self.base, self.dest)
        self.assertFalse((self.dest / 'qualification.json').exists())
        self.assertEqual(json.loads((self.dest / 'preparation.json').read_text())['status'], 'unqualified')

    def test_missing_original_nodeid_is_not_accepted(self):
        def bad(*args, **kwargs):
            result = self.runner(*args, **kwargs)
            result['test_inventory']['outcomes'] = {}
            return result
        with patch.object(prep, 'run_tests', side_effect=bad):
            with self.assertRaisesRegex(ValueError, 'inventory_unqualified'):
                prep.prepare_repository_diagnostic_baseline(self.spec, self.base, self.dest)
        self.assertFalse((self.dest / 'qualification.json').exists())

    def test_root_cancel_is_recorded_and_never_starts_tests(self):
        class Control:
            def check(self): raise BudgetExceeded('cancel_requested')
        with patch.object(prep, 'run_tests') as run:
            with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
                prep.prepare_repository_diagnostic_baseline(self.spec, self.base, self.dest, execution_control=Control())
        run.assert_not_called()
        self.assertEqual(json.loads((self.dest / 'preparation.json').read_text())['reason'], 'cancel_requested')


if __name__ == '__main__':
    unittest.main()
