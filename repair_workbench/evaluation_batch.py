"""Serial, evaluator-owned batch driver with complete planned-run accounting.

The trusted executor receives a RunContext and returns a verdict; this module
does not import task code, start a provider, or decide correctness itself.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

from .evaluation_budget import EvaluationLedger, _atomic_write
from .experiment_manifest import agent_task, validate_manifest, write_manifest
from .session import BudgetExceeded


_OUTCOMES = {'passed', 'failed', 'inconclusive', 'submitted', 'no_change',
             'insufficient_evidence', 'not_reproduced', 'needs_requirement'}
_LOCAL_STOPS = {'run_token_limit', 'run_request_limit', 'run_deadline'}


def _seconds(value, field):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('invalid_' + field)
    return value


class RunContext:
    """Application-only controls; pass only ``task`` to the model's prompt.

    An executor must route every model request through ``metered_model`` and
    honor ``check`` for physical tools. Model calls are never started here until
    the executor explicitly invokes the returned transport.
    """
    def __init__(self, batch, row):
        self._batch, self._row = batch, row
        self.run_id = row['run_id']
        self.task = agent_task(batch.manifest, row['task_id'])
        self.policy = next(v['policy'] for v in batch.manifest['variants']
                           if v['id'] == row['variant_id'])
        self.output = batch.output / self.run_id
        self.output.mkdir()
        self.started = batch.clock()
        self._evaluation_started = None

    def check(self):
        self._batch.check()
        if self.remaining_seconds() <= 0:
            raise BudgetExceeded('final_evaluation_deadline' if self._evaluation_started is not None
                                 else 'run_deadline')

    def remaining_seconds(self):
        """Remaining current-phase and batch time; final scoring has its own cap."""
        if self._evaluation_started is None:
            phase = self._batch.run_seconds - (self._batch.clock() - self.started)
        else:
            phase = self._batch.final_evaluation_seconds - (self._batch.clock() - self._evaluation_started)
        return max(0, min(phase, self._batch.batch_seconds - (self._batch.clock() - self._batch.started)))

    def _model_check(self):
        self.check()
        if self._evaluation_started is not None:
            raise BudgetExceeded('final_submission_closed_model_calls')

    def begin_evaluation(self, candidate_ref):
        """Close inference and start the independent scoring clock exactly once.

        The executor must separately freeze/verify the actual candidate content.
        This transition closes model access; it is not itself a file snapshot.
        """
        self.check()
        if self._evaluation_started is not None:
            raise RuntimeError('final_evaluation_already_started')
        if (not isinstance(candidate_ref, str) or not candidate_ref
                or self._batch.final_evaluation_seconds is None):
            raise ValueError('candidate_ref_and_final_evaluation_budget_required')
        self._evaluation_started = self._batch.clock()
        self.emit('final_evaluation_started', candidate_ref=candidate_ref)

    def emit(self, kind, **data):
        """Link tool/candidate/verdict artifacts using real application receipts."""
        if not isinstance(kind, str) or not kind or len(kind) > 100:
            raise ValueError('invalid_event_kind')
        event = {'run_id': self.run_id, 'task_id': self._row['task_id'],
                 'variant_id': self._row['variant_id'], 'kind': kind, 'data': data}
        # Run directory is evaluator-owned, never part of the Agent workspace.
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n'
        with (self.output / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(encoded)
            stream.flush()

    def metered_model(self, api_key, *, role='main'):
        from .evaluation_model import MeteredEvaluationTongyi
        model = self._batch.config['model']
        if model['region'] != 'cn-beijing':
            raise ValueError('metered_transport_supports_cn_beijing_only')
        return MeteredEvaluationTongyi(api_key=api_key, model_name=model['id'],
            ledger=self._batch.ledger, run_id=self.run_id, trace_dir=self.output,
            validate_frozen=self._model_check, evaluation_role=role, cache=False,
            max_output_tokens=model['output_token_limit'],
            sampling_temperature=model.get('temperature', 0),
            call_timeout=model.get('call_timeout_seconds', 45))


class FrozenEvaluationBatch:
    """One invocation, no resume/retry; all unstarted rows remain in the report.

    ``expected_sha256`` is an externally retained plan digest. ``current_config``
    is a callable returning the actual runner settings, checked before every
    model call. A content hash is not an authorization or a digital signature.
    """
    def __init__(self, manifest, output, *, expected_sha256, current_config,
                 cancelled=lambda: False, clock=time.monotonic):
        if not callable(current_config) or not callable(cancelled):
            raise ValueError('configuration_and_cancel_callbacks_required')
        self._current_config, self.cancelled, self.clock = current_config, cancelled, clock
        self.expected_sha256 = expected_sha256
        self.manifest = validate_manifest(manifest, current_config=current_config(),
                                          expected_sha256=expected_sha256)
        if len({task['split'] for task in self.manifest['tasks']}) != 1:
            raise ValueError('a_batch_requires_one_task_split')
        self.config = copy.deepcopy(self.manifest['config'])
        limits, model = self.config['limits'], self.config['model']
        self.run_seconds = _seconds(limits.get('run_seconds'), 'run_seconds')
        self.batch_seconds = _seconds(limits.get('batch_seconds'), 'batch_seconds')
        self.final_evaluation_seconds = (None if 'final_evaluation_seconds' not in limits else
                                        _seconds(limits['final_evaluation_seconds'], 'final_evaluation_seconds'))
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        write_manifest(self.output / 'plan.json', self.manifest)
        self.ledger = EvaluationLedger(self.output / 'token-ledger.json', expected_sha256,
            {k: limits[k] for k in ('run_tokens', 'batch_tokens', 'requests_per_run', 'batch_requests')},
            input_token_limit=model.get('input_token_limit'),
            output_token_limit=model.get('output_token_limit'))
        self.started = clock()
        self.report = {'manifest_sha256': expected_sha256,
                       'started_utc': datetime.now(timezone.utc).isoformat(),
                       'rows': [{**item, 'state': 'not_run', 'outcome': 'not_run'}
                                for item in self.manifest['schedule']], 'stop_reason': None}
        self._used = False
        self._save()

    def _save(self):
        self.report['budget'] = {'charged_or_reserved_tokens': self.ledger.charged(),
                                'calls': len(self.ledger.state['calls']),
                                'usage_complete': all(c['state'] == 'settled'
                                                      for c in self.ledger.state['calls'])}
        _atomic_write(self.output / 'evaluation.json', self.report)

    def check(self):
        if self.cancelled():
            raise BudgetExceeded('cancel_requested')
        if self.clock() - self.started >= self.batch_seconds:
            raise BudgetExceeded('batch_deadline')
        if self.ledger.state['blocked']:
            raise BudgetExceeded(self.ledger.state['blocked'])
        validate_manifest(self.manifest, current_config=self._current_config(),
                          expected_sha256=self.expected_sha256)

    def run(self, execute):
        """Execute trusted callback serially. It must return outcome + cleanup_ok.

        Correctness verdicts and cleanup receipts belong to the executor, not the
        LLM. Raw independent reports can be linked in the returned JSON object.
        """
        if self._used:
            raise RuntimeError('batch_already_started')
        self._used = True
        row = None
        try:
            for row in self.report['rows']:
                self.check()
                row['state'], row['outcome'] = 'running', 'running'
                self._save()
                context = RunContext(self, row)
                context.emit('run_started', policy=context.policy)
                cleanup_confirmed = False
                try:
                    verdict = execute(context)
                    if any(c['state'] != 'settled' for c in self.ledger.state['calls']
                           if c['run_id'] == context.run_id):
                        raise BudgetExceeded('unsettled_model_usage')
                    if not isinstance(verdict, dict) or verdict.get('outcome') not in _OUTCOMES:
                        raise ValueError('invalid_executor_verdict')
                    if verdict.get('cleanup_ok') is not True:
                        raise BudgetExceeded('container_cleanup_unconfirmed')
                    cleanup_confirmed = True
                    context.check()
                    row.update(state='completed', outcome=verdict['outcome'], result=copy.deepcopy(verdict))
                    context.emit('run_completed', outcome=row['outcome'])
                except BudgetExceeded as exc:
                    row.update(state='stopped', outcome=str(exc))
                    context.emit('run_stopped', reason=str(exc), cleanup_confirmed=cleanup_confirmed)
                    if str(exc) not in _LOCAL_STOPS or not cleanup_confirmed:
                        raise
                finally:
                    row['seconds'] = round(self.clock() - context.started, 6)
                    self._save()
        except BaseException as exc:
            self.report['stop_reason'] = str(exc) if isinstance(exc, BudgetExceeded) else type(exc).__name__
            if row is not None and row['state'] == 'running':
                row.update(state='error', outcome='evaluation_error', error_type=type(exc).__name__)
            self._save()
            if not isinstance(exc, Exception):
                raise
        finally:
            self.report['completed_utc'] = datetime.now(timezone.utc).isoformat()
            self._save()
        return copy.deepcopy(self.report)
