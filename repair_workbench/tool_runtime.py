"""Task-scoped scheduling for trusted local and MCP adapters.

Only pure, version-bound operations may opt into caching/single-flight. Admission
to application tools remains outside this layer, so cache hits never grant quota.
There are no automatic retries or model-selected server commands.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import math
import threading
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def error_category(exc, backend):
    """Use structured status/known codes; never guess from exception prose."""
    if getattr(exc, 'category', None) in {'authentication', 'quota', 'rate_limit', 'remote_connection',
                                        'remote_execution', 'protocol', 'multiple_errors'}:
        return exc.category
    if isinstance(exc, BaseExceptionGroup):
        categories = {error_category(item, backend) for item in exc.exceptions}
        return next(iter(categories)) if len(categories) == 1 else 'multiple_errors'
    status = getattr(exc, 'status_code', None)
    if status is None:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status in {401, 403}:
        return 'authentication'
    if status == 402:
        return 'quota'
    if status == 429:
        return 'rate_limit'
    reason = getattr(exc, 'reason', None)
    if reason in {'cancel_requested', 'diagnostic_probe_timeout', 'mcp_probe_timeout'}:
        return 'cancelled' if reason == 'cancel_requested' else 'timeout'
    if isinstance(exc, TimeoutError):
        return 'timeout'
    if backend == 'local':
        return 'local_execution'
    if getattr(getattr(exc, 'error', None), 'code', None) == -32000:
        return 'remote_connection'  # MCP SDK CONNECTION_CLOSED, not message matching.
    if reason == 'mcp_transport_error' or isinstance(exc, (ConnectionError, EOFError, OSError)):
        return 'remote_connection'
    if reason and reason.startswith('mcp_') and reason != 'mcp_tool_error':
        return 'protocol'
    return 'remote_execution'


@dataclass
class _Flight:
    finished: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


class ToolRuntime:
    """One scheduler shared by a task's main Agent and read-only investigations.

    QPS applies to physical dispatches across all registered endpoints in this
    process. Endpoint concurrency is separately bounded. Cache identity includes
    endpoint, operation, arguments and caller-supplied immutable version scope.
    """
    def __init__(self, *, endpoints=('local', 'mcp:diagnostics', 'mcp:reports'),
                 qps=40, max_inflight=4, per_endpoint=2, cache_entries=128, record=None):
        if (type(qps) not in {int, float} or not math.isfinite(qps) or qps <= 0
                or any(type(v) is not int or v < 1 for v in (max_inflight, per_endpoint, cache_entries))):
            raise ValueError('invalid tool runtime limits')
        self.endpoints = frozenset(endpoints)
        self.qps, self.max_inflight, self.per_endpoint = qps, max_inflight, per_endpoint
        self.cache_entries, self.record = cache_entries, record
        self._lock = threading.Lock()
        self._cache, self._flights = OrderedDict(), {}
        self._active, self._by_endpoint, self._next = 0, {}, 0.0
        self._counts = dict(requests=0, dispatched=0, cache_hits=0, coalesced=0, failures=0)

    def summary(self):
        with self._lock:
            return {**self._counts, 'qps': self.qps, 'max_inflight': self.max_inflight,
                    'per_endpoint': self.per_endpoint, 'scope': 'one_task_one_process'}

    def _emit(self, **event):
        if self.record:
            self.record('tool_runtime', **event)

    def _acquire(self, endpoint, check):
        while True:
            if check:
                check()
            with self._lock:
                now = time.monotonic()
                if (self._active < self.max_inflight
                        and self._by_endpoint.get(endpoint, 0) < self.per_endpoint and now >= self._next):
                    self._active += 1
                    self._by_endpoint[endpoint] = self._by_endpoint.get(endpoint, 0) + 1
                    self._next = now + 1 / self.qps
                    self._counts['dispatched'] += 1
                    return
            # Bound each wait so root cancellation/deadlines remain responsive.
            threading.Event().wait(.01)

    def invoke(self, endpoint, operation, arguments, execute, *, version=None, check=None):
        if endpoint not in self.endpoints:
            raise ValueError('unregistered tool endpoint')
        # A fresh JSON copy prevents callers from mutating cached observations.
        # No version => stateful operation => never cached or coalesced.
        key = digest([endpoint, operation, arguments, version])
        mode, flight = 'dispatch', None
        with self._lock:
            self._counts['requests'] += 1
            if version is not None and key in self._cache:
                mode, value = 'cache_hit', self._cache[key]
                self._cache.move_to_end(key)
                self._counts['cache_hits'] += 1
            elif version is not None and key in self._flights:
                mode, flight = 'coalesced', self._flights[key]
                self._counts['coalesced'] += 1
            elif version is not None:
                flight = self._flights[key] = _Flight()
        event = {'endpoint': endpoint, 'operation': operation, 'request_sha256': key, 'mode': mode}
        if check:
            try:
                check()
            except BaseException as exc:
                if mode == 'dispatch' and flight is not None:
                    with self._lock:
                        flight.error = exc
                        self._flights.pop(key, None)
                        flight.finished.set()
                raise
        if mode == 'cache_hit':
            self._emit(**event, outcome='ok')
            return json.loads(value)
        if mode == 'coalesced':
            while not flight.finished.wait(.01):
                if check:
                    check()
            if check:
                check()
            if flight.error is not None:
                self._emit(**event, outcome='error', category=error_category(flight.error, endpoint))
                raise flight.error
            self._emit(**event, outcome='ok')
            return json.loads(flight.result)
        acquired = False
        try:
            self._acquire(endpoint, check)
            acquired = True
            result = execute()
            if check:
                check()
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False) if version is not None else None
            with self._lock:
                if version is not None:
                    self._cache[key] = encoded
                    while len(self._cache) > self.cache_entries:
                        self._cache.popitem(last=False)
                if flight is not None:
                    flight.result = encoded
            self._emit(**event, outcome='ok')
            return result
        except BaseException as exc:
            with self._lock:
                self._counts['failures'] += 1
                if flight is not None:
                    flight.error = exc
            # Preserve original permission/budget/cancellation exceptions.
            self._emit(**event, outcome='error', category=error_category(exc, endpoint))
            raise
        finally:
            with self._lock:
                if acquired:
                    self._active -= 1
                    self._by_endpoint[endpoint] -= 1
                if flight is not None:
                    self._flights.pop(key, None)
                    flight.finished.set()
