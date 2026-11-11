"""Public native graph closes before a separate trusted evaluator is invoked."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_repository_preparation as preparation_helpers
import test_repository_diagnosis as diagnosis_helpers
from repair_workbench import repository_execution as execution
from repair_workbench.session import BudgetExceeded


class RepositoryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.helper = preparation_helpers.RepositoryPreparationTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.root, self.base, self.spec = self.helper.root, self.helper.base, self.helper.spec
        self.model = diagnosis_helpers.RepositoryPolicy()
        self.output = self.root / 'execution'
        self.phases = []

    def run_suite(self, *args, **kwargs):
        result = self.helper.runner(*args, **kwargs)
        (args[0] / 'reports/pytest.xml').write_text('<testsuite><testcase classname="pricing" name="boundary"/></testsuite>')
        return result

    def grade(self, spec, baseline, candidate, output, allowed, **kwargs):
        self.assertEqual(self.model.calls, 5)
        self.assertTrue((self.output / 'agent/final-submission.json').exists())
        self.assertEqual(candidate, self.output / 'agent/attempt-01/verification')
        self.assertEqual(spec.sha256, self.spec.sha256)
        self.assertEqual(allowed, {'pricing.py'})
        self.assertNotIn('FINAL_CASE_SENTINEL', json.dumps(self.model.payload))
        self.assertNotIn('final_inputs', json.dumps(self.model.payload))
        self.assertEqual(self.phases, ['sealed'])
        self.phases.append('graded')
        return {'status': 'passed', 'reason': 'offline_fixed_grader_receipt', 'cleanup_ok': True}

    def execute(self, *, grader=None, **kwargs):
        with patch('repair_workbench.repository_preparation.run_tests', side_effect=self.run_suite), \
             patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', diagnosis_helpers.PublicProbeStub), \
             patch.object(execution, '_grade_in_separate_process', side_effect=grader or self.grade) as grade:
            result = execution.run_repository_diagnostic_task(self.spec, self.base, self.output,
                {'pricing.py'}, self.model, problem_statement='Investigate the public behavior without changing the contract.',
                on_final_submission=lambda digest: self.phases.append('sealed'), **kwargs)
        return result, grade

    def test_native_graph_is_closed_and_hidden_data_never_enters_model_messages(self):
        result, grade = self.execute()
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['agent_finished_before_final_evaluation'])
        self.assertTrue(result['independent_evaluation_started'])
        self.assertEqual(self.phases, ['sealed', 'graded'])
        self.assertEqual(grade.call_count, 1)
        self.assertEqual(self.model.calls, 5)

    def test_hidden_evaluator_error_does_not_start_a_second_model_attempt(self):
        def bad(*args, **kwargs): raise ValueError('offline evaluator unavailable')
        result, grade = self.execute(grader=bad)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(self.model.calls, 5)
        self.assertEqual(grade.call_count, 1)

    def test_post_grade_sealed_receipt_drift_cannot_publish_pass(self):
        def changed(*args, **kwargs):
            result = self.grade(*args, **kwargs)
            p = self.output / 'agent/final-submission.json'
            p.write_text(p.read_text() + ' ')
            return result
        result, _ = self.execute(grader=changed)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['reason'], 'repository_execution_error')

    def test_original_test_policy_is_shared_with_independent_grader(self):
        def checked(*args, **kwargs):
            self.assertEqual(kwargs['test_pids_limit'], 128)
            return self.grade(*args, **kwargs)
        result, _ = self.execute(grader=checked, test_pids_limit=128)
        self.assertEqual(result['status'], 'passed', result)

    def test_unconfirmed_evaluator_cleanup_cannot_publish_pass(self):
        def uncertain(*args, **kwargs):
            return {**self.grade(*args, **kwargs), 'cleanup_ok': False}
        result, _ = self.execute(grader=uncertain)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['reason'], 'independent_evaluation_cleanup_unconfirmed')

    def test_output_inside_source_is_rejected_without_creating_a_directory(self):
        output = self.base / 'workspace/forbidden-output'
        with self.assertRaisesRegex(ValueError, 'output_must_be_outside_source'):
            execution.run_repository_diagnostic_task(self.spec, self.base, output, {'pricing.py'},
                                                     self.model, problem_statement='public problem')
        self.assertFalse(output.exists())

    def test_cancel_before_preparation_is_persisted_without_model_or_grader(self):
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            self.execute(cancellation_check=lambda: True)
        self.assertEqual(self.model.calls, 0)
        self.assertFalse((self.output / 'agent').exists())
        result = json.loads((self.output / 'execution.json').read_text())
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse(result['cleanup_ok'])


if __name__ == '__main__':
    unittest.main()
