"""Frozen fixed/adaptive repository evaluation with an explicit model key.

No credentials, downloads, model authorization or implicit retries are supplied
by this module. Qualification is based on frozen physical controls owned by the
operator. An exposed development task stays development; this adapter does not
turn a successful reference control into a model result or a holdout task.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from .diagnostic_evaluation import source_fingerprint
from .evaluation_batch import FrozenEvaluationBatch
from .evaluation_budget import _atomic_write
from .experiment_manifest import canonical_bytes
from .repository_execution import run_repository_diagnostic_task
from .repository_performance import _commit_matches, _supported, original_suite_sha256, registered_implementations
from .reproduction import resolve_image
from .session import BudgetExceeded
from .snapshot import snapshot_id, verify_snapshot
from .workload_contract import WorkloadSpec


_TOOLS = {'list_sources', 'read_source', 'source_outline', 'read_symbol', 'search_source', 'read_test_log',
          'record_hypothesis', 'run_probe', 'read_evidence', 'read_probe_report', 'submit_ref_edits',
          'conclude_diagnosis', 'read_file', 'write_todos'}
_PRIVATE = {'baseline', 'contract', 'contract_sha256', 'qualification'}
_QUALIFICATION = {'schema_version', 'contract_sha256', 'baseline_ref', 'image_id', 'implementation_sha256',
                  'positive_execution', 'positive_result', 'positive_implementation', 'negative_result',
                  'developer_exposed'}


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _absolute(value):
    if type(value) is not str:
        raise ValueError('repository_evaluator_path_must_be_absolute')
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.as_posix() != value:
        raise ValueError('repository_evaluator_path_must_be_absolute_and_canonical')
    return path


def _read(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('missing_or_oversized_repository_qualification_artifact')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_repository_qualification_field')
            result[key] = value
        return result
    data = json.loads(path.read_bytes().decode('utf-8'), object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_repository_qualification')))
    if type(data) is not dict:
        raise ValueError('invalid_repository_qualification_artifact')
    return data


def _task_paths(task):
    private = task.get('evaluator')
    if type(private) is not dict or set(private) != _PRIVATE:
        raise ValueError('repository_evaluator_requires_baseline_contract_and_qualification')
    contract_sha = private['contract_sha256']
    if type(contract_sha) is not str or not re.fullmatch('[a-f0-9]{64}', contract_sha):
        raise ValueError('invalid_frozen_repository_contract_hash')
    baseline, contract, qualification = (_absolute(private[k]) for k in ('baseline', 'contract', 'qualification'))
    q = _read(qualification)
    if (set(q) != _QUALIFICATION or type(q['schema_version']) is not int or q['schema_version'] != 1
            or type(q['developer_exposed']) is not bool):
        raise ValueError('invalid_repository_qualification_fields')
    positive_execution, positive, implementation, negative = (_absolute(q[k]) for k in
        ('positive_execution', 'positive_result', 'positive_implementation', 'negative_result'))
    if (positive != positive_execution.parent / 'independent-evaluation/result.json'
            or implementation != positive_execution.parent.parent / 'implementation.json'):
        raise ValueError('repository_positive_control_execution_result_mismatch')
    return baseline, contract, qualification, q, positive_execution, positive, implementation, negative


def required_binding_paths(task):
    """Freeze exact trusted control artifacts; the Agent receives none of them."""
    baseline, contract, qualification, _, execution, positive, implementation, negative = _task_paths(task)
    return [baseline / 'manifest.json', contract, qualification, execution, positive, implementation, negative]


def _test_environment(receipt, image, pids, status):
    return (type(receipt) is dict and receipt.get('status') == status and receipt.get('cleanup_ok') is True
            and receipt.get('image_id') == image and receipt.get('test_execution_policy') == {
                'pids_limit': pids, 'memory_bytes': 268435456, 'cpus': 1, 'network': 'none', 'auto_pull': False})


def require_physical_qualification(spec, baseline_ref, pids, execution, positive, negative):
    """Reject noisy, incomplete or mixed-run controls before any model request."""
    data = spec.as_dict()
    image, planned = data['measurement']['image_id'], 8 * len(data['final_inputs'])
    for record in (positive, negative):
        if (record.get('contract_sha256') != spec.sha256 or record.get('baseline_ref') != baseline_ref
                or record.get('test_pids_limit') != pids or record.get('planned_measurement_runs') != planned):
            raise ValueError('repository_qualification_identity_mismatch')
    if (execution.get('status') != 'passed' or execution.get('agent_status') != 'submitted'
            or execution.get('independent_evaluation_started') is not True
            or execution.get('agent_finished_before_final_evaluation') is not True
            or execution.get('cleanup_ok') is not True
            or execution.get('independent_verdict') != 'independent-evaluation/result.json'
            or positive.get('status') != 'passed'):
        raise ValueError('repository_positive_control_not_qualified')
    fields = ('case_id', 'probe', 'position', 'role')
    expected = [(case['case_id'], probe, position, role) for case in data['final_inputs']
                for probe in ('benchmark', 'memory') for position, role in enumerate(data['measurement']['order'])]
    def schedule(record, key):
        return [tuple(row.get(k) for k in fields) for row in record.get(key, [])]
    def original_passed(record, role):
        outcomes = record.get('tests', {}).get(role, {}).get('test_inventory', {}).get('outcomes')
        return spec.original_test_result(outcomes)['accepted'] if type(outcomes) is dict and outcomes else None
    if (len(positive.get('schedule', [])) != planned or len(positive.get('runs', [])) != planned
            or any(row.get('state') != 'completed' for row in positive['schedule'])
            or schedule(positive, 'schedule') != expected or schedule(positive, 'runs') != expected
            or any(row['result'].get('cleanup_ok') is not True or row['result'].get('input_integrity_ok') is not True
                   or row['result'].get('status') != 'ok' or row['result'].get('image_id') != image
                   for row in positive['runs'])
            or any(not _test_environment(positive.get('tests', {}).get(role), image, pids, 'tests_passed')
                   or original_passed(positive, role) is not True for role in ('baseline', 'candidate'))):
        raise ValueError('repository_positive_control_incomplete')
    if (negative.get('status') != 'failed' or negative.get('reason') != 'candidate_original_tests_rejected'
            or negative.get('runs') != [] or len(negative.get('schedule', [])) != planned
            or schedule(negative, 'schedule') != expected
            or any(row.get('state') != 'not_run' for row in negative['schedule'])
            or not _test_environment(negative.get('tests', {}).get('baseline'), image, pids, 'tests_passed')
            or original_passed(negative, 'baseline') is not True
            or original_passed(negative, 'candidate') is not False
            or not _test_environment(negative.get('tests', {}).get('candidate'), image, pids, 'tests_failed')):
        raise ValueError('repository_negative_control_not_qualified')


class RepositoryEvaluationExecutor:
    def __init__(self, batch, api_key):
        if type(api_key) is not str or not api_key.strip():
            raise ValueError('explicit_model_key_required')
        self.batch, self._key, self._ready = batch, api_key, False

    def _settings(self):
        config, manifest = self.batch.config, self.batch.manifest
        implementation = source_fingerprint()
        if config.get('implementation') != implementation:
            raise ValueError('repository_implementation_or_dependency_drift')
        settings = config.get('repository_diagnostic')
        if (type(settings) is not dict or set(settings) != {'enable_reproduction', 'test_pids_limit'}
                or type(settings['enable_reproduction']) is not bool
                or type(settings['test_pids_limit']) is not int or not 64 <= settings['test_pids_limit'] <= 256):
            raise ValueError('invalid_repository_diagnostic_settings')
        tools = _TOOLS | ({'propose_reproducer', 'run_reproducer'} if settings['enable_reproduction'] else set())
        if set(config['tools']) != tools:
            raise ValueError('repository_frozen_toolset_mismatch')
        limits = config['limits']
        if (limits.get('max_tool_calls') != 60 or not 1 <= limits.get('requests_per_run', 0) <= 30
                or limits.get('max_attempts', 3) != 3
                or not 3 <= limits.get('max_probe_runs', 0) <= 12
                or not 1 <= limits.get('final_evaluation_seconds', 0) <= 3600):
            raise ValueError('unsupported_repository_batch_limits')
        if (len(manifest['variants']) != 2 or {v['policy'] for v in manifest['variants']} != {'fixed', 'adaptive'}):
            raise ValueError('repository_batch_requires_fixed_and_adaptive')
        return settings, _digest(implementation)

    def _binding(self, task, settings, implementation_sha):
        baseline, contract, qualification, q, execution, positive, implementation, negative = _task_paths(task)
        bound = {r['path'] for r in self.batch.manifest['files']}
        if not {str(p) for p in (baseline / 'manifest.json', contract, qualification, execution, positive, implementation, negative)} <= bound:
            raise ValueError('repository_evaluator_artifacts_not_bound')
        adapters, checkers = registered_implementations()
        spec = WorkloadSpec.read_frozen(contract, expected_sha256=task['evaluator']['contract_sha256'],
                                       adapters=adapters, checkers=checkers)
        data = _supported(spec)
        if q['developer_exposed'] and task['split'] != 'dev':
            raise ValueError('repository_exposed_task_must_stay_dev')
        manifest = verify_snapshot(baseline)
        baseline_ref = snapshot_id(manifest)
        image = data['measurement']['image_id']
        if (q['contract_sha256'] != spec.sha256 or q['baseline_ref'] != baseline_ref or q['image_id'] != image
                or q['implementation_sha256'] != implementation_sha
                or data['task_id'] != task['id'] or data['repo'] != task['public']['repo']
                or data['base_commit'] != task['public']['base_commit']
                or not _commit_matches(manifest, data['base_commit'])
                or original_suite_sha256(manifest) != data['original_tests']['suite_sha256']):
            raise ValueError('repository_task_or_qualification_binding_mismatch')
        require_physical_qualification(spec, baseline_ref, settings['test_pids_limit'],
                                       _read(execution), _read(positive), _read(negative))
        if _digest(_read(implementation)) != implementation_sha:
            raise ValueError('repository_positive_control_source_or_dependencies_changed')
        if resolve_image(image) != image:
            raise ValueError('repository_qualified_image_missing_or_changed')
        from .candidate import validate_source_path
        allowed = set()
        for entry in manifest['files']:
            name = entry['path']
            try:
                validate_source_path(name, {name})
            except ValueError:
                continue
            allowed.add(name)
        if not allowed:
            raise ValueError('repository_has_no_approved_source_files')
        return spec, baseline, allowed

    def __call__(self, ctx):
        receipt = {'phase': 'preflight', 'cleanup_ok': True}
        output = ctx.output / 'repository'
        try:
            ctx.check()
            settings, fingerprint = self._settings()
            tasks = self.batch.manifest['tasks']
            if not self._ready:
                for task in tasks:
                    self.batch.check()
                    self._binding(task, settings, fingerprint)
                self._ready = True
            task = next(t for t in tasks if t['id'] == ctx.task['id'])
            spec, baseline, allowed = self._binding(task, settings, fingerprint)
            ctx.check()
            limits = self.batch.config['limits']
            model = ctx.metered_model(self._key)
            receipt.update(phase='execution', cleanup_ok=False)
            result = run_repository_diagnostic_task(spec, baseline, output, allowed, model,
                problem_statement=ctx.task['problem_statement'], strategy=ctx.policy,
                max_model_calls=limits['requests_per_run'], max_probe_runs=limits['max_probe_runs'],
                deadline_seconds=ctx.remaining_seconds(), final_seconds=math.ceil(limits['final_evaluation_seconds']),
                enable_reproduction=settings['enable_reproduction'], test_pids_limit=settings['test_pids_limit'],
                execution_control=ctx.execution_control(), identity_check=ctx.check,
                on_final_submission=ctx.begin_evaluation, event_sink=lambda event: ctx.emit('agent_event', receipt=event))
            receipt.update(phase='completed', cleanup_ok=result.get('cleanup_ok') is True,
                           agent_status=result.get('agent_status'), outcome=result['status'])
            ctx.check()
            return {'outcome': result['status'] if result['status'] in ('passed', 'failed') else 'inconclusive',
                    'cleanup_ok': receipt['cleanup_ok'], 'reason': result['reason'],
                    'execution': 'repository/execution.json'}
        except BudgetExceeded as exc:
            if str(exc) == 'run_deadline' and (output / 'execution.json').is_file():
                result = _read(output / 'execution.json')
                receipt['cleanup_ok'] = result.get('cleanup_ok') is True
                return {'outcome': 'inconclusive', 'cleanup_ok': receipt['cleanup_ok'], 'reason': 'run_deadline',
                        'execution': 'repository/execution.json'}
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            receipt.update(error_type=type(exc).__name__, reason=(reason if re.fullmatch('[A-Za-z0-9_]+', reason)
                                                                  else 'repository_executor_error'))
            raise
        finally:
            _atomic_write(ctx.output / 'repository-executor-receipt.json', receipt, create=True)


def run_repository_batch(manifest, output, *, expected_sha256, current_config, api_key,
                         cancelled=lambda: False):
    """Application-only entry for an exact already authorized frozen plan."""
    batch = FrozenEvaluationBatch(manifest, output, expected_sha256=expected_sha256,
                                  current_config=current_config, cancelled=cancelled)
    return batch.run(RepositoryEvaluationExecutor(batch, api_key))
