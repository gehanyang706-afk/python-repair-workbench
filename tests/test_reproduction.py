"""Reproducer ownership and real execution boundaries; no real model calls."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_agent import fixture
from repair_workbench.reproduction import ReproductionWorkspace


class ReproductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.baseline = fixture(self.root)
        self.admitted = []

    def workspace(self, **kwargs):
        with patch('repair_workbench.reproduction.resolve_image', return_value='sha256:' + 'a' * 64):
            return ReproductionWorkspace(self.baseline, self.root / 'repro', 'local-image',
                                         admit=self.admitted.append, **kwargs)

    def test_version_bound_ref_and_rejected_programs_consume_admission(self):
        workspace = self.workspace()
        with self.assertRaises(ValueError):
            workspace.propose_reproducer('print("no run function")')
        ref = workspace.propose_reproducer('def run():\n    return {"input_size": 100}\n')
        self.assertTrue(ref['reproducer_ref'].startswith('rp-'))
        self.assertNotIn(str(self.root), str(ref))
        self.assertEqual(workspace.proposals, 2)
        self.assertEqual(self.admitted, ['propose_reproducer', 'propose_reproducer'])
        self.assertEqual((self.baseline / 'workspace/pricing.py').read_text().count('input_size'), 0)

    def test_unissued_ref_and_changed_code_rejected_before_docker(self):
        workspace = self.workspace()
        with patch('repair_workbench.reproduction.subprocess.Popen') as execute:
            with self.assertRaisesRegex(ValueError, 'unknown_reproducer'):
                workspace.run_reproducer('../outside')
            ref = workspace.propose_reproducer('def run():\n    return 1\n')['reproducer_ref']
            (workspace.output / ref / 'program.py').write_text('def run():\n    return 2\n')
            with self.assertRaisesRegex(ValueError, 'program_changed'):
                workspace.run_reproducer(ref)
        execute.assert_not_called()

    def test_resource_bools_and_output_inside_source_are_rejected(self):
        with self.assertRaises(ValueError):
            self.workspace(max_runs=True)
        with self.assertRaisesRegex(ValueError, 'outside'):
            ReproductionWorkspace(self.baseline, self.baseline / 'repro', 'local-image')

    def execute_observation(self, workspace, ref, *, cleanup_ok=True, mutate=None):
        """The child emits fixed JSON only; it never imports target repository code."""
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            if mutate:
                mutate()
            return popen([sys.executable, '-c',
                'print(\'{"schema_version":1,"observation":{"claim":"passed"}}\')'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        def cleanup(args, **kwargs):
            missing = cleanup_ok and 'inspect' in args
            return subprocess.CompletedProcess(args, 1 if missing else 0, '',
                                               'Error: No such object' if missing else '')
        with patch('repair_workbench.reproduction.resolve_image', return_value=workspace.image_id), \
             patch('repair_workbench.reproduction.subprocess.Popen', side_effect=launch), \
             patch('repair_workbench.reproduction.subprocess.run', side_effect=cleanup):
            return workspace.run_reproducer(ref)

    def test_unknown_cleanup_blocks_workspace_and_cannot_look_completed(self):
        workspace = self.workspace()
        ref = workspace.propose_reproducer('def run():\n    return 1\n')['reproducer_ref']
        result = self.execute_observation(workspace, ref, cleanup_ok=False)
        self.assertEqual(result['status'], 'cleanup_unconfirmed')
        self.assertFalse(result['cleanup_ok'])
        for action in (lambda: workspace.run_reproducer(ref),
                       lambda: workspace.propose_reproducer('def run():\n    return 2\n')):
            with self.assertRaisesRegex(ValueError, 'cleanup_unconfirmed'):
                action()
        self.assertEqual(json.loads((workspace.output / 'workspace.json').read_text())[
            'blocked_reason'], 'reproduction_cleanup_unconfirmed')

    def test_source_or_program_drift_during_execution_invalidates_observation(self):
        workspace = self.workspace()
        ref = workspace.propose_reproducer('def run():\n    return 1\n')['reproducer_ref']
        result = self.execute_observation(workspace, ref, mutate=lambda:
            (self.baseline / 'workspace/pricing.py').write_text('# Changed during execution\n'))
        self.assertEqual(result['status'], 'input_changed')
        self.assertFalse(result['input_integrity_ok'])
        self.assertTrue(result['cleanup_ok'])
        self.assertEqual(workspace.blocked_reason, 'reproduction_inputs_changed')
        self.assertEqual(json.loads((workspace.output / 'run-01/result.json').read_text())['status'],
                         'input_changed')

    def test_wrapper_preflight_does_not_create_an_unrecorded_run(self):
        workspace = self.workspace()
        ref = workspace.propose_reproducer('def run():\n    return 1\n')['reproducer_ref']
        workspace.wrapper_path.write_text('changed_wrapper = True\n')
        with self.assertRaisesRegex(ValueError, 'wrapper_changed'):
            workspace.run_reproducer(ref)
        self.assertEqual(workspace.runs, 0)
        self.assertFalse((workspace.output / 'run-01').exists())

    def test_mount_preflight_after_run_admission_retains_result(self):
        workspace = self.workspace()
        ref = workspace.propose_reproducer('def run():\n    return 1\n')['reproducer_ref']
        # A comma in a Docker --mount source is unsupported. The failed run is
        # retained once its allowance has been consumed, even before Popen.
        workspace.wrapper_path = workspace.output / 'comma,worker.py'
        workspace.wrapper_path.write_bytes((workspace.output / '_worker.py').read_bytes())
        with patch('repair_workbench.reproduction.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 1, '', 'Error: No such object')):
            result = workspace.run_reproducer(ref)
        self.assertEqual(result['status'], 'execution_error')
        self.assertEqual(workspace.runs, 1)
        self.assertTrue((workspace.output / 'run-01/result.json').is_file())


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'requires local Docker image')
class ReproductionDockerTests(ReproductionTests):
    def test_real_execution_readonly_output_cap_and_timeout_all_cleanup(self):
        workspace = ReproductionWorkspace(self.baseline, self.root / 'docker-repro',
            os.environ.get('REPAIR_TEST_IMAGE', 'repair-workbench-test:20261014-pinned'),
            max_programs=4, max_runs=4, admit=self.admitted.append)
        programs = [
            ('def run():\n    import pricing\n    return {"observed": pricing.discounted_price(100)}\n', 'completed', 30),
            ('def run():\n    from pathlib import Path\n    Path("/workspace/pricing.py").write_text("changed")\n', 'execution_error', 30),
            ('def run():\n    return "x" * 200000\n', 'output_limit', 30),
            ('def run():\n    import time\n    time.sleep(60)\n', 'timeout', 1),
        ]
        before = (self.baseline / 'workspace/pricing.py').read_bytes()
        for code, status, timeout in programs:
            ref = workspace.propose_reproducer(code)['reproducer_ref']
            result = workspace.run_reproducer(ref, timeout_seconds=timeout, max_output_bytes=4096)
            self.assertEqual(result['status'], status, result)
            self.assertTrue(result['cleanup_ok'], result)
            self.assertLessEqual(result['captured_bytes'], 4096)
            self.assertEqual(result['trust'], 'model_program_output')
            if status == 'completed':
                self.assertEqual(result['observation'], {'observed': 100.0})
        self.assertEqual(before, (self.baseline / 'workspace/pricing.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
