"""Real isolated Redis and independent processes; no model or target-source execution."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

try:
    import redis
except ImportError:
    redis = None

from repair_workbench.redis_rate_limit import RedisRateLimiter
from repair_workbench.session import BudgetExceeded
from repair_workbench.tool_runtime import ToolRuntime


def consume(url, namespace, start, queue, count):
    client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    limiter = RedisRateLimiter(client, namespace, 20)
    def record(event, **fields):
        if fields.get('outcome') == 'shared_rate_admitted':
            queue.put({'pid': os.getpid(), 'time_us': fields['server_time_us']})
    runtime = ToolRuntime(qps=10000, shared_limiter=limiter, record=record)
    if not start.wait(10):
        raise TimeoutError('process start barrier timed out')
    for number in range(count):
        runtime.invoke('local' if number % 2 else 'mcp:reports', 'read', {'number': number}, lambda: 1)


def abandon(url, namespace, queue):
    limiter = RedisRateLimiter(redis.Redis.from_url(url), namespace, 20)
    queue.put(limiter.acquire())
    queue.close()
    queue.join_thread()
    os._exit(7)


@unittest.skipUnless(redis is not None and os.environ.get('RUN_REDIS_TESTS') == '1', 'explicit isolated Redis tests')
class RedisRateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'repair-rate-test-' + uuid.uuid4().hex[:12]
        cls.archive = Path(__file__).resolve().parents[1] / 'runtime' / cls.name
        cls.archive.mkdir(parents=True)
        def cleanup():
            logs = subprocess.run(['docker', 'logs', cls.name], capture_output=True, text=True, timeout=10)
            (cls.archive / 'redis.log').write_text(logs.stdout + logs.stderr)
            removed = subprocess.run(['docker', 'rm', '-f', cls.name], capture_output=True, timeout=15)
            absent = subprocess.run(['docker', 'inspect', cls.name], capture_output=True, timeout=10).returncode != 0
            (cls.archive / 'cleanup.json').write_text(json.dumps({'removed': removed.returncode == 0,
                                                                 'inspect_confirms_absent': absent}) + '\n')
        cls.addClassCleanup(cleanup)
        image = subprocess.check_output(['docker', 'image', 'inspect',
            os.environ.get('REPAIR_REDIS_TEST_IMAGE', 'redis:7.4.2'), '--format', '{{.Id}}'], text=True).strip()
        (cls.archive / 'image.json').write_text(json.dumps({'image_id': image}) + '\n')
        subprocess.run(['docker', 'run', '-d', '--pull', 'never', '--name', cls.name,
                        '--label', 'repair-workbench.redis-test=true', '-p', '127.0.0.1::6379',
                        '--memory', '128m', '--cpus', '.5', '--pids-limit', '64',
                        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', 'redis', image,
                        'redis-server', '--save', '', '--appendonly', 'no', '--maxmemory', '64mb',
                        '--maxmemory-policy', 'noeviction'], check=True, capture_output=True, timeout=30)
        port = subprocess.check_output(['docker', 'port', cls.name, '6379/tcp'], text=True).strip().rsplit(':', 1)[1]
        cls.url = 'redis://127.0.0.1:' + port + '/0'
        cls.client = redis.Redis.from_url(cls.url, socket_timeout=1, socket_connect_timeout=1)
        deadline = time.monotonic() + 10
        while True:
            try:
                cls.client.ping()
                break
            except redis.RedisError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def namespace(self):
        return 'test-' + uuid.uuid4().hex

    def test_multiple_task_processes_share_one_rate_across_endpoints(self):
        context = multiprocessing.get_context('spawn')
        queue, start = context.Queue(), context.Event()
        namespace = self.namespace()
        workers = [context.Process(target=consume, args=(self.url, namespace, start, queue, 4)) for _ in range(3)]
        try:
            for process in workers:
                process.start()
            start.set()
            admissions = [queue.get(timeout=15) for _ in range(12)]
            for process in workers:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in workers:
                if process.is_alive():
                    process.terminate()
                process.join(5)
            queue.close()
        ordered = sorted(r['time_us'] for r in admissions)
        intervals = [b - a for a, b in zip(ordered, ordered[1:])]
        self.assertEqual(len({r['pid'] for r in admissions}), 3)
        self.assertGreaterEqual(min(intervals), 50000)
        (self.archive / 'multiprocess-admissions.json').write_text(json.dumps(admissions, indent=2) + '\n')
        print(json.dumps({'redis_multiprocess': {'processes': 3, 'admissions': 12,
            'configured_qps': 20, 'minimum_interval_us': min(intervals), 'model_calls': 0}}))

    def test_conflicting_policy_and_backend_outage_fail_closed(self):
        namespace = self.namespace()
        RedisRateLimiter(self.client, namespace, 20).acquire()
        with self.assertRaisesRegex(BudgetExceeded, 'shared_rate_policy_conflict'):
            RedisRateLimiter(self.client, namespace, 30).acquire()
        # Disconnect only this adapter; never shut down a shared external service.
        bad = redis.Redis(host='127.0.0.1', port=1, socket_timeout=.1, socket_connect_timeout=.1)
        runtime = ToolRuntime(shared_limiter=RedisRateLimiter(bad, self.namespace(), 20))
        called = []
        with self.assertRaisesRegex(BudgetExceeded, 'shared_rate_backend_unavailable'):
            runtime.invoke('mcp:reports', 'read', {}, lambda: called.append(1))
        self.assertFalse(called)

    def test_process_crash_leaves_only_a_finite_rate_reservation(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        namespace = self.namespace()
        process = context.Process(target=abandon, args=(self.url, namespace, queue))
        process.start()
        reserved = queue.get(timeout=10)
        process.join(10)
        self.assertEqual(process.exitcode, 7)
        admitted = RedisRateLimiter(self.client, namespace, 20).acquire()
        self.assertGreaterEqual(admitted - reserved, 50000)
        self.assertGreater(self.client.pttl('repair-tools:{' + namespace + '}:rate'), 0)
        queue.close()

    def test_session_configuration_wires_shared_rate_without_logging_connection_url(self):
        from test_diagnosis import passing_baseline
        from repair_workbench.diagnosis import DiagnosticSession
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = passing_baseline(root)
            config = root / 'rate.json'
            namespace = self.namespace()
            config.write_text(json.dumps({'url': self.url, 'namespace': namespace, 'qps': 20}))
            session = DiagnosticSession(baseline, root / 'agent', {'map_exporter.py'},
                                        tool_rate_config=config, tool_concurrency=2)
            session.tool_runtime.invoke('local', 'read', {}, lambda: 1)
            saved = session.save('running')
            self.assertEqual(saved['tool_runtime']['shared_rate']['namespace'], namespace)
            self.assertNotIn(self.url, json.dumps(saved))
            self.assertEqual(saved['tool_runtime']['shared_rate']['backend'], 'redis')

    def test_waiting_for_shared_rate_observes_root_cancellation(self):
        limiter = RedisRateLimiter(self.client, self.namespace(), .5)
        limiter.acquire()
        started = time.monotonic()
        def check():
            if time.monotonic() - started > .05:
                raise BudgetExceeded('cancel_requested')
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            limiter.acquire(check)
        self.assertLess(time.monotonic() - started, 1)

    def test_real_redis_pause_stops_dispatch_and_unpause_allows_recovery(self):
        from redis.retry import Retry
        from redis.backoff import NoBackoff
        client = redis.Redis.from_url(self.url, socket_timeout=.2, socket_connect_timeout=.2,
                                      retry=Retry(NoBackoff(), 0))
        runtime = ToolRuntime(shared_limiter=RedisRateLimiter(client, self.namespace(), 20))
        called = []
        subprocess.run(['docker', 'pause', self.name], check=True, capture_output=True, timeout=10)
        try:
            with self.assertRaisesRegex(BudgetExceeded, 'shared_rate_backend_unavailable'):
                runtime.invoke('local', 'read', {}, lambda: called.append(1))
            self.assertFalse(called)
            self.assertEqual(runtime.summary()['dispatched'], 0)
        finally:
            subprocess.run(['docker', 'unpause', self.name], check=True, capture_output=True, timeout=10)
        runtime.invoke('local', 'read', {}, lambda: called.append(1))
        self.assertEqual(called, [1])

    def test_failed_rate_preflight_persists_a_stopped_task_before_any_model(self):
        from test_diagnosis import passing_baseline
        from repair_workbench.diagnosis import DiagnosticSession
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = passing_baseline(root)
            config = root / 'rate.json'
            config.write_text(json.dumps({'url': 'redis://127.0.0.1:1/0',
                                           'namespace': self.namespace(), 'qps': 20}))
            with self.assertRaisesRegex(BudgetExceeded, 'shared_rate_backend_unavailable'):
                DiagnosticSession(baseline, root / 'agent', {'map_exporter.py'}, tool_rate_config=config)
            saved = json.loads((root / 'agent/result.json').read_text())
            self.assertEqual(saved['state'], 'stopped')
            self.assertEqual(saved['model_calls'], 0)


class RedisPolicyTests(unittest.TestCase):
    def test_invalid_namespace_and_unbounded_rate_are_rejected_without_a_connection(self):
        for namespace, qps in [('bad{namespace}', 20), ('valid', 0), ('valid', True),
                                ('valid', float('nan')), ('valid', .001), ('valid', 1001)]:
            with self.subTest(namespace=namespace, qps=qps), self.assertRaises(ValueError):
                RedisRateLimiter(None, namespace, qps)


if __name__ == '__main__':
    unittest.main()
