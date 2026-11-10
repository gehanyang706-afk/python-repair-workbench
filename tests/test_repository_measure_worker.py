"""Registered input validation without importing any target repository code."""
import hashlib
import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from repair_workbench.repository_measure_worker import canonical, validate_input, validate_job, _operation


class RegisteredMeasurementInputTests(unittest.TestCase):
    def test_comment_only_reproducer_does_not_append_a_terminating_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            origin = workspace / 'sqlparse/__init__.py'; origin.parent.mkdir(); origin.write_text('')
            observed = []
            library = SimpleNamespace(__file__=str(origin), format=lambda value, **kwargs: observed.append(value) or '')
            with patch.dict('sys.modules', {'sqlparse': library}):
                invoke = _operation({'workload_id': 'sql-comments-v3',
                    'input': {'size': 3, 'kind': 'line_comments'}}, workspace)
                invoke()
            self.assertEqual(observed, ['-- measured comment\n' * 3])
            self.assertNotIn('SELECT', observed[0])

    def test_only_bounded_data_is_accepted_not_dynamic_target_code(self):
        for workload, data in [('sql-format-v2', {'size': 1000, 'kind': 'line_comments'}),
                ('iterator-slice-v2', {'size': 20000, 'start': -20000, 'stop': -1, 'step': 1})]:
            validate_input(workload, data)
            job = {'workload_id': workload, 'input': data, 'probe': 'memory',
                   'input_sha256': hashlib.sha256(canonical(data).encode()).hexdigest(), 'warmups': 1, 'repeats': 3}
            validate_job(job)
            validate_job({**job, 'warmup_seconds': .5, 'gc_policy': 'collect_before_sample', 'batch_iterations': 30})
            with self.assertRaises(ValueError): validate_job({**job, 'warmup_seconds': True})
            with self.assertRaises(ValueError): validate_job({**job, 'command': 'arbitrary'})
            with self.assertRaises(ValueError): validate_job({**job, 'input_sha256': '0' * 64})

    def test_bools_zero_steps_and_oversized_workloads_are_rejected(self):
        for data in [{'size': True, 'kind': 'line_comments'}, {'size': 4001, 'kind': 'line_comments'},
                     {'size': 10, 'kind': 'arbitrary_python'}]:
            with self.subTest(data=data), self.assertRaises(ValueError): validate_input('sql-format-v2', data)
        for field, value in [('step', 0), ('step', True), ('start', -200001), ('size', 100001)]:
            data = {'size': 200, 'start': 0, 'stop': None, 'step': 1}; data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): validate_input('iterator-slice-v2', data)


if __name__ == '__main__':
    unittest.main()
