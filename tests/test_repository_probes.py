"""Public repository probe boundaries; synthetic receipts are not model evidence."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_agent import fixture
from test_workload_contract import case, contract
from repair_workbench import repository_probes as probes
from repair_workbench import repository_probe_worker as cpu_worker
from repair_workbench import repository_measure_worker as registry
from repair_workbench.evaluation_budget import _atomic_write
from repair_workbench.session import BudgetExceeded
from repair_workbench.snapshot import verify_snapshot


IMAGE = 'sha256:' + 'a' * 64
ENVIRONMENT = {'python': '3.13', 'implementation': 'CPython', 'system': 'Linux',
               'machine': 'aarch64', 'clock': 'perf_counter_ns'}


class RepositoryProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.baseline = fixture(self.root)
        data = contract()
        self.public = {key: data[key] for key in ('schema_version', 'task_id', 'repo', 'base_commit',
            'workload_id', 'workload_version', 'goal', 'thresholds', 'development_inputs')}
        self.public.update(workload_id='sql-comments-v3', base_commit=verify_snapshot(self.baseline)['commit'],
            development_inputs=[case('dev-small', {'size': 1, 'kind': 'line_comments'}),
                                case('dev-boundary', {'size': 2, 'kind': 'quoted_markers'}, 'guardrail')])
        self.public['semantic_contract'] = {key: data['semantics'][key] for key in
            ('strategy', 'baseline_behavior_confirmed', 'output_comparison', 'contract_reference')}
        self.calls = []

    def workspace(self, **kwargs):
        with patch.object(probes, 'resolve_image', return_value=IMAGE):
            return probes.RepositoryProbeWorkspace(self.baseline, self.root / 'public-probes',
                                                   'existing-local-image', self.public, **kwargs)

    def report(self, job):
        common = {'schema_version': 1, 'workload_id': job['workload_id'], 'probe': job['probe'],
                  'input_sha256': job['input_sha256'], 'output_sha256': ['1' * 64] * job['repeats'],
                  'output_lengths': [0] * job['repeats'], 'environment': ENVIRONMENT.copy()}
        if job['probe'] == 'cpu':
            return {**common, 'profiled_seconds': [.01] * job['repeats'], 'top_functions': [
                {'file': 'sqlparse/filters/others.py', 'line': n + 1, 'function': 'process', 'calls': 3,
                 'own_seconds': .001, 'cumulative_seconds': .002} for n in range(15)]}
        return {**common, 'samples': ([.01] if job['probe'] == 'benchmark' else [100]) * job['repeats']}

    def run_physical(self, snapshot, destination, image, job, **kwargs):
        self.calls.append(copy.deepcopy(job))
        self.assertEqual(image, IMAGE)
        self.assertEqual(kwargs['timeout_seconds'], 30)
        destination.mkdir()
        _atomic_write(destination / 'job.json', job, create=True)
        key = 'cpu_report' if job['probe'] == 'cpu' else 'measurement'
        result = {'status': 'ok', 'cleanup_ok': True, 'input_integrity_ok': True,
                  'snapshot_ref': probes.snapshot_id(verify_snapshot(snapshot)), 'image_id': image,
                  'adapter_sha256': probes._digest(registry.__file__), key: self.report(job)}
        _atomic_write(destination / 'result.json', result, create=True)
        return result

    def runners(self, execute=None):
        stack = ExitStack()
        stack.enter_context(patch.object(probes, '_run_cpu_probe', side_effect=execute or self.run_physical))
        stack.enter_context(patch.object(probes.repository_measurement, 'run_repository_measurement',
                                        side_effect=execute or self.run_physical))
        return stack

    def test_projection_rejects_final_checker_and_nested_extra_fields(self):
        for where, field in [('root', 'final_inputs'), ('root', 'checker_sha256'),
                             ('semantic', 'checker_id'), ('case', 'final')]:
            value = copy.deepcopy(self.public)
            target = value if where == 'root' else value['semantic_contract'] if where == 'semantic' else value['development_inputs'][0]
            target[field] = 'hidden'
            with self.subTest(where=where, field=field), self.assertRaises(ValueError):
                probes.validate_development_workload(value)
        value = copy.deepcopy(self.public)
        value['development_inputs'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'input'):
            probes.validate_development_workload(value)

    def test_cache_rechecks_admission_but_not_physical_quota_and_returns_copies(self):
        admitted = []
        workspace = self.workspace(max_runs=1, admit=admitted.append)
        with self.runners():
            first = workspace.run_probe('benchmark')
            first['measurement']['samples'][0] = 99
            second = workspace.run_probe('benchmark', 'dev-small')
            with self.assertRaisesRegex(ValueError, 'run_limit'):
                workspace.run_probe('memory')
        self.assertEqual(workspace.runs, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(admitted, ['run_probe'] * 3)
        self.assertTrue(first['dispatched'])
        self.assertTrue(second['cache_hit'])
        self.assertFalse(second['dispatched'])
        self.assertEqual(second['measurement']['samples'][0], .01)
        self.assertEqual(first['report_ref'], second['report_ref'])

    def test_three_probes_bind_only_public_cases_and_cpu_report_is_paged(self):
        workspace = self.workspace()
        with self.runners():
            rows = [workspace.run_probe(name, 'dev-boundary') for name in probes.PROBES]
        self.assertEqual(workspace.runs, 3)
        self.assertTrue(all(row['status'] == 'ok' and row['cleanup_ok'] and row['input_integrity_ok'] for row in rows))
        self.assertEqual({job['input']['size'] for job in self.calls}, {2})
        self.assertTrue(all(set(job) == {'workload_id', 'input', 'input_sha256', 'probe', 'warmups',
            'repeats', 'batch_iterations', 'gc_policy', 'warmup_seconds'} for job in self.calls))
        cpu = rows[0]
        self.assertEqual(len(cpu['summary']['top_functions']), 3)
        page = workspace.read_report(cpu['report_ref'], 'top_functions', offset=5, limit=5)
        self.assertEqual(len(page['items']), 5)
        self.assertEqual(page['next_offset'], 10)
        self.assertEqual(page['report_sha256'], cpu['report_sha256'])
        self.assertLessEqual(len(probes.canonical_json(page).encode()), 8192)

    def test_unknown_probe_case_and_report_never_dispatch(self):
        workspace = self.workspace()
        with self.runners():
            for probe, case_id in [('shell', None), ('cpu', 'final-secret'), ('memory', '../final')]:
                with self.subTest(probe=probe, case_id=case_id), self.assertRaises(ValueError):
                    workspace.run_probe(probe, case_id)
            with self.assertRaises(ValueError):
                workspace.read_report('../result.json')
        self.assertEqual(workspace.runs, 0)
        self.assertFalse(self.calls)

    def test_snapshot_drift_after_dispatch_blocks_and_is_never_cached(self):
        workspace = self.workspace()
        def changed(*args, **kwargs):
            result = self.run_physical(*args, **kwargs)
            (self.baseline / 'workspace/pricing.py').write_text('# drift\n')
            return result
        with self.runners(changed):
            result = workspace.run_probe('benchmark')
            with self.assertRaises(ValueError): workspace.run_probe('benchmark')
        self.assertEqual(result['status'], 'input_changed')
        self.assertEqual(workspace.blocked_reason, 'repository_probe_inputs_changed')
        self.assertEqual(workspace.cache, {})
        self.assertTrue(workspace.cleanup_ok)

    def test_manifest_metadata_and_sampling_are_fenced_even_on_cache_hits(self):
        workspace = self.workspace()
        with self.runners(): workspace.run_probe('memory')
        path = self.baseline / 'manifest.json'
        value = json.loads(path.read_text()); value['new_claim'] = 'same source bytes'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): workspace.run_probe('memory')
        self.assertEqual(workspace.blocked_reason, 'repository_probe_inputs_changed')

    def test_mutated_sampling_is_not_silently_a_new_public_protocol(self):
        workspace = self.workspace()
        workspace.sampling['repeats'] = 9
        with self.assertRaises(ValueError): workspace.run_probe('cpu')
        self.assertEqual(workspace.runs, 0)

    def test_report_tampering_blocks_cached_return(self):
        workspace = self.workspace()
        with self.runners(): row = workspace.run_probe('benchmark')
        path = workspace.output / (row['report_ref'] + '.json')
        path.write_text('{}')
        with self.assertRaises(ValueError): workspace.run_probe('benchmark')
        self.assertEqual(workspace.blocked_reason, 'repository_probe_report_changed')

    def test_failed_or_unclean_physical_observations_permanently_block(self):
        workspace = self.workspace()
        def unclean(*args, **kwargs):
            return {**self.run_physical(*args, **kwargs), 'cleanup_ok': False}
        with self.runners(unclean): result = workspace.run_probe('cpu')
        self.assertEqual(result['status'], 'cleanup_unconfirmed')
        self.assertFalse(workspace.cleanup_ok)
        self.assertEqual(workspace.cache, {})
        with self.assertRaises(ValueError): workspace.run_probe('cpu')

    def test_clean_timeout_is_failed_not_cached(self):
        workspace = self.workspace()
        def timeout(*args, **kwargs):
            return {**self.run_physical(*args, **kwargs), 'status': 'timeout'}
        with self.runners(timeout): result = workspace.run_probe('benchmark')
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(workspace.cleanup_ok)
        self.assertEqual(workspace.blocked_reason, 'repository_probe_execution_failed')
        self.assertFalse(workspace.cache)

    def test_cancel_recovers_actual_cleanup_receipt_and_propagates(self):
        workspace = self.workspace()
        def cancelled(*args, **kwargs):
            self.run_physical(*args, **kwargs)
            raise BudgetExceeded('cancel_requested')
        with self.runners(cancelled), self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            workspace.run_probe('memory')
        self.assertTrue(workspace.cleanup_ok)
        self.assertEqual(workspace.blocked_reason, 'repository_probe_stopped')
        self.assertFalse(workspace.cache)

    def test_interrupted_cleanup_without_receipt_stays_unknown(self):
        workspace = self.workspace()
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        with self.runners(interrupted), self.assertRaises(KeyboardInterrupt): workspace.run_probe('cpu')
        self.assertFalse(workspace.cleanup_ok)
        saved = json.loads((workspace.output / 'workspace.json').read_text())
        self.assertFalse(saved['cleanup_ok'])
        self.assertEqual(saved['blocked_reason'], 'repository_probe_cleanup_unconfirmed')

    def test_nondeterministic_or_absolute_path_cpu_reports_are_rejected(self):
        workspace = self.workspace()
        def changed(*args, **kwargs):
            result = self.run_physical(*args, **kwargs)
            result['cpu_report']['top_functions'][0]['file'] = '/host/secret.py'
            return result
        with self.runners(changed): result = workspace.run_probe('cpu')
        self.assertNotEqual(result['status'], 'ok')
        self.assertFalse(workspace.cache)

    def test_registered_cpu_worker_profiles_only_fixed_operation_and_keeps_old_api(self):
        job = {'workload_id': 'sql-comments-v3', 'input': {'size': 1, 'kind': 'line_comments'},
               'input_sha256': probes.input_sha256({'size': 1, 'kind': 'line_comments'}), 'probe': 'cpu',
               'warmups': 1, 'repeats': 3, 'batch_iterations': 2, 'gc_policy': 'collect_before_sample',
               'warmup_seconds': 0}
        invocations = []
        def operation():
            invocations.append(1)
            return ['same output']
        registered = SimpleNamespace(validate_job=registry.validate_job, canonical=registry.canonical,
                                     _operation=lambda received, root: operation)
        with patch.object(cpu_worker, 'load_registered_registry', return_value=registered):
            report = cpu_worker.execute_registered_cpu(job, self.baseline / 'workspace')
        probes.validate_cpu_report(report, job)
        self.assertEqual(len(invocations), 7)
        self.assertEqual(len(report['profiled_seconds']), 3)
        self.assertLessEqual(len(report['top_functions']), 15)
        self.assertTrue(all(not Path(row['file']).is_absolute() for row in report['top_functions']))
        self.assertTrue(callable(cpu_worker.execute))

    def test_cpu_container_mounts_current_job_registry_and_source_only(self):
        workspace = self.workspace()
        case = self.public['development_inputs'][0]
        job = {'workload_id': self.public['workload_id'], 'input': case['input'], 'input_sha256': case['sha256'],
               'probe': 'cpu', **{k: v for k, v in workspace.sampling.items() if k not in ('timeout_seconds', 'cpu_affinity')}}
        report = self.report(job)
        original = subprocess.Popen
        commands = []
        def launch(command, **kwargs):
            commands.append(command)
            return original([sys.executable, '-c', 'import json; print(json.dumps(' + repr(report) + '))'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        def remove(command, **kwargs):
            return subprocess.CompletedProcess(command, 1 if command[1] == 'inspect' else 0, '', 'No such object')
        with patch.object(probes.subprocess, 'Popen', side_effect=launch), \
             patch.object(probes.subprocess, 'run', side_effect=remove):
            result = probes._run_cpu_probe(self.baseline, self.root / 'cpu-only', IMAGE, job,
                                           timeout_seconds=30, execution_control=None)
        self.assertEqual(result['status'], 'ok', result)
        command = commands[0]
        mounts = [command[n + 1] for n, item in enumerate(command) if item == '--mount']
        self.assertEqual(len(mounts), 4)
        self.assertTrue(all(value.endswith(',readonly') for value in mounts))
        self.assertTrue(any('dst=/trusted/registry.py,' in value for value in mounts))
        self.assertFalse(any('final' in value or 'contract.json' in value for value in mounts))
        self.assertEqual(command[command.index('--pids-limit') + 1], '64')
        self.assertEqual(command[command.index('--network') + 1], 'none')


if __name__ == '__main__':
    unittest.main()
