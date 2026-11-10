"""Durable pessimistic Token reservations, owned by one batch process.

This is a local evaluation ledger, not a distributed billing service. Bounds
must come from a frozen provider configuration, never from model estimates.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

from .session import BudgetExceeded


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError('invalid_' + name)
    return value


def _atomic_write(path, value, *, create=False):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
    if create:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        temporary = path.with_name('.' + path.name + '.' + uuid4().hex)
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class EvaluationLedger:
    """Reserve before send; settle once with complete provider usage only.

    Calls from different roles share run/batch Token and optional request caps.
    A ledger cannot be reopened/reset, inherited by another process or resumed.
    Unknown usage retains its full reservation and blocks every later call.
    """
    def __init__(self, path, plan_hash, limits, *, input_token_limit, output_token_limit):
        requested = Path(path).absolute()
        if requested.is_symlink():
            raise ValueError('symlinked_ledger_path')
        # Resolve the operator's directory once (including macOS /var aliases).
        # Every subsequent write uses this canonical parent, never the alias.
        self.path = requested.parent.resolve(strict=True) / requested.name
        if not isinstance(plan_hash, str) or not plan_hash:
            raise ValueError('invalid_plan_hash')
        self._input_limit = _positive(input_token_limit, 'input_token_limit')
        self._output_limit = _positive(output_token_limit, 'output_token_limit')
        self._limits = copy.deepcopy(limits)
        if not isinstance(self._limits, dict):
            raise ValueError('invalid_limits')
        for field in ('batch_tokens', 'run_tokens'):
            _positive(self._limits.get(field), field)
        for field in ('requests_per_run', 'batch_requests'):
            if field in self._limits:
                _positive(self._limits[field], field)
        self._owner = os.getpid()
        self._lock = threading.RLock()
        self._state = {'schema_version': 2, 'plan_hash': plan_hash,
                       'limits': self._limits,
                       'usage_bounds': {'input_tokens': self._input_limit,
                                        'output_tokens': self._output_limit},
                       'calls': [], 'blocked': None}
        _atomic_write(self.path, self._state, create=True)

    @property
    def input_token_limit(self):
        return self._input_limit

    @property
    def output_token_limit(self):
        return self._output_limit

    @property
    def reservation(self):
        return self._input_limit + self._output_limit

    @property
    def limits(self):
        return copy.deepcopy(self._limits)

    @property
    def state(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _check_owner(self):
        if os.getpid() != self._owner:
            raise RuntimeError('ledger_process_owner_changed')

    def flush(self):
        self._check_owner()
        try:
            _atomic_write(self.path, self._state)
        except BaseException:
            # A failed durable write cannot be followed by another request,
            # even if the caller catches the filesystem exception.
            self._state['blocked'] = 'ledger_persistence_failure'
            raise

    def charged(self, run_id=None):
        with self._lock:
            return sum(c['charged_tokens'] for c in self._state['calls']
                       if run_id is None or c['run_id'] == run_id)

    def reserve(self, run_id, *, role='main'):
        with self._lock:
            self._check_owner()
            if self._state['blocked']:
                raise BudgetExceeded(self._state['blocked'])
            if (not isinstance(run_id, str) or not run_id or len(run_id) > 200
                    or not isinstance(role, str) or not role or len(role) > 64):
                raise ValueError('invalid_call_identity')
            calls = self._state['calls']
            if ('batch_requests' in self._limits and len(calls) >= self._limits['batch_requests']):
                raise BudgetExceeded('batch_request_limit')
            if ('requests_per_run' in self._limits
                    and sum(c['run_id'] == run_id for c in calls) >= self._limits['requests_per_run']):
                raise BudgetExceeded('run_request_limit')
            for scope, cap in ((None, self._limits['batch_tokens']),
                               (run_id, self._limits['run_tokens'])):
                if self.charged(scope) + self.reservation > cap:
                    raise BudgetExceeded('batch_token_limit' if scope is None else 'run_token_limit')
            call = {'index': len(calls) + 1, 'run_id': run_id, 'role': role,
                    'state': 'reserved', 'charged_tokens': self.reservation,
                    'input_tokens': None, 'output_tokens': None}
            calls.append(call)
            self.flush()
            return call

    def _pending(self, call):
        self._check_owner()
        if (not any(entry is call for entry in self._state['calls'])
                or call['state'] != 'reserved'):
            raise ValueError('unknown_or_already_settled_call')

    def unknown(self, call, reason):
        with self._lock:
            self._pending(call)
            call.update(state='usage_unknown', reason=reason)
            self._state['blocked'] = 'batch_usage_unknown'
            self.flush()

    def settle(self, call, message):
        with self._lock:
            self._pending(call)
            usage = getattr(message, 'usage_metadata', None)
            if not usage:
                usage = (getattr(message, 'response_metadata', None) or {}).get('token_usage')
            if not (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                    for k in ('input_tokens', 'output_tokens'))):
                self.unknown(call, 'incomplete_provider_usage')
                raise BudgetExceeded('batch_usage_unknown')
            if (usage['input_tokens'] > self._input_limit
                    or usage['output_tokens'] > self._output_limit):
                self.unknown(call, 'provider_usage_exceeds_native_bounds')
                raise BudgetExceeded('batch_usage_unknown')
            if 'total_tokens' in usage and (type(usage['total_tokens']) is not int
                    or usage['total_tokens'] != usage['input_tokens'] + usage['output_tokens']):
                self.unknown(call, 'inconsistent_provider_usage')
                raise BudgetExceeded('batch_usage_unknown')
            try:
                json.dumps(usage, allow_nan=False)
            except (TypeError, ValueError):
                self.unknown(call, 'unserializable_provider_usage')
                raise BudgetExceeded('batch_usage_unknown')
            call.update(state='settled', input_tokens=usage['input_tokens'],
                        output_tokens=usage['output_tokens'],
                        charged_tokens=usage['input_tokens'] + usage['output_tokens'])
            # Preserve detailed cached/reasoning counts as subsets, not extra Tokens.
            call['provider_usage'] = copy.deepcopy(usage)
            self.flush()
