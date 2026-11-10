"""Independently rebuild and score a frozen repository performance candidate.

Only the trusted final evaluator calls this module. It never accepts a model's
test or performance receipts, imports target code on the host, or returns hidden
feedback to an active Agent. Controlled-project isolation is not adversarial
attestation: Python under test can observe the input it is asked to process.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import time

from . import (candidate as candidate_module, performance_scoring, repository_measure_worker,
               repository_measurement, runner, snapshot as snapshot_module, test_receipt_worker,
               workload_contract, reproduction, evaluation_budget)
from .candidate import create_candidate
from .evaluation_budget import _atomic_write
from .performance_scoring import score_final_measurements
from .repository_measurement import run_repository_measurement, validate_measurement
from .runner import run_tests
from .session import BudgetExceeded
from .snapshot import git, snapshot_id, verify_snapshot
from .workload_contract import WorkloadSpec, canonical_json


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def registered_implementations():
    """Application registry, never loaded from an Agent-provided contract."""
    adapter = _digest(repository_measure_worker.__file__)
    return ({key: adapter for key in ('sql-format-v2', 'sql-comments-v3', 'iterator-slice-v2')},
            {'canonical-output-v1': _digest(performance_scoring.__file__)})


def original_suite_sha256(manifest):
    """Bind original tests and their configuration, excluding editable library code."""
    entries = []
    for item in manifest['files']:
        path = PurePosixPath(item['path'])
        if (path.parts[0] in ('tests', 'test') or path.name == 'conftest.py'
                or path.name in ('pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml')
                or path.name.startswith('test_') or path.name.endswith('_test.py')):
            entries.append({'path': item['path'], 'sha256': item['sha256']})
    if not entries:
        raise ValueError('original_test_suite_required')
    return hashlib.sha256(canonical_json(sorted(entries, key=lambda v: v['path'])).encode()).hexdigest()


def _commit_matches(base, expected):
    if base['commit'] == expected:
        return True
    # An archive-based source preparer may have a different local Git commit.
    # Its trusted upstream alias must retain the same complete Git tree. This
    # checks the local tree binding; upstream authenticity remains the source
    # preparer's responsibility, not a claim proved by manifest strings.
    return (base.get('upstream_commit') == expected and base.get('upstream_tree')
            == git(Path(base['source_repo']), 'rev-parse', base['commit'] + '^{tree}').decode().strip())


def _supported(spec):
    if type(spec) is not WorkloadSpec:
        raise ValueError('frozen_workload_spec_required')
    data = spec.as_dict()
    adapters, checkers = registered_implementations()
    # Revalidate against the actual installed implementations, not just a
    # registry that happened to be supplied when the contract was constructed.
    WorkloadSpec(data, adapters=adapters, checkers=checkers)
    m, s = data['measurement'], data['semantics']
    if (m['cpus'] != 1 or m['memory_limit_bytes'] != 268435456
            or m['memory_metric'] != 'python_traced_peak_bytes'
            or m['warmups'] > 3 or m['repeats'] > 9 or m['timeout_seconds'] > 120
            or s['strategy'] != 'baseline_equivalence' or s['output_comparison'] != 'canonical_json'
            or s['metamorphic_checks']):
        raise ValueError('workload_contract_not_supported_by_repository_runner')
    for case in data['final_inputs']:
        repository_measure_worker.validate_job(_job(data, case, 'benchmark'))
    return data


def _job(data, case, probe):
    m = data['measurement']
    return {'workload_id': data['workload_id'], 'input': case['input'],
            'input_sha256': case['sha256'], 'probe': probe,
            'warmups': m['warmups'], 'repeats': m['repeats'],
            'batch_iterations': m.get('batch_iterations_by_case', {}).get(
                case['case_id'], m.get('batch_iterations', 1)),
            'gc_policy': m.get('gc_policy', 'unmodified'),
            'warmup_seconds': m.get('warmup_seconds', 0)}


class _Control:
    """Check-compatible polling plus ownership bookkeeping for the test runner."""
    def __init__(self, parent, record):
        self.parent, self.record = parent, record

    def check(self):
        if self.parent is not None:
            self.parent.check()

    def container(self, name, state):
        self.record['containers'][name] = state
        if self.parent is not None and hasattr(self.parent, 'container'):
            self.parent.container(name, state)


def verify_repository_candidate(baseline, candidate, destination, allowed_paths, spec, *,
                                execution_control=None, test_pids_limit=64):
    """Run original tests then final-data ABBA, once, outside the Agent workspace.

    Caller owns the contract, edit allowlist, process boundary and any outer
    batch deadline. A batch should pass its cheap execution-control view for
    polling and retain full batch checks before/after this final stage. Each
    physical run here has its own frozen timeout; cleanup/input drift stops the
    remaining schedule. All original snapshots and prior reports stay intact.
    """
    data = _supported(spec)
    if type(test_pids_limit) is not int or not 64 <= test_pids_limit <= 256:
        raise ValueError('invalid_original_test_pids_limit')
    baseline, candidate = Path(baseline).resolve(strict=True), Path(candidate).resolve(strict=True)
    destination = Path(destination).resolve()
    base, proposed = verify_snapshot(baseline), verify_snapshot(candidate)
    base_ref, proposed_ref = snapshot_id(base), snapshot_id(proposed)
    if not _commit_matches(base, data['base_commit']) or proposed.get('base_snapshot_id') != base_ref:
        raise ValueError('candidate_or_contract_baseline_mismatch')
    if original_suite_sha256(base) != data['original_tests']['suite_sha256']:
        raise ValueError('frozen_original_suite_mismatch')
    roots = (baseline, candidate, Path(base['source_repo']).resolve())
    if any(destination == p or p in destination.parents for p in roots):
        raise ValueError('final_output_must_be_outside_source_and_candidate')
    if destination.exists():
        raise FileExistsError('cannot_overwrite_final_evaluation')
    before = {v['path']: v['sha256'] for v in base['files']}
    after = {v['path']: v['sha256'] for v in proposed['files']}
    if before.keys() != after.keys():
        raise ValueError('candidate_cannot_add_or_remove_files')
    changes = {name: (candidate / 'workspace' / name).read_bytes().decode('utf-8')
               for name in before if before[name] != after[name]}
    for path in changes:
        candidate_module.validate_source_path(path, allowed_paths)
    implementation = {str(Path(m.__file__).resolve()): _digest(m.__file__) for m in
        (candidate_module, performance_scoring, repository_measure_worker, repository_measurement,
         runner, snapshot_module, test_receipt_worker, workload_contract, reproduction, evaluation_budget)}
    implementation[str(Path(__file__).resolve())] = _digest(__file__)
    manifests = {baseline / 'manifest.json': _digest(baseline / 'manifest.json'),
                 candidate / 'manifest.json': _digest(candidate / 'manifest.json')}
    destination.mkdir(parents=True, mode=0o700)
    spec.write_frozen(destination / 'contract.json')
    manifests[destination / 'contract.json'] = _digest(destination / 'contract.json')
    schedule = [{'case_id': case['case_id'], 'input_sha256': case['sha256'], 'probe': probe,
                 'position': position, 'role': role, 'state': 'not_run'}
                for case in data['final_inputs'] for probe in ('benchmark', 'memory')
                for position, role in enumerate(data['measurement']['order'])]
    record = {'schema_version': 1, 'contract_sha256': spec.sha256, 'status': 'inconclusive',
              'reason': 'evaluation_incomplete', 'baseline_ref': base_ref,
              'submitted_ref': proposed_ref, 'candidate_ref': None,
              'changed_paths': sorted(changes), 'containers': {}, 'tests': {}, 'runs': [],
              'measurements': [], 'implementation_sha256': implementation,
              'test_pids_limit': test_pids_limit, 'real_model_calls': 0,
              'planned_measurement_runs': 8 * len(data['final_inputs']),
              'schedule': schedule,
              'scope': 'Frozen repository workload acceptance; not model ability or adversarial attestation.'}
    control = _Control(execution_control, record)

    def save(status, reason, **details):
        if reason != 'evaluation_incomplete':
            for row in schedule:
                if row['state'] == 'running':
                    row['state'] = 'stopped' if status == 'stopped' else 'error'
        record.update(status=status, reason=reason, **details)
        _atomic_write(destination / 'result.json', record)
        return record

    def fence():
        control.check()
        if (snapshot_id(verify_snapshot(baseline)) != base_ref
                or snapshot_id(verify_snapshot(candidate)) != proposed_ref
                or any(_digest(p) != digest for p, digest in manifests.items())
                or any(_digest(p) != digest for p, digest in implementation.items())):
            raise ValueError('final_evaluation_inputs_or_implementation_changed')
        if record['candidate_ref'] is not None:
            if (snapshot_id(verify_snapshot(destination / 'candidate')) != record['candidate_ref']
                    or snapshot_id(verify_snapshot(destination / 'baseline')) != base_ref):
                raise ValueError('final_rebuilt_snapshot_changed')

    try:
        fence()
        rebuilt = create_candidate(baseline, destination / 'candidate', changes, allowed_paths)
        record['candidate_ref'] = snapshot_id(rebuilt)
        if record['candidate_ref'] != proposed_ref:
            return save('inconclusive', 'independent_reconstruction_mismatch')
        # Copy only trusted source+manifest; never reuse the submitted reports,
        # dependencies, scripts, or claims about correctness/performance.
        original = destination / 'baseline'
        original.mkdir()
        shutil.copytree(baseline / 'workspace', original / 'workspace')
        _atomic_write(original / 'manifest.json', base, create=True)
        for snapshot in (original, destination / 'candidate'):
            manifests[snapshot / 'manifest.json'] = _digest(snapshot / 'manifest.json')
        for role in ('baseline', 'candidate'):
            fence()
            snapshot = destination / role
            result = run_tests(snapshot, data['measurement']['image_id'],
                data['measurement']['timeout_seconds'], execution_control=control,
                collect_nodeids=True, test_pids_limit=test_pids_limit)
            record['tests'][role] = result
            fence()
            expected = base_ref if role == 'baseline' else record['candidate_ref']
            if (result.get('cleanup_ok') is not True or result.get('snapshot_id') != expected
                    or result.get('image_id') != data['measurement']['image_id']):
                return save('inconclusive', role + '_test_cleanup_or_binding_unconfirmed')
            inventory = result.get('test_inventory')
            if result.get('status') not in ('tests_passed', 'tests_failed') or type(inventory) is not dict:
                return save('inconclusive', role + '_test_execution_unusable')
            outcome = spec.original_test_result(inventory['outcomes'])
            if not outcome['accepted']:
                return save('inconclusive' if role == 'baseline' else 'failed',
                            role + '_original_tests_rejected', original_tests=outcome)
            if result['status'] != 'tests_passed':
                return save('inconclusive', role + '_test_status_disagrees_with_inventory')
        receipt = {'contract_sha256': spec.sha256, 'snapshot_ref': record['candidate_ref'],
                   'image_id': data['measurement']['image_id'],
                   'suite_sha256': data['original_tests']['suite_sha256'], 'cleanup_ok': True,
                   'producer': 'independent_original_test_runner',
                   'outcomes': record['tests']['candidate']['test_inventory']['outcomes']}
        _atomic_write(destination / 'original-test-receipt.json', receipt, create=True)
        execution = {k: v for k, v in data['measurement'].items() if k != 'order'}
        execution['adapter_sha256'] = data['adapter_sha256']
        for case in data['final_inputs']:
            for probe in ('benchmark', 'memory'):
                for position, role in enumerate(data['measurement']['order']):
                    fence()
                    job = _job(data, case, probe)
                    run_path = destination / 'measurements' / f'{case["case_id"]}-{probe}-{position}-{role}'
                    entry = schedule[len(record['runs'])]
                    entry.update(state='running', artifact=str(run_path.relative_to(destination)))
                    save('inconclusive', 'evaluation_incomplete')
                    result = run_repository_measurement(destination / role, run_path,
                        data['measurement']['image_id'], job,
                        timeout_seconds=data['measurement']['timeout_seconds'], execution_control=control,
                        cpu_affinity=data['measurement'].get('cpu_affinity'))
                    record['runs'].append({'case_id': case['case_id'], 'probe': probe,
                                           'position': position, 'role': role, 'result': result})
                    entry.update(state='completed' if result.get('status') == 'ok' else 'inconclusive',
                                 physical_status=result.get('status'))
                    fence()
                    if (result.get('status') != 'ok' or result.get('cleanup_ok') is not True
                            or result.get('input_integrity_ok') is not True
                            or result.get('snapshot_ref') != (base_ref if role == 'baseline' else record['candidate_ref'])
                            or result.get('image_id') != data['measurement']['image_id']
                            or result.get('adapter_sha256') != data['adapter_sha256']):
                        return save('inconclusive', 'measurement_execution_or_binding_unconfirmed')
                    report = validate_measurement(result['measurement'], job)
                    outputs = set(zip(report['output_sha256'], report['output_lengths']))
                    if len(outputs) != 1:
                        return save('inconclusive' if role == 'baseline' else 'failed',
                                    role + '_output_not_deterministic')
                    digest, length = outputs.pop()
                    record['measurements'].append({'contract_sha256': spec.sha256,
                        'snapshot_ref': result['snapshot_ref'], 'role': role, 'probe': probe,
                        'position': position, 'case_id': case['case_id'], 'input_sha256': case['sha256'],
                        'execution': execution, 'environment': report['environment'], 'cleanup_ok': True,
                        'producer': 'independent_registered_workload_runner', 'samples': report['samples'],
                        'output_sha256': digest, 'output_length': length})
                    save('inconclusive', 'evaluation_incomplete')
        fence()
        verdict = score_final_measurements(spec, baseline_ref=base_ref,
            candidate_ref=record['candidate_ref'], original_test_receipt=receipt,
            measurements=record['measurements'])
        fence()
        return save(verdict['status'], verdict['reason'], verdict=verdict)
    except (BudgetExceeded, KeyboardInterrupt) as exc:
        save('stopped', str(exc) or type(exc).__name__)
        raise
    except (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        return save('inconclusive', 'final_evaluation_error', error_type=type(exc).__name__, error=str(exc)[:300])


def main(argv=None):
    """Operator CLI. The contract and edit permissions never come from a model."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('baseline', 'candidate', 'output', 'contract'):
        parser.add_argument('--' + field, type=Path, required=True)
    parser.add_argument('--contract-sha256', required=True)
    parser.add_argument('--allow-source', action='append', required=True)
    parser.add_argument('--test-pids-limit', type=int, default=64)
    parser.add_argument('--total-seconds', type=int, default=900)
    args = parser.parse_args(argv)
    if not 1 <= args.total_seconds <= 3600:
        parser.error('total-seconds must be within 1..3600')
    adapters, checkers = registered_implementations()
    spec = WorkloadSpec.read_frozen(args.contract, expected_sha256=args.contract_sha256,
                                  adapters=adapters, checkers=checkers)
    started = time.monotonic()
    class Control:
        def check(self):
            if time.monotonic() - started >= args.total_seconds:
                raise BudgetExceeded('final_evaluation_deadline')
    try:
        result = verify_repository_candidate(args.baseline, args.candidate, args.output,
            set(args.allow_source), spec, execution_control=Control(), test_pids_limit=args.test_pids_limit)
    except (BudgetExceeded, KeyboardInterrupt):
        print(json.dumps({'status': 'stopped', 'result': str(args.output / 'result.json')}))
        return 2
    print(json.dumps({'status': result['status'], 'reason': result['reason'],
                      'result': str(args.output / 'result.json')}))
    return 0 if result['status'] == 'passed' else 1 if result['status'] == 'failed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
