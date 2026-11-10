"""Measurement receipt boundaries with fixed JSON children, never target imports."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_agent import fixture
from repair_workbench.repository_measure_worker import canonical
from repair_workbench.repository_measurement import run_repository_measurement, validate_measurement
from repair_workbench.session import BudgetExceeded


def job_and_report():
    data = {'size': 3, 'start': 0, 'stop': None, 'step': 1}
    job = {'workload_id': 'iterator-slice-v2', 'input': data,
        'input_sha256': hashlib.sha256(canonical(data).encode()).hexdigest(),
        'probe': 'memory', 'warmups': 1, 'repeats': 3}
    report = {'schema_version': 1, 'workload_id': job['workload_id'], 'input_sha256': job['input_sha256'],
        'probe': 'memory', 'samples': [100] * 3, 'output_sha256': ['1' * 64] * 3, 'output_lengths': [3] * 3,
        'environment': {'python': '3.13', 'implementation': 'CPython', 'system': 'Linux',
                        'machine': 'aarch64', 'clock': 'perf_counter_ns'}}
    return job, report


class RepositoryMeasurementTests(unittest.TestCase):
    def test_missing_samples_foreign_inputs_and_invalid_types_are_rejected(self):
        job, report = job_and_report()
        validate_measurement(report, job)
        for field, value in [('samples', [100]), ('samples', [True] * 3),
                             ('samples', [float('nan')] * 3), ('input_sha256', '0' * 64),
                             ('output_lengths', [False] * 3)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_measurement({**report, field: value}, job)

    def execute(self, *, cleanup=True, mutate=False, cancelled=False):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); baseline = fixture(root); job, report = job_and_report()
        original = subprocess.Popen
        class Control:
            calls = 0
            def check(self):
                self.calls += 1
                if cancelled and self.calls >= 3: raise BudgetExceeded('cancel_requested')
        def launch(*args, **kwargs):
            if mutate: (baseline / 'workspace/pricing.py').write_text('# changed during execution\n')
            return original([sys.executable, '-c', 'import json; print(json.dumps(' + repr(report) + '))'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        def remove(args, **kwargs):
            missing = cleanup and 'inspect' in args
            return subprocess.CompletedProcess(args, 1 if missing else 0, '', 'Error: No such object' if missing else '')
        with patch('repair_workbench.repository_measurement.resolve_image', return_value='sha256:' + 'a' * 64), \
             patch('repair_workbench.repository_measurement.subprocess.Popen', side_effect=launch), \
             patch('repair_workbench.repository_measurement.subprocess.run', side_effect=remove):
            if cancelled:
                with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
                    run_repository_measurement(baseline, root / 'measurement', 'local', job, execution_control=Control())
                result = json.loads((root / 'measurement/result.json').read_text())
            else:
                result = run_repository_measurement(baseline, root / 'measurement', 'local', job)
        return result

    def test_completed_observation_needs_both_cleanup_and_input_integrity(self):
        result = self.execute()
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(result['cleanup_ok'])
        self.assertTrue(result['input_integrity_ok'])
        self.assertEqual(self.execute(cleanup=False)['status'], 'cleanup_unconfirmed')
        self.assertEqual(self.execute(mutate=True)['status'], 'input_changed')

    def test_root_cancellation_propagates_after_saving_and_cleanup(self):
        result = self.execute(cancelled=True)
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['cleanup_ok'])


if __name__ == '__main__':
    unittest.main()
