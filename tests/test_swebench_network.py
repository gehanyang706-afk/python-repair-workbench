"""Isolated timeout networking is explicit, task-specific and owner-cleaned."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from test_swebench_adapter import fixture_module
from repair_workbench.swebench_adapter import (
    cleanup_run_network, evaluate, import_dataset, prepare_workspace, wait_worker)
from repair_workbench.session import BudgetExceeded
from repair_workbench.swebench_worker import (
    NETWORK_OWNER_LABEL, REQUESTS_NETWORK_POLICY, execution_policy, network_plan,
    prepare_network, run)


class IsolatedNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_id = 'repair-' + 'a' * 32
        self.plan = network_plan(REQUESTS_NETWORK_POLICY, 'psf__requests-3362', self.run_id)

    def test_policy_is_explicit_task_scoped_and_names_are_not_model_selected(self):
        for policy in ('none', 'bridge'):
            self.assertIsNone(network_plan(policy, 'any__repo-1', self.run_id))
            self.assertEqual(execution_policy(policy, 'any__repo-1')['network'], policy)
        self.assertEqual(self.plan['name'], 'repair-swe-net-' + self.run_id)
        self.assertEqual(self.plan['labels'][NETWORK_OWNER_LABEL], self.run_id)
        with self.assertRaisesRegex(ValueError, 'only supports'):
            network_plan(REQUESTS_NETWORK_POLICY, 'workbench__fixture-1', self.run_id)
        with self.assertRaisesRegex(ValueError, 'owner run id'):
            network_plan(REQUESTS_NETWORK_POLICY, 'psf__requests-3362', '../arbitrary')

    def worker_client(self, *, internal=True, endpoint_ip='10.255.255.2'):
        client = MagicMock()
        client.networks.list.return_value = []
        network = MagicMock()
        network.id = 'b' * 64
        network.attrs = {'Id': network.id, 'Name': self.plan['name'], 'Driver': 'bridge',
                         'Internal': internal, 'Labels': self.plan['labels'],
                         'IPAM': {'Config': [{'Subnet': '10.255.255.0/24', 'Gateway': '10.255.255.254'}]}}
        client.networks.create.return_value = network
        client.api.create_endpoint_config.side_effect = lambda **kwargs: kwargs
        container = MagicMock()
        container.attrs = {'Image': 'sha256:' + 'c' * 64,
            'HostConfig': {'NetworkMode': self.plan['name'], 'Memory': 2 * 1024**3,
                'NanoCpus': 2_000_000_000, 'PidsLimit': 256, 'CapDrop': ['ALL'],
                'CapAdd': None, 'SecurityOpt': ['no-new-privileges']},
            'NetworkSettings': {'Networks': {self.plan['name']: {
                'IPAddress': endpoint_ip, 'NetworkID': network.id}}}}
        client.containers.create.return_value = container
        container.exec_run.side_effect = [
            types.SimpleNamespace(output=b'base\n', exit_code=0),
            types.SimpleNamespace(output=b'100644 blob ' + b'd' * 40 + b'\tmodule.py\0', exit_code=0),
            types.SimpleNamespace(output=b'', exit_code=0)]
        return client, container

    def worker_run(self, client):
        types_stub = types.SimpleNamespace(IPAMConfig=lambda **kw: kw, IPAMPool=lambda **kw: kw)
        docker_stub = types.SimpleNamespace(from_env=lambda **kw: client, types=types_stub)
        row = {'instance_id': 'psf__requests-3362', 'base_commit': 'e' * 40,
               'image': 'old', 'eval_script': 'ORIGINAL_TEST_SCRIPT'}
        payload = {'record': row, 'prediction': {'model_patch': 'UNCHANGED'},
            'image_id': 'sha256:' + 'c' * 64, 'base_git_blobs': {'module.py': 'd' * 40},
            'network_policy': REQUESTS_NETWORK_POLICY, 'run_id': self.run_id,
            'timeout': 5, 'negative': False}
        def official(spec, prediction, sdk, run_id, timeout, **kwargs):
            self.assertEqual(spec['eval_script'], 'ORIGINAL_TEST_SCRIPT')
            container = sdk.containers.create(image=payload['image_id'], network_mode='none', cap_add=['SYS_ADMIN'])
            container.start()
            return 'psf__requests-3362', {'psf__requests-3362': {
                'patch_successfully_applied': True, 'resolved': True}}
        modules = {'docker': docker_stub,
                   'swebench.harness.run_evaluation': types.SimpleNamespace(run_instance=official),
                   'swebench.harness.utils': types.SimpleNamespace(make_test_spec=lambda row: row)}
        old = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(sys.modules, modules), patch('repair_workbench.swebench_worker.verify_harness',
                                                        return_value={'commit': 'test'}):
                return run(payload)
        finally:
            os.chdir(old)

    def test_worker_creates_internal_ipam_network_and_pins_only_container_endpoint(self):
        client, container = self.worker_client()
        original_create = client.containers.create
        result = self.worker_run(client)
        self.assertEqual(result['status'], 'resolved')
        kwargs = client.networks.create.call_args.kwargs
        self.assertTrue(kwargs['internal'])
        self.assertEqual(kwargs['labels'], self.plan['labels'])
        self.assertEqual(kwargs['ipam']['pool_configs'], [
            {'subnet': '10.255.255.0/24', 'gateway': '10.255.255.254'}])
        creation = original_create.call_args.kwargs
        self.assertNotIn('network_mode', creation)
        self.assertEqual(creation['network'], self.plan['name'])
        self.assertEqual(creation['networking_config'], {self.plan['name']: {'ipv4_address': '10.255.255.2'}})
        self.assertEqual(creation['cap_drop'], ['ALL'])
        self.assertNotIn('cap_add', creation)
        self.assertFalse(creation['network_disabled'])
        actual = json.loads((self.root / 'container-policy.json').read_text())
        self.assertEqual(actual['network_endpoint']['IPAddress'], '10.255.255.2')
        self.assertEqual(actual['network_owner'], self.plan['labels'])
        self.assertIn('not the original official environment', result['execution_policy']['network_adapter']['scope'])

    def test_existing_network_is_never_reused(self):
        client, _ = self.worker_client()
        client.networks.list.return_value = [object()]
        with self.assertRaisesRegex(ValueError, 'refusing reuse'):
            prepare_network(client, self.plan, MagicMock())
        client.networks.create.assert_not_called()

    def test_worker_rejects_unapplied_internal_policy_or_wrong_ip(self):
        for internal, ip in [(False, '10.255.255.2'), (True, '10.255.255.9')]:
            with self.subTest(internal=internal, ip=ip):
                client, container = self.worker_client(internal=internal, endpoint_ip=ip)
                with self.assertRaisesRegex(ValueError, 'not applied'):
                    self.worker_run(client)
                container.exec_run.assert_not_called()

    def prepare_requests_fixture(self):
        row = fixture_module.prepare_fixture(self.root / 'fixture')
        row.update(instance_id='psf__requests-3362', repo='psf/requests')
        source = self.root / 'records.json'
        source.write_text(json.dumps([row]))
        dataset = self.root / 'dataset'
        import_dataset(source, dataset, dataset_name='fixture', revision='1' * 40)
        workspace = self.root / 'workspace'
        prepare_workspace(dataset, row['instance_id'], self.root / 'fixture/repo', workspace)
        return row, workspace

    def parent_run(self, *, wrong_owner=False, removal_failure=False, container_failure=False,
                   execution_control=None):
        row, workspace = self.prepare_requests_fixture()
        state = {'container': True, 'network': True}
        calls = []
        actual_run = subprocess.run
        output = self.root / 'evaluation'
        process = MagicMock()
        process.poll.return_value = 0
        def start(*args, **kwargs):
            payload = json.loads((output / 'evaluator-input.json').read_text())
            state['plan'] = network_plan(REQUESTS_NETWORK_POLICY, row['instance_id'], payload['run_id'])
            (output / 'worker-result.json').write_text(json.dumps({'status': 'resolved', 'reason': 'official_get_eval_report'}))
            return process
        def command(args, **kwargs):
            if args[0] != 'docker':
                return actual_run(args, **kwargs)
            calls.append(args)
            if args[1] == 'inspect':
                return subprocess.CompletedProcess(args, 0 if state['container'] else 1, '[]',
                    '' if state['container'] else 'Error: No such object: container')
            if args[1] == 'rm':
                state['container'] = container_failure
                return subprocess.CompletedProcess(args, int(container_failure), '', '')
            if args[1:3] == ['network', 'inspect']:
                plan = state['plan']
                labels = {**plan['labels'], NETWORK_OWNER_LABEL: 'another-run'} if wrong_owner else plan['labels']
                data = [{'Id': 'f' * 64, 'Name': plan['name'], 'Labels': labels}]
                return subprocess.CompletedProcess(args, 0 if state['network'] else 1,
                    json.dumps(data), '' if state['network'] else f'Error: network {plan["name"]} not found')
            if args[1:3] == ['network', 'rm']:
                self.assertEqual(args[-1], 'f' * 64)
                state['network'] = removal_failure
                return subprocess.CompletedProcess(args, int(removal_failure), '', '')
            raise AssertionError(args)
        with patch('repair_workbench.swebench_adapter.image_identity', return_value='sha256:' + 'c' * 64), \
             patch('repair_workbench.swebench_adapter.isolated_env', return_value={}), \
             patch('repair_workbench.swebench_adapter.start_worker', side_effect=start), \
             patch('repair_workbench.swebench_adapter.subprocess.run', side_effect=command):
            result = evaluate(row, {'instance_id': row['instance_id'], 'model_name_or_path': 'test',
                                    'model_patch': row['patch']}, output, sys.executable,
                              workspace=workspace, network_policy=REQUESTS_NETWORK_POLICY,
                              execution_control=execution_control)
        return result, calls

    def test_parent_removes_container_then_owned_network_and_confirms_absence(self):
        result, calls = self.parent_run()
        self.assertTrue(result['cleanup_ok'])
        self.assertTrue(result['network_cleanup_ok'])
        self.assertEqual(result['status'], 'resolved')
        container_rm = next(i for i, args in enumerate(calls) if args[1] == 'rm')
        network_rm = next(i for i, args in enumerate(calls) if args[1:3] == ['network', 'rm'])
        self.assertLess(container_rm, network_rm)

    def test_foreign_network_is_not_removed_and_blocks_success(self):
        result, calls = self.parent_run(wrong_owner=True)
        self.assertFalse(result['cleanup_ok'])
        self.assertEqual(result['reason'], 'network_cleanup_unconfirmed')
        self.assertFalse(any(args[1:3] == ['network', 'rm'] for args in calls))

    def test_network_removal_failure_is_inconclusive(self):
        result, _ = self.parent_run(removal_failure=True)
        self.assertFalse(result['cleanup_ok'])
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['reason'], 'network_cleanup_unconfirmed')

    def test_container_removal_failure_prevents_network_removal(self):
        result, calls = self.parent_run(container_failure=True)
        self.assertFalse(result['cleanup_ok'])
        self.assertEqual(result['reason'], 'container_cleanup_unconfirmed')
        self.assertFalse(any(args[1:3] == ['network', 'rm'] for args in calls))

    def test_network_daemon_failure_is_not_mistaken_for_absence(self):
        failed = subprocess.CompletedProcess([], 1, '', 'Cannot connect to the Docker daemon')
        with patch('repair_workbench.swebench_adapter.subprocess.run', return_value=failed):
            self.assertFalse(cleanup_run_network(self.plan)['cleanup_ok'])

    def test_controlled_wait_checks_budget_between_short_waits(self):
        process, control = MagicMock(), MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired('worker', 0.25), 0]
        self.assertEqual(wait_worker(process, 600, control), 0)
        self.assertEqual(control.check.call_count, 3)
        self.assertTrue(all(call.kwargs['timeout'] <= 0.25 for call in process.wait.call_args_list))
        process.wait.reset_mock(side_effect=True)
        wait_worker(process, 600)
        process.wait.assert_called_once_with(timeout=600)

    def test_controlled_wait_preserves_harness_wall_timeout(self):
        with patch('repair_workbench.swebench_adapter.time.monotonic', side_effect=[0, 601]):
            with self.assertRaises(subprocess.TimeoutExpired):
                wait_worker(MagicMock(), 600, MagicMock())

    def test_budget_cancel_records_result_and_cleans_before_propagating(self):
        control = MagicMock()
        control.check.side_effect = [None, BudgetExceeded('batch_cancel_requested')]
        with self.assertRaisesRegex(BudgetExceeded, 'batch_cancel_requested'):
            self.parent_run(execution_control=control)
        result = json.loads((self.root / 'evaluation/result.json').read_text())
        self.assertEqual(result['reason'], 'batch_cancel_requested')
        self.assertTrue(result['container_cleanup_ok'])
        self.assertTrue(result['network_cleanup_ok'])
        self.assertTrue(result['cleanup_ok'])
        with patch('repair_workbench.swebench_adapter.subprocess.run', side_effect=subprocess.TimeoutExpired('docker', 15)):
            self.assertFalse(cleanup_run_network(self.plan)['cleanup_ok'])

    def test_adapter_rejects_other_task_before_preflight_or_output(self):
        row = fixture_module.prepare_fixture(self.root / 'fixture')
        with patch('repair_workbench.swebench_adapter.image_identity') as image:
            with self.assertRaisesRegex(ValueError, 'only supports'):
                evaluate(row, {}, self.root / 'rejected', sys.executable, network_policy=REQUESTS_NETWORK_POLICY)
        image.assert_not_called()
        self.assertFalse((self.root / 'rejected').exists())


if __name__ == '__main__':
    unittest.main()
