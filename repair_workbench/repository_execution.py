"""Close public repository diagnosis before independent final-data evaluation.

The caller explicitly supplies a model. This library does not load credentials
or authorize API use. Real runs must use an approved frozen batch and its metered
model; the separate evaluator receives no model object or provider environment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import time

from .evaluation_budget import _atomic_write
from .repository_preparation import prepare_repository_diagnostic_baseline
from .session import BudgetExceeded
from .snapshot import snapshot_id, verify_snapshot
from .workload_contract import canonical_json


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_candidate(agent_output, result, submission, expected_base):
    relative = submission.get('candidate_path')
    if (type(relative) is not str or PurePosixPath(relative).as_posix() != relative
            or not re.fullmatch(r'attempt-0[1-5]/verification', relative)):
        raise ValueError('invalid_sealed_candidate_path')
    number = int(relative.split('/')[0].split('-')[1])
    if result.get('selected_attempt') != number:
        raise ValueError('selected_attempt_disagrees_with_sealed_submission')
    candidate = agent_output / relative
    if candidate.is_symlink() or candidate.resolve() != candidate:
        raise ValueError('sealed_candidate_must_be_a_regular_workspace')
    manifest = verify_snapshot(candidate)
    if (manifest.get('base_snapshot_id') != expected_base
            or snapshot_id(manifest) != submission.get('candidate_ref')
            or _hash(candidate / 'patch.diff') != submission.get('patch_sha256')):
        raise ValueError('sealed_candidate_or_patch_changed')
    return candidate


def _final_cleanup_confirmed(verdict):
    containers, tests, runs, schedule = (verdict.get(key) for key in ('containers', 'tests', 'runs', 'schedule'))
    return (type(containers) is dict and all(state == 'cleaned' for state in containers.values())
            and type(tests) is dict and all(item.get('cleanup_ok') is True for item in tests.values())
            and type(runs) is list and all(item['result'].get('cleanup_ok') is True for item in runs)
            and type(schedule) is list
            and not any(row.get('state') in ('running', 'stopped', 'error') for row in schedule))


def _grade_in_separate_process(spec, baseline, candidate, output, allowed_paths, *,
                                total_seconds, execution_control=None, test_pids_limit=64):
    """Launch the trusted no-key verifier CLI, retaining its first result/log."""
    from .swebench_adapter import isolated_env
    contract = output.parent / 'private-final-contract.json'
    spec.write_frozen(contract)
    command = [sys.executable, '-m', 'repair_workbench.repository_performance',
        '--baseline', str(baseline), '--candidate', str(candidate), '--output', str(output),
        '--contract', str(contract), '--contract-sha256', spec.sha256,
        '--total-seconds', str(total_seconds), '--test-pids-limit', str(test_pids_limit)]
    for source in sorted(allowed_paths):
        command += ['--allow-source', source]
    process = None
    started = time.monotonic()
    try:
        with (output.parent / 'independent-evaluator.log').open('xb') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                env=isolated_env(), start_new_session=True)
            while process.poll() is None:
                if execution_control is not None:
                    execution_control.check()
                if time.monotonic() - started > total_seconds + 5:
                    raise BudgetExceeded('independent_evaluator_deadline')
                time.sleep(.05)
        path = output / 'result.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('missing_or_oversized_independent_verdict')
        result = json.loads(path.read_text())
        if (result.get('contract_sha256') != spec.sha256
                or result.get('baseline_ref') != snapshot_id(verify_snapshot(baseline))
                or result.get('submitted_ref') != snapshot_id(verify_snapshot(candidate))
                or result.get('status') not in ('passed', 'failed', 'inconclusive', 'stopped')
                or process.returncode != (0 if result['status'] == 'passed' else 1 if result['status'] == 'failed' else 2)):
            raise ValueError('independent_verdict_binding_or_exit_mismatch')
        return {**result, 'cleanup_ok': _final_cleanup_confirmed(result)}
    finally:
        if process is not None and process.poll() is None:
            # SIGINT lets the child execute its owned-container finally blocks.
            # If it cannot exit within cleanup grace, this caller must retain
            # unknown cleanup and stop; it never assumes process death cleaned Docker.
            try:
                import os
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=35)
            except (OSError, subprocess.SubprocessError):
                try: process.kill()
                except ProcessLookupError: pass
                process.wait(timeout=5)


def run_repository_diagnostic_task(spec, baseline, output, allowed_paths, model, *, problem_statement,
                                   strategy='adaptive', max_model_calls=12, max_probe_runs=6,
                                   deadline_seconds=180, final_seconds=900, enable_reproduction=False,
                                   event_sink=None, cancellation_check=None, execution_control=None,
                                   identity_check=None, on_final_submission=None, test_pids_limit=64):
    """Trusted orchestration: prepare → public Agent → seal → separate grader.

    ``identity_check`` is an application-owned full frozen-state gate. Polling
    should use a cheap execution_control view. Independent final feedback is
    written only after run_agent returns; no second model invocation is made.
    """
    if (type(problem_statement) is not str or not 1 <= len(problem_statement) <= 12000
            or type(final_seconds) is not int or not 1 <= final_seconds <= 3600):
        raise ValueError('invalid_repository_task_or_final_deadline')
    baseline = Path(baseline).resolve(strict=True)
    manifest = verify_snapshot(baseline)
    output = Path(output).resolve()
    if any(output == root or root in output.parents
           for root in (baseline, Path(manifest['source_repo']).resolve())):
        raise ValueError('repository_execution_output_must_be_outside_source')
    allowed_paths = frozenset(allowed_paths)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    record = {'status': 'inconclusive', 'phase': 'preparation', 'reason': 'not_completed',
              'agent_finished_before_final_evaluation': False, 'independent_evaluation_started': False,
              'test_pids_limit': test_pids_limit, 'cleanup_ok': False,
              'scope': 'One task execution; submitted/proposal are not independent acceptance.'}
    class PollControl:
        def check(self):
            if cancellation_check is not None and cancellation_check():
                raise BudgetExceeded('cancel_requested')
            if execution_control is not None:
                execution_control.check()
    poll = PollControl()
    def fence():
        if identity_check is not None:
            identity_check()
        poll.check()
    try:
        fence()
        prepared = prepare_repository_diagnostic_baseline(spec, baseline, output / 'preparation',
            execution_control=poll, test_pids_limit=test_pids_limit)
        fence()
        from .repository_diagnosis import RepositoryDiagnosticSession
        from .agent import run_agent
        session = RepositoryDiagnosticSession(prepared['baseline'], output / 'agent', allowed_paths,
            prepared['development_workload'], prepared['baseline_qualification'], strategy=strategy,
            max_model_calls=max_model_calls, max_probe_runs=max_probe_runs, deadline_seconds=deadline_seconds,
            enable_reproduction=enable_reproduction, event_sink=event_sink,
            cancellation_check=cancellation_check, execution_control=poll)
        record['phase'] = 'agent'
        result = run_agent(session, model, problem_statement)
        record.update(agent_result='agent/result.json', agent_status=result.get('status'),
                      agent_finished_before_final_evaluation=True, cleanup_ok=result.get('cleanup_ok') is True)
        fence()
        if result.get('cleanup_ok') is not True:
            record.update(reason='public_execution_cleanup_unconfirmed', cleanup_ok=False)
            return record
        if result.get('status') not in ('submitted', 'proposal_sealed'):
            record.update(reason='no_sealed_submission', cleanup_ok=True)
            return record
        sealed = output / 'agent/final-submission.json'
        if (sealed.is_symlink() or not sealed.is_file() or sealed.stat().st_size > 65536
                or _hash(sealed) != result.get('final_submission_sha256')):
            raise ValueError('sealed_submission_missing_or_changed')
        submission = json.loads(sealed.read_text())
        public = prepared['development_workload']
        expected_base = snapshot_id(verify_snapshot(prepared['baseline']))
        if (submission.get('schema_version') != 1 or submission.get('task_id') != public['task_id']
                or submission.get('baseline_ref') != expected_base
                or submission.get('public_workload_sha256') != hashlib.sha256(canonical_json(public).encode()).hexdigest()
                or submission.get('trust') != 'sealed_submission_not_independent_acceptance'):
            raise ValueError('sealed_submission_public_binding_mismatch')
        if submission['decision'] in ('no_change', 'insufficient_evidence'):
            record.update(reason='sealed_proposal_requires_independent_review', cleanup_ok=True,
                          submission='agent/final-submission.json')
            return record
        if submission['decision'] != 'candidate' or result['status'] != 'submitted':
            raise ValueError('invalid_final_submission_decision')
        candidate = _selected_candidate(output / 'agent', result, submission, expected_base)
        fence()
        if on_final_submission is not None:
            on_final_submission(_hash(sealed))
        record.update(phase='evaluation', independent_evaluation_started=True,
                      submission='agent/final-submission.json', final_submission_sha256=_hash(sealed),
                      cleanup_ok=False)
        _atomic_write(output / 'execution.json', record)
        verdict = _grade_in_separate_process(spec, prepared['baseline'], candidate,
            output / 'independent-evaluation', allowed_paths, total_seconds=final_seconds,
            execution_control=poll, test_pids_limit=test_pids_limit)
        record['cleanup_ok'] = verdict.get('cleanup_ok') is True
        fence()
        _selected_candidate(output / 'agent', result, submission, expected_base)
        if _hash(sealed) != record['final_submission_sha256']:
            raise ValueError('sealed_submission_changed_during_final_evaluation')
        if not record['cleanup_ok']:
            record.update(reason='independent_evaluation_cleanup_unconfirmed',
                          independent_verdict='independent-evaluation/result.json')
            return record
        record.update(status=verdict['status'], phase='completed', reason=verdict['reason'],
                      independent_verdict='independent-evaluation/result.json')
        return record
    except (BudgetExceeded, KeyboardInterrupt) as exc:
        record.update(status='stopped', reason=str(exc) or 'cancel_requested')
        raise
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        record.update(status='inconclusive', reason='repository_execution_error', error_type=type(exc).__name__)
        return record
    finally:
        _atomic_write(output / 'execution.json', record)
