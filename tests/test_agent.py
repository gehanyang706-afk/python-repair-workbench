"""Agent 权限与预算契约；确定性模型只存在于测试代码中。"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repair_workbench.session import BudgetExceeded, RepairSession
from repair_workbench.snapshot import create_snapshot, snapshot_id

try:
    from repair_workbench.agent import build_agent, run_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field
    AGENT_AVAILABLE = True
except ImportError:
    AGENT_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]


def fixture(root, real=False):
    source = root / 'source'
    shutil.copytree(ROOT / 'tests/fixtures/broken_discount', source,
                    ignore=shutil.ignore_patterns('__pycache__'))
    for args in (('init', '-q'), ('add', '--all'), ('commit', '-qm', 'Regression fixture')):
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        '-C', str(source), *args], check=True, capture_output=True)
    baseline = root / 'baseline'
    manifest = create_snapshot(source, baseline)
    if real:
        from repair_workbench.runner import run_tests
        result = run_tests(baseline, os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'))
        assert result['status'] == 'tests_failed', result
    else:
        reports = baseline / 'reports'
        reports.mkdir()
        (reports / 'pytest.xml').write_text('<testsuite><testcase classname="pricing" name="boundary"><failure/></testcase></testsuite>')
        (reports / 'pytest.log').write_text('assert 100.0 == 90.0\n')
        (reports / 'result.json').write_text(json.dumps({'snapshot_id': snapshot_id(manifest),
            'status': 'tests_failed', 'cleanup_ok': True, 'image_id': 'unit-test-only',
            'counts': {'failed': 1}}))
    return baseline


if AGENT_AVAILABLE:
    class ScriptedModel(BaseChatModel):
        responses: list = Field(default_factory=list)
        calls: int = 0
        exposed: list = Field(default_factory=list)

        @property
        def _llm_type(self):
            return 'test-scripted'

        def bind_tools(self, tools, **kwargs):
            self.exposed = [t.name for t in tools]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            response = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return ChatResult(generations=[ChatGeneration(message=response)])

    def call(name, args, identity):
        return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': identity}])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = fixture(self.root)

    def session(self, **kwargs):
        return RepairSession(self.baseline, self.root / 'agent', {'pricing.py'}, **kwargs)

    def test_read_scope_and_line_numbers(self):
        session = self.session()
        self.assertIn('1:', session.read_source('pricing.py')['content'])
        for path in ('../source/pricing.py', '/etc/passwd', 'tests/../pricing.py', '.env'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                session.read_source(path)

    def test_baseline_mismatch_rejected(self):
        (self.baseline / 'workspace/pricing.py').write_text('changed')
        with self.assertRaises(ValueError):
            self.session()
        self.assertFalse((self.root / 'agent').exists())

    def test_unreproduced_baseline_rejected(self):
        p = self.baseline / 'reports/result.json'
        data = json.loads(p.read_text()); data['status'] = 'execution_error'
        p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.session()

    def test_invalid_attempt_consumes_budget_and_cannot_change_tests(self):
        session = self.session(max_attempts=1)
        result = session.submit_candidate({'tests/test_pricing.py': ''}, 'remove test')
        self.assertEqual(result['status'], 'inconclusive')
        with self.assertRaises(BudgetExceeded):
            session.submit_candidate({'pricing.py': 'x=1'}, 'second')
        self.assertTrue((self.baseline / 'workspace/tests/test_pricing.py').read_text())

    def test_deadline_and_model_limits(self):
        session = self.session(max_model_calls=1)
        session.reserve_model()
        with self.assertRaises(BudgetExceeded):
            session.reserve_model()
        session.started -= session.deadline_seconds + 1
        with self.assertRaises(BudgetExceeded):
            session.read_source('pricing.py')

    def test_nonfinite_timeout_rejected(self):
        with self.assertRaises(ValueError):
            self.session(deadline_seconds=float('nan'))

    def test_output_cannot_overwrite_or_enter_original(self):
        session = self.session()
        with self.assertRaises(FileExistsError):
            self.session()
        with self.assertRaises(ValueError):
            RepairSession(self.baseline, self.root / 'source/agent', {'pricing.py'})

    def test_report_log_is_bounded(self):
        (self.baseline / 'reports/pytest.log').write_text('x' * 18000)
        result = self.session().read_test_log()
        self.assertEqual(len(result['content']), 16000)
        self.assertEqual(result['next_offset'], 16000)


@unittest.skipUnless(AGENT_AVAILABLE, '需要安装 agent 依赖')
class AgentContractTests(unittest.TestCase):
    setUp = SessionTests.setUp
    session = SessionTests.session

    def test_text_claim_does_not_pass_and_tools_are_restricted(self):
        session = self.session()
        model = ScriptedModel(responses=[AIMessage(content='修复成功，所有测试通过')])
        result = run_agent(session, model, '修复折扣边界')
        self.assertEqual(result['reason'], 'no_candidate')
        self.assertEqual(result['status'], 'inconclusive')
        self.assertIn('submit_candidate', model.exposed)
        self.assertIn('read_file', model.exposed)
        self.assertNotIn('execute', model.exposed)
        self.assertNotIn('task', model.exposed)
        self.assertNotIn('write_file', model.exposed)

    def test_model_loop_stops_at_limit(self):
        session = self.session(max_model_calls=2)
        model = ScriptedModel(responses=[call('list_sources', {}, 'list')])
        result = run_agent(session, model, '修复折扣边界')
        self.assertEqual(result['stop_reason'], 'model_call_limit')
        self.assertEqual(result['model_calls'], 2)

    def test_unknown_shell_tool_cannot_execute(self):
        marker = self.root / 'unexpected'
        model = ScriptedModel(responses=[call('execute', {'command': f'touch {marker}'}, 'shell'),
                                         AIMessage(content='done')])
        result = run_agent(self.session(), model, '修复折扣边界')
        self.assertFalse(marker.exists())
        self.assertNotEqual(result['status'], 'passed')

    def test_provider_exception_is_sanitized(self):
        session = self.session()
        with patch.object(ScriptedModel, '_generate', side_effect=RuntimeError('secret-marker')):
            result = run_agent(session, ScriptedModel(), '修复折扣边界')
        self.assertEqual(result['stop_reason'], 'agent_error')
        self.assertNotIn('secret-marker', (session.output / 'result.json').read_text())
        self.assertNotIn('secret-marker', (session.output / 'events.jsonl').read_text())


@unittest.skipUnless(AGENT_AVAILABLE and os.environ.get('RUN_DOCKER_TESTS') == '1', '需要 Agent 与 Docker')
class AgentDockerTests(unittest.TestCase):
    def test_failed_candidate_then_real_verified_repair(self):
        from uuid import uuid4
        root = ROOT / 'runtime' / ('agent-contract-' + uuid4().hex[:10])
        baseline = fixture(root, real=True)
        original = (baseline / 'workspace/pricing.py').read_text()
        model = ScriptedModel(responses=[
            call('read_test_log', {}, 'log'),
            call('read_source', {'path': 'pricing.py'}, 'read'),
            call('submit_candidate', {'changes': {'pricing.py': original.replace('amount > 100', 'amount > 110')},
                                      'diagnosis': '候选阈值调整'}, 'wrong'),
            call('submit_candidate', {'changes': {'pricing.py': original.replace('amount > 100', 'amount >= 100')},
                                      'diagnosis': '边界应包含 100'}, 'fix'),
            AIMessage(content='原始测试范围通过')])
        session = RepairSession(baseline, root / 'agent', {'pricing.py'})
        result = run_agent(session, model, '修复满 100 元折扣')
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual([a['status'] for a in result['attempts']], ['failed', 'passed'])
        self.assertEqual((root / 'source/pricing.py').read_text(), original)
        self.assertTrue((session.output / result['patch']).exists())
        with self.assertRaises(ValueError):
            session.submit_candidate({'pricing.py': 'x=1'}, '已成功不得继续改动')
