"""Separate official-harness process; never imports or invokes an LLM client."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import sys

REQUESTS_NETWORK_POLICY = 'requests-isolated-timeout-v1'
NETWORK_POLICIES = ('none', 'bridge', REQUESTS_NETWORK_POLICY)
NETWORK_OWNER_LABEL = 'repair-workbench.owner-run'


def execution_policy(network_policy, instance_id):
    if network_policy not in NETWORK_POLICIES:
        raise ValueError('unsupported frozen network policy')
    policy = {'network': network_policy, 'cpus': 2, 'memory': '2g',
              'capabilities': 'dropped', 'auto_pull': False}
    if network_policy == REQUESTS_NETWORK_POLICY:
        if instance_id != 'psf__requests-3362':
            raise ValueError('requests-isolated-timeout-v1 only supports psf__requests-3362')
        policy['network_adapter'] = {'driver': 'bridge', 'internal': True,
            'subnet': '10.255.255.0/24', 'gateway': '10.255.255.254',
            'container_ip': '10.255.255.2', 'unassigned_timeout_target': '10.255.255.1',
            'scope': 'explicit environment adaptation; not the original official environment'}
    return policy


def network_plan(network_policy, instance_id, run_id):
    policy = execution_policy(network_policy, instance_id)
    if network_policy != REQUESTS_NETWORK_POLICY:
        return None
    if not re.fullmatch(r'repair-[a-f0-9]{32}', run_id):
        raise ValueError('invalid network owner run id')
    return {**policy['network_adapter'], 'name': 'repair-swe-net-' + run_id,
            'labels': {NETWORK_OWNER_LABEL: run_id,
                       'repair-workbench.network-policy': network_policy,
                       'repair-workbench.instance': instance_id}}


def prepare_network(client, plan, docker_types):
    """Create one isolated, labelled network; the parent owns final cleanup."""
    if plan is None:
        return None
    if client.networks.list(names=[plan['name']]):
        raise ValueError('run network already exists; refusing reuse')
    network = client.networks.create(plan['name'], driver=plan['driver'], internal=True,
        check_duplicate=True, labels=plan['labels'],
        ipam=docker_types.IPAMConfig(pool_configs=[docker_types.IPAMPool(
            subnet=plan['subnet'], gateway=plan['gateway'])]))
    network.reload()
    attrs = network.attrs
    actual = {key: attrs.get(key) for key in ('Id', 'Name', 'Driver', 'Internal', 'IPAM', 'Labels')}
    Path('network-policy.json').write_text(json.dumps({'requested': plan, 'actual': actual}, indent=2) + '\n')
    pools = (attrs.get('IPAM') or {}).get('Config', [])
    if (attrs.get('Name') != plan['name'] or attrs.get('Driver') != 'bridge'
            or attrs.get('Internal') is not True or attrs.get('Labels') != plan['labels']
            or len(pools) != 1 or pools[0].get('Subnet') != plan['subnet']
            or pools[0].get('Gateway') != plan['gateway']):
        raise ValueError('isolated network policy was not applied')
    return network


def verify_harness():
    lock = json.loads(Path(__file__).with_name('swebench_harness_lock.json').read_text())
    spec = importlib.util.find_spec('swebench')
    if spec is None or not spec.origin:
        raise ValueError('official harness is not installed in the selected interpreter')
    root = Path(spec.origin).parent
    actual = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob('*.py')}
    if actual != lock['python_files'] or importlib.metadata.version('swebench') != lock['version']:
        raise ValueError('official harness source/version drift')
    return lock


class HarnessClient:
    """Cache SDK collections: DockerClient properties return a new object each time."""
    def __init__(self, client):
        self._client = client
        self.containers = client.containers
        self.images = client.images

    def __getattr__(self, name):
        return getattr(self._client, name)


def parse_source_blobs(raw):
    blobs = {}
    for entry in raw.split(b'\0'):
        if not entry:continue
        header, path = entry.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('unsupported image Git entry')
        name = path.decode()
        if name in blobs:
            raise ValueError('duplicate image Git path')
        blobs[name] = oid
    return blobs


def run(payload):
    policy = execution_policy(payload['network_policy'], payload['record']['instance_id'])
    plan = network_plan(payload['network_policy'], payload['record']['instance_id'], payload['run_id'])
    lock = verify_harness()
    import docker
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.utils import make_test_spec
    client = HarnessClient(docker.from_env(timeout=60))
    network = prepare_network(client, plan, docker.types)
    original_create = client.containers.create

    def create(*args, **kwargs):
        # Execution restrictions do not change the upstream test script/parser
        # or scoring. Python track needs no browser/SYS_ADMIN privileges.
        kwargs.pop('cap_add', None)
        kwargs.pop('network', None)
        kwargs.pop('networking_config', None)
        kwargs.update(network_disabled=payload['network_policy'] == 'none',
                      network_mode=payload['network_policy'], cap_drop=['ALL'],
                      security_opt=['no-new-privileges'], mem_limit='2g',
                      nano_cpus=2_000_000_000, pids_limit=256)
        if network is not None:
            kwargs.pop('network_mode')
            kwargs.update(network=plan['name'], networking_config={plan['name']:
                client.api.create_endpoint_config(ipv4_address=plan['container_ip'])})
        container = original_create(*args, **kwargs)
        container.reload()
        config = container.attrs['HostConfig']
        actual = {'image_id': container.attrs['Image'], 'network': config['NetworkMode'],
                  'memory_bytes': config['Memory'], 'nano_cpus': config['NanoCpus'],
                  'pids_limit': config['PidsLimit'], 'cap_drop': config['CapDrop'],
                  'cap_add': config['CapAdd'], 'security_opt': config['SecurityOpt']}
        if plan is not None:
            actual['network_policy'] = payload['network_policy']
            actual['network_owner'] = plan['labels']
            actual['network_adapter'] = policy['network_adapter']
        Path('container-policy.json').write_text(json.dumps(actual, indent=2) + '\n')
        if (actual['image_id'] != payload['image_id']
                or actual['network'] != (plan['name'] if plan else payload['network_policy'])
                or actual['memory_bytes'] != 2 * 1024**3 or actual['nano_cpus'] != 2_000_000_000
                or actual['pids_limit'] != 256 or actual['cap_add']
                or 'ALL' not in (actual['cap_drop'] or [])):
            raise ValueError('container execution policy was not applied')
        original_start = container.start

        def checked_start():
            original_start()
            if plan is not None:
                container.reload()
                attached = container.attrs.get('NetworkSettings', {}).get('Networks', {})
                endpoint = attached.get(plan['name'], {})
                actual['network_endpoint'] = endpoint
                Path('container-policy.json').write_text(json.dumps(actual, indent=2) + '\n')
                if (set(attached) != {plan['name']} or endpoint.get('IPAddress') != plan['container_ip']
                        or endpoint.get('NetworkID') != network.id):
                    raise ValueError('container isolated endpoint was not applied')
            head = container.exec_run(['git', 'rev-parse', 'HEAD'], workdir='/testbed')
            listing = container.exec_run(['git', 'ls-tree', '-rz', 'HEAD'], workdir='/testbed')
            blobs = parse_source_blobs(listing.output)
            dirty = container.exec_run(['git', '-c', 'core.fileMode=false', 'diff', '--exit-code', 'HEAD'], workdir='/testbed')
            source_receipt = {'upstream_commit': payload['record']['base_commit'],
                'image_commit': head.output.decode().strip(), 'checked_files': len(blobs),
                'content_match': blobs == payload['base_git_blobs'], 'dirty_returncode': dirty.exit_code,
                'identity': 'all tracked paths and Git blob IDs; regular-file executable bits may differ'}
            Path('source-binding.json').write_text(json.dumps(source_receipt, indent=2) + '\n')
            if head.exit_code != 0 or listing.exit_code != 0 or dirty.exit_code != 0 or not source_receipt['content_match']:
                raise ValueError('official image source content does not match pinned base')

        container.start = checked_start
        return container

    def no_pull(*args, **kwargs):
        raise RuntimeError('adapter forbids automatic image pulls')

    client.containers.create = create
    client.images.pull = no_pull
    record = dict(payload['record'])
    record['image'] = payload['image_id']
    spec = make_test_spec(record)
    result = run_instance(spec, payload['prediction'], client, payload['run_id'],
                          payload['timeout'], skip_patch=payload['negative'])
    if result is None:
        return {'status': 'inconclusive', 'reason': 'official_report_missing'}
    instance_id, report = result
    item = report[instance_id]
    if not item.get('patch_successfully_applied'):
        status, reason = 'inconclusive', 'official_output_unusable'
    else:
        status = 'resolved' if item['resolved'] else 'unresolved'
        reason = 'official_get_eval_report'
    return {'status': status, 'reason': reason, 'official_report': report,
            'harness_commit': lock['commit'],
            'execution_policy': policy}


def main():
    output = Path.cwd() / 'worker-result.json'
    try:
        result = run(json.loads(Path(sys.argv[1]).read_text()))
    except Exception as exc:
        result = {'status': 'inconclusive', 'reason': 'official_worker_error',
                  'error_type': type(exc).__name__}
        # No provider/account environment is inherited by this process.
        import traceback
        traceback.print_exc()
    output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
