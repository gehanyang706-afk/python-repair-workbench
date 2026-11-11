"""Produce a public baseline qualification without disclosing final workload data."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

from .evaluation_budget import _atomic_write
from .repository_performance import _commit_matches, _supported, original_suite_sha256
from .runner import run_tests
from .snapshot import snapshot_id, verify_snapshot
from .workload_contract import canonical_json


def prepare_repository_diagnostic_baseline(spec, baseline, destination, *,
                                          execution_control=None, test_pids_limit=64):
    """Trusted preparer owns the complete spec; the Session receives only its projection.

    A new baseline/report directory is made for every qualification. Neither the
    target repository nor pre-existing reports are modified. No model is loaded.
    The returned receipt is a locally trusted preparation fact, not authenticated
    evidence that an arbitrary external workload or JSON file is trustworthy.
    """
    data = _supported(spec)
    if type(test_pids_limit) is not int or not 64 <= test_pids_limit <= 256:
        raise ValueError('invalid_original_test_pids_limit')
    baseline = Path(baseline).resolve(strict=True)
    destination = Path(destination).resolve()
    manifest = verify_snapshot(baseline)
    baseline_ref = snapshot_id(manifest)
    if (not _commit_matches(manifest, data['base_commit'])
            or original_suite_sha256(manifest) != data['original_tests']['suite_sha256']):
        raise ValueError('diagnostic_preparation_contract_source_mismatch')
    for root in (baseline, Path(manifest['source_repo']).resolve()):
        if destination == root or root in destination.parents:
            raise ValueError('diagnostic_preparation_must_be_outside_source')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    clone = destination / 'baseline'
    clone.mkdir()
    shutil.copytree(baseline / 'workspace', clone / 'workspace')
    _atomic_write(clone / 'manifest.json', manifest, create=True)
    manifest_hash = hashlib.sha256((baseline / 'manifest.json').read_bytes()).hexdigest()
    record = {'status': 'incomplete', 'baseline_ref': baseline_ref,
              'contract_sha256': spec.sha256, 'containers': {}, 'real_model_calls': 0}
    class Control:
        def check(self):
            if execution_control is not None:
                execution_control.check()
        def container(self, name, state):
            record['containers'][name] = state
            if execution_control is not None and hasattr(execution_control, 'container'):
                execution_control.container(name, state)
    control = Control()
    try:
        control.check()
        result = run_tests(clone, data['measurement']['image_id'], data['measurement']['timeout_seconds'],
                           collect_nodeids=True, test_pids_limit=test_pids_limit, execution_control=control)
        record['test_result'] = result
        control.check()
        if (snapshot_id(verify_snapshot(baseline)) != baseline_ref
                or hashlib.sha256((baseline / 'manifest.json').read_bytes()).hexdigest() != manifest_hash
                or snapshot_id(verify_snapshot(clone)) != baseline_ref):
            raise ValueError('diagnostic_preparation_source_drift')
        if (result.get('status') != 'tests_passed' or result.get('cleanup_ok') is not True
                or result.get('image_id') != data['measurement']['image_id']
                or result.get('snapshot_id') != baseline_ref
                or type(result.get('test_inventory')) is not dict):
            raise ValueError('diagnostic_preparation_original_tests_unqualified')
        outcomes = result['test_inventory']['outcomes']
        accepted = spec.original_test_result(outcomes)
        record['original_tests'] = accepted
        if not accepted['accepted']:
            raise ValueError('diagnostic_preparation_original_inventory_unqualified')
        report_hash = hashlib.sha256((clone / 'reports/result.json').read_bytes()).hexdigest()
        qualification = {'schema_version': 1, 'task_id': data['task_id'], 'repo': data['repo'],
            'base_commit': data['base_commit'], 'baseline_ref': baseline_ref,
            'image_id': data['measurement']['image_id'],
            'suite_sha256': data['original_tests']['suite_sha256'], 'report_sha256': report_hash,
            'inventory_sha256': hashlib.sha256(canonical_json(outcomes).encode()).hexdigest(),
            'original_tests_accepted': True}
        projection = spec.development_projection()
        _atomic_write(destination / 'qualification.json', qualification, create=True)
        _atomic_write(destination / 'development.json', projection, create=True)
        record['status'] = 'qualified'
        return {'baseline': clone, 'development_workload': projection,
                'baseline_qualification': qualification}
    except BaseException as exc:
        record.update(status='unqualified', error_type=type(exc).__name__, reason=str(exc)[:300])
        raise
    finally:
        _atomic_write(destination / 'preparation.json', record, create=True)
