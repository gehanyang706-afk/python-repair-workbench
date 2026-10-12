"""任务持久化、HTTP 权限、事件重连与执行器集成契约。"""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from repair_workbench.storage import TaskStore
from repair_workbench.service import TaskService
from test_agent import fixture, AGENT_AVAILABLE

try:
    from fastapi.testclient import TestClient
    from repair_workbench.api import create_app
    WEB_AVAILABLE = True
except ImportError:
    WEB_AVAILABLE = False


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tasks.sqlite'
        self.store = TaskStore(self.path)

    def test_atomic_claim_only_one_worker_receives_task(self):
        task = self.store.create({'task': 'test'})
        results = []
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            results.append(TaskStore(self.path).claim())
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.store.get(task['id'])['state'], 'running')

    def test_cancelled_queue_item_is_not_executed(self):
        task = self.store.create({})
        self.store.cancel(task['id'])
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(task['id'])['state'], 'cancelled')

    def test_restart_marks_active_interrupted_and_keeps_queue(self):
        active = self.store.create({}); self.store.claim()
        waiting = self.store.create({})
        reopened = TaskStore(self.path); reopened.recover()
        self.assertEqual(reopened.get(active['id'])['state'], 'interrupted')
        self.assertEqual(reopened.claim()['id'], waiting['id'])

    def test_cancel_wins_completion_race_but_preserves_result(self):
        task = self.store.create({}); self.store.claim(); self.store.cancel(task['id'])
        self.store.finish(task['id'], {'agent': {'status': 'passed'}})
        result = self.store.get(task['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['result']['agent']['status'], 'passed')

    def test_events_resume_after_cursor(self):
        task = self.store.create({})
        first = self.store.events(task['id'])[-1]['id']
        self.store.event(task['id'], {'event': 'new'})
        events = self.store.events(task['id'], first)
        self.assertEqual([e['event'] for e in events], ['new'])

    def test_baseline_progress_persists_without_finishing_task(self):
        task = self.store.create({}); self.store.claim()
        self.store.event(task['id'], {'event': 'baseline_finished'}, result={
            'baseline': {'counts': {'passed': 2, 'failed': 1}}})
        restored = TaskStore(self.path).get(task['id'])
        self.assertEqual(restored['state'], 'running')
        self.assertEqual(restored['result']['baseline']['counts']['failed'], 1)


@unittest.skipUnless(WEB_AVAILABLE, '需要安装 web 依赖')
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.model_factory = Mock()
        self.service = TaskService(self.root / 'data', [self.root / 'source'], 'test-local', model_factory=self.model_factory)
        self.client = TestClient(create_app(self.service, start_worker=False), base_url='http://127.0.0.1')
        self.headers = {'X-Workbench-Request': '1', 'Origin': 'http://127.0.0.1'}
        self.body = {'repo': str(self.root / 'source'), 'task': '修复边界', 'allowed_paths': ['pricing.py']}

    def post_task(self):
        response = self.client.post('/api/tasks', json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_foreign_origin_and_missing_header_rejected(self):
        self.assertEqual(self.client.post('/api/tasks', json=self.body).status_code, 403)
        self.assertEqual(self.client.post('/api/tasks', json=self.body,
            headers={**self.headers, 'Origin': 'https://foreign.example'}).status_code, 403)
        self.assertEqual(self.service.store.list(), [])

    def test_repository_inspection_and_pinned_submission(self):
        result = self.client.post('/api/repository', json={'repo': self.body['repo']}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['source_files'], ['pricing.py'])
        task = self.post_task()
        self.assertEqual(task['request']['revision'], result.json()['commit'])
        self.assertNotIn('env_file', self.client.get('/api/config').text)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_path_escape_and_tests_not_accepted(self):
        response = self.client.post('/api/repository', json={'repo': str(self.root)}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.body['allowed_paths'] = ['tests/test_pricing.py']
        self.assertEqual(self.client.post('/api/tasks', json=self.body, headers=self.headers).status_code, 400)
        self.assertEqual(self.service.store.list(), [])

    def test_web_cannot_supply_model_configuration(self):
        self.body['env_file'] = '/private/config'
        self.assertEqual(self.client.post('/api/tasks', json=self.body, headers=self.headers).status_code, 422)

    def test_cancel_and_event_cursor(self):
        task = self.post_task()
        first = self.service.store.events(task['id'])[0]['id']
        response = self.client.post(f"/api/tasks/{task['id']}/cancel", headers=self.headers)
        self.assertEqual(response.json()['state'], 'cancelled')
        result = self.client.get(f"/api/tasks/{task['id']}/events", headers={'Last-Event-ID': str(first)})
        self.assertNotIn('"event": "queued"', result.text)
        self.assertIn('"event": "cancelled"', result.text)
        self.assertIn('event: complete', result.text)
        self.assertIsNone(self.service.store.claim())

    def test_passing_baseline_never_calls_model(self):
        task = self.post_task()
        with patch('repair_workbench.service.run_tests', return_value={
                'status': 'tests_passed', 'cleanup_ok': True, 'counts': {'total': 3, 'passed': 3}}):
            self.service.execute(self.service.store.claim())
        self.model_factory.assert_not_called()
        result = self.client.get(f"/api/tasks/{task['id']}").json()
        self.assertEqual(result['result']['reason'], 'no_failure_to_repair')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.client.get(f"/api/tasks/{task['id']}/patch").status_code, 404)

    def test_failed_baseline_does_not_start_agent(self):
        task = self.post_task()
        with patch('repair_workbench.service.run_tests', return_value={
                'status': 'execution_error', 'cleanup_ok': True, 'counts': {}}):
            self.service.execute(self.service.store.claim())
        self.model_factory.assert_not_called()
        self.assertEqual(self.service.store.get(task['id'])['result']['reason'], 'baseline_not_reproduced')

    def test_artifact_symlink_escape_blocked(self):
        task = self.post_task()
        report = self.service.data_dir / 'runs' / task['id'] / 'baseline/reports'
        report.mkdir(parents=True)
        (report / 'pytest.log').symlink_to(self.root / 'source/pricing.py')
        self.assertEqual(self.client.get(f"/api/tasks/{task['id']}/log").status_code, 400)

    def test_duplicate_executor_is_rejected(self):
        second = TaskService(self.service.data_dir, [self.root / 'source'], 'test-local', model_factory=Mock())
        self.service.start()
        try:
            with self.assertRaises(RuntimeError): second.start()
        finally:
            self.service.stop()

    def test_cooperative_cancel_reaches_session_budget(self):
        from repair_workbench.session import RepairSession, BudgetExceeded
        cancelled = threading.Event()
        events = []
        session = RepairSession(self.root / 'baseline', self.root / 'agent', {'pricing.py'},
                                cancellation_check=cancelled.is_set, event_sink=events.append)
        cancelled.set()
        with self.assertRaisesRegex(BudgetExceeded, 'cancel_requested'):
            session.reserve_model()
        self.assertEqual(events[0]['event'], 'session_started')


@unittest.skipUnless(WEB_AVAILABLE and AGENT_AVAILABLE and os.environ.get('RUN_DOCKER_TESTS') == '1', '需要 Web、Agent 和 Docker')
class WebDockerTests(unittest.TestCase):
    def test_submit_queue_execute_report_and_download(self):
        from test_agent import ScriptedModel, call, AIMessage, ROOT
        from uuid import uuid4
        root = ROOT / 'runtime' / ('web-contract-' + uuid4().hex[:10])
        fixture(root)
        original = (root / 'source/pricing.py').read_text()
        model = ScriptedModel(responses=[call('submit_edits', {
            'edits': [{'path': 'pricing.py', 'sha256': hashlib.sha256(original.encode()).hexdigest(),
                       'start_line': 5, 'old_text': '    if amount > 100:',
                       'new_text': '    if amount >= 100:'}],
            'diagnosis': '边界包含 100'}, 'fix'), AIMessage(content='done')])
        service = TaskService(root / 'data', [root / 'source'],
                              os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'),
                              model_factory=lambda: model)
        self.assertEqual(service.profile, 'v2')
        with TestClient(create_app(service), base_url='http://127.0.0.1') as client:
            task = client.post('/api/tasks', headers={'X-Workbench-Request': '1'}, json={
                'repo': str(root / 'source'), 'task': '修复边界', 'allowed_paths': ['pricing.py']}).json()
            import time
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                detail = client.get('/api/tasks/' + task['id']).json()
                if detail['state'] in {'completed', 'error'}: break
                time.sleep(.1)
            self.assertEqual(detail['state'], 'completed', detail)
            self.assertEqual(detail['result']['agent']['status'], 'passed')
            patch_response = client.get(f"/api/tasks/{task['id']}/patch")
            self.assertEqual(patch_response.status_code, 200)
            self.assertIn('attachment', patch_response.headers['content-disposition'])
            patch_text = patch_response.text
            self.assertIn('+    if amount >= 100:', patch_text)
            self.assertEqual((root / 'source/pricing.py').read_text(), original)
            events = client.get(f"/api/tasks/{task['id']}/events").text
            self.assertIn('attempt_finished', events)
            self.assertIn('event: complete', events)
            report_response = client.get(f"/api/tasks/{task['id']}/report")
            self.assertEqual(report_response.status_code, 200)
            self.assertEqual(report_response.json()['result']['agent']['status'], 'passed')
            self.assertIn('attachment', report_response.headers['content-disposition'])
