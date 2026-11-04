"""Scheduling, sharing and error contracts; no model or network requests."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest

from repair_workbench.tool_runtime import ToolRuntime, error_category
from repair_workbench.session import BudgetExceeded


class ToolRuntimeTests(unittest.TestCase):
    def test_immutable_cache_is_scoped_by_endpoint_version_and_arguments(self):
        runtime = ToolRuntime(qps=10000)
        calls = []
        def execute():
            calls.append(1)
            return {'rows': [1]}
        def query(endpoint='local', version='snapshot-a', offset=0):
            return runtime.invoke(endpoint, 'read', {'offset': offset}, execute, version=version)
        query()['rows'].append(99)
        self.assertEqual(query(), {'rows': [1]})
        query(version='snapshot-b')
        query(endpoint='mcp:reports')
        query(offset=1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(runtime.summary()['cache_hits'], 1)

    def test_parallel_identical_queries_coalesce_and_do_not_share_mutable_results(self):
        runtime = ToolRuntime(qps=10000)
        started, release = threading.Event(), threading.Event()
        calls = []
        def execute():
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(3))
            return {'rows': [1]}
        with ThreadPoolExecutor(4) as pool:
            owner = pool.submit(runtime.invoke, 'mcp:reports', 'read', {}, execute, version='v1')
            self.assertTrue(started.wait(3))
            others = [pool.submit(runtime.invoke, 'mcp:reports', 'read', {}, execute, version='v1') for _ in range(3)]
            deadline = time.monotonic() + 3
            while runtime.summary()['coalesced'] < 3 and time.monotonic() < deadline:
                threading.Event().wait(.005)
            release.set()
            values = [owner.result(), *[future.result() for future in others]]
        self.assertEqual(len(calls), 1)
        values[0]['rows'].append(2)
        self.assertTrue(all(row == {'rows': [1]} for row in values[1:]))
        self.assertEqual(runtime.summary()['coalesced'], 3)

    def test_qps_is_shared_across_different_endpoints(self):
        runtime = ToolRuntime(qps=20, max_inflight=2)
        timestamps = []
        for endpoint in ('local', 'mcp:diagnostics', 'mcp:reports'):
            runtime.invoke(endpoint, 'read', {}, lambda: timestamps.append(time.monotonic()))
        self.assertTrue(all(b - a >= .045 for a, b in zip(timestamps, timestamps[1:])))

    def test_stateful_operations_and_errors_are_never_cached_or_retried(self):
        runtime = ToolRuntime(qps=10000)
        calls = []
        def execute():
            calls.append(1)
            return len(calls)
        self.assertEqual(runtime.invoke('local', 'edit', {}, execute), 1)
        self.assertEqual(runtime.invoke('local', 'edit', {}, execute), 2)
        def fail():
            calls.append(1)
            raise ConnectionError('do not put this prose into telemetry')
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                runtime.invoke('mcp:reports', 'read', {}, fail, version='v1')
        self.assertEqual(len(calls), 4)
        self.assertEqual(runtime.summary()['failures'], 2)

    def test_cancelled_cache_hit_and_queued_call_cannot_bypass_root_budget(self):
        runtime = ToolRuntime(qps=1)
        runtime.invoke('local', 'read', {}, lambda: 1, version='v1')
        def stop():
            raise BudgetExceeded('cancel_requested')
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            runtime.invoke('local', 'read', {}, lambda: self.fail('cached execution'), version='v1', check=stop)
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            runtime.invoke('mcp:reports', 'read', {}, lambda: self.fail('queued execution'), check=stop)
        self.assertEqual(runtime.summary()['dispatched'], 1)

    def test_per_endpoint_slots_do_not_block_a_different_mcp_service(self):
        runtime = ToolRuntime(qps=10000, max_inflight=2, per_endpoint=1)
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            self.assertTrue(release.wait(3))
            return 1
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(runtime.invoke, 'mcp:diagnostics', 'probe', {}, blocked)
            self.assertTrue(started.wait(3))
            second = pool.submit(runtime.invoke, 'mcp:reports', 'read', {}, lambda: 2)
            try:
                self.assertEqual(second.result(timeout=2), 2)
            finally:
                release.set()
            self.assertEqual(first.result(), 1)

    def test_errors_are_classified_by_status_and_backend_without_message_matching(self):
        class StatusError(Exception):
            def __init__(self, status):
                self.status_code = status
        self.assertEqual(error_category(StatusError(401), 'mcp:reports'), 'authentication')
        self.assertEqual(error_category(StatusError(402), 'mcp:reports'), 'quota')
        self.assertEqual(error_category(StatusError(429), 'mcp:reports'), 'rate_limit')
        self.assertEqual(error_category(ConnectionError(), 'mcp:reports'), 'remote_connection')
        self.assertEqual(error_category(ValueError(), 'mcp:reports'), 'remote_execution')
        self.assertEqual(error_category(ValueError('429 Unauthorized'), 'local'), 'local_execution')

    def test_queued_request_observes_cancellation_without_running_the_handler(self):
        runtime = ToolRuntime(qps=10000, max_inflight=1)
        started, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(3)
            return 1
        def check():
            if cancelled.is_set():
                raise BudgetExceeded('cancel_requested')
        with ThreadPoolExecutor(2) as pool:
            owner = pool.submit(runtime.invoke, 'local', 'read', {}, blocked)
            self.assertTrue(started.wait(3))
            queued = pool.submit(runtime.invoke, 'mcp:reports', 'read', {},
                                 lambda: self.fail('cancelled handler executed'), check=check)
            cancelled.set()
            try:
                with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
                    queued.result(timeout=2)
            finally:
                release.set()
            self.assertEqual(owner.result(), 1)

    def test_real_mcp_connection_error_groups_keep_connection_classification(self):
        try:
            from mcp.shared.exceptions import MCPError
        except ImportError:
            self.skipTest('optional MCP SDK required')
        exc = ExceptionGroup('outer', [ExceptionGroup('inner', [MCPError(-32000, 'Connection closed')])])
        self.assertEqual(error_category(exc, 'mcp:reports'), 'remote_connection')


if __name__ == '__main__':
    unittest.main()
