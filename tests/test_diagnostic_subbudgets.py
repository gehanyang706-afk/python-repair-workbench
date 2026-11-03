"""Offline child quotas preserve root capacity; synthetic clocks/verifier only.

These tests run real Deep Agents graphs with deterministic provider doubles.
They establish admission, cancellation and continuation behavior, not LLM quality,
real optimization results, HTTP request counts or token hard-budget coverage.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from repair_workbench import diagnostic_delegation as delegation
from repair_workbench.agent import run_agent
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.model_transport import run_request_worker
from repair_workbench.session import BudgetExceeded, RepairSession
from test_diagnosis import passing_baseline


class QuotaDriver:
    def __init__(self, mode='models'):
        self.mode = mode
        self.child_calls = 0
        self.main_calls = 0
        self.clock_offset = 0
        self.child_views = []
        self.main_task_receipts = []
        self.child_hook = None


class QuotaModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    driver: object = Field(exclude=True)
    tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return 'offline-child-quota-policy'

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={'tools': [tool.name for tool in tools]})

    def invoke_controlled(self, messages, session, stop=None, **kwargs):
        session.check_budget()
        if hasattr(session, '_root'):
            self.driver.child_views.append({
                'started': session.started, 'deadline_seconds': session.deadline_seconds,
                'root_started': session._root.started,
                'root_deadline_seconds': session._root.deadline_seconds,
            })
            if self.driver.mode == 'expired_before_provider':
                # No provider entry after the local deadline, including a delayed request.
                self.driver.clock_offset += session.deadline_seconds + .01
                session.check_budget()
        return self.invoke(messages, stop=stop, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        driver = self.driver
        child = any('你是只读代码调查子Agent' in str(message.content) for message in messages)
        if child:
            driver.child_calls += 1
            if driver.child_hook:
                driver.child_hook()
            if driver.mode == 'time_after_provider':
                driver.clock_offset += 1
            calls = [{'name': 'read_source', 'args': {'path': 'map_exporter.py'},
                      'id': f'child-read-{driver.child_calls}-0'}]
            if driver.mode == 'tools':
                calls.append({'name': 'source_outline', 'args': {'path': 'map_exporter.py'},
                              'id': f'child-read-{driver.child_calls}-1'})
            message = AIMessage(content='', tool_calls=calls)
        else:
            driver.main_calls += 1
            latest = next((item for item in reversed(messages) if isinstance(item, ToolMessage)), None)
            if latest is None:
                message = self.action('task', {
                    'description': 'Inspect the export source with read-only tools and cite findings.',
                    'subagent_type': 'investigator',
                }, 'main-investigation')
            elif latest.name == 'task':
                driver.main_task_receipts.append(json.loads(latest.content))
                message = self.action('read_source', {'path': 'map_exporter.py'}, 'main-read')
            elif latest.name == 'read_source':
                read = json.loads(latest.content)
                old = read['content'].splitlines(keepends=True)[0]
                message = self.action('submit_ref_edits', {
                    'edits': [{'source_ref': read['source_ref'], 'old_text': old,
                               'new_text': old + '# Offline subbudget mechanism candidate.\n'}],
                    'diagnosis': 'Mechanism-only candidate; verifier is an offline test double.',
                }, 'main-submit')
            else:
                message = AIMessage(content='Main workflow completed under its original task budget.')
        message.usage_metadata = {'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 8}
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def action(name, args, identity):
        return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': identity}])


class LocalWorkerQuotaModel(QuotaModel):
    payload: dict = Field(default_factory=dict)

    def invoke_controlled(self, messages, session, stop=None, **kwargs):
        if hasattr(session, '_root'):
            return run_request_worker(self.payload, session, call_timeout=5, command=[
                sys.executable, '-c',
                "import json,os,sys,time; p=json.load(sys.stdin); "
                "open(p['pid'],'w').write(str(os.getpid())); time.sleep(5); "
                "open(p['late'],'w').write('finished'); print('{}')",
            ])
        return super().invoke_controlled(messages, session, stop=stop, **kwargs)


class DiagnosticSubbudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = passing_baseline(self.root)

    def session(self, **kwargs):
        return DiagnosticSession(self.baseline, self.root / 'diagnosis', {'map_exporter.py'},
                                 use_investigator=True, **kwargs)

    def run_policy(self, session, driver):
        original_started, original_deadline = session.started, session.deadline_seconds
        with patch.object(delegation, '_now', side_effect=lambda: time.monotonic() + driver.clock_offset), \
                patch('repair_workbench.performance.verify_performance_candidate', return_value={
                    'status': 'passed', 'reason': 'passed_performance_scope', 'cleanup_ok': True,
                    'scope': 'Offline mechanism receipt, not a measured optimization.',
                }) as verify:
            result = run_agent(session, QuotaModel(driver=driver), 'Diagnose and preserve export behavior.')
        self.assertEqual(session.started, original_started)
        self.assertEqual(session.deadline_seconds, original_deadline)
        return result, verify

    def assert_main_completed(self, session, result, verify):
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(session.attempts), 1)
        self.assertFalse(result['investigator_budget_policy']['token_hard_limit'])
        self.assertIsNone(delegation._ACTIVE_INVESTIGATOR.get())

    def test_child_model_loop_is_capped_by_remaining_root_quota_and_main_reserve(self):
        session = self.session(max_model_calls=6, main_model_reserve=3,
                               investigator_max_model_calls=30)
        driver = QuotaDriver()
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        record = session.delegation_history[0]
        self.assertEqual(record['budget']['max_model_calls'], 2)
        self.assertEqual(record['model_calls_used'], 2)
        self.assertEqual(driver.child_calls, 2)
        self.assertEqual(session.model_calls, 6)
        self.assertEqual(record['stop_reason'], 'investigator_model_call_limit')
        self.assertEqual(driver.main_task_receipts[0]['status'], 'delegation_limited')
        self.assertTrue(driver.main_task_receipts[0]['main_can_continue'])

    def test_child_request_cap_is_checked_before_a_second_provider_entry(self):
        session = self.session(max_model_calls=8, investigator_max_model_calls=1)
        driver = QuotaDriver()
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        self.assertEqual(driver.child_calls, 1)
        self.assertEqual(len(driver.child_views), 1)
        self.assertEqual(session.delegation_history[0]['model_calls_used'], 1)
        self.assertLess(session.model_calls, session.max_model_calls)

    def test_child_tool_batch_cannot_consume_main_tool_reserve(self):
        session = self.session(max_model_calls=8, investigator_max_tools=12)
        session.tool_calls = 54  # Simulate prior admitted task work; global limit stays 60.
        driver = QuotaDriver('tools')
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        record = session.delegation_history[0]
        self.assertEqual(record['budget']['max_tools'], 1)
        self.assertEqual(record['tools_admitted'], 1)
        self.assertEqual(record['tool_calls_used'], 1)
        self.assertEqual(record['tool_calls_after'], 56)
        self.assertEqual(record['stop_reason'], 'investigator_tool_limit')
        self.assertEqual(session.tool_calls, 58)

    def test_insufficient_tool_headroom_declines_without_starting_child(self):
        session = self.session(max_model_calls=8)
        session.tool_calls = 55
        driver = QuotaDriver()
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        record = session.delegation_history[0]
        self.assertEqual(record['status'], 'delegation_declined')
        self.assertEqual(record['stop_reason'], 'main_tools_reserve')
        self.assertEqual(record['model_calls_used'], 0)
        self.assertEqual(driver.child_calls, 0)

    def test_insufficient_time_headroom_declines_without_starting_child(self):
        session = self.session(max_model_calls=8, deadline_seconds=20,
                               main_time_reserve_seconds=20)
        driver = QuotaDriver()
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        self.assertEqual(driver.child_calls, 0)
        self.assertEqual(session.delegation_history[0]['stop_reason'], 'main_seconds_reserve')

    def test_child_deadline_after_response_is_recoverable_and_preserves_main_time(self):
        session = self.session(max_model_calls=8, investigator_timeout_seconds=.1)
        driver = QuotaDriver('time_after_provider')
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        record = session.delegation_history[0]
        self.assertEqual(record['stop_reason'], 'investigator_deadline_exceeded')
        self.assertEqual(record['model_calls_used'], 1)
        self.assertEqual(record['tool_calls_used'], 0)
        self.assertGreaterEqual(record['delegation_elapsed_seconds'], .1)
        self.assertEqual(driver.child_views[0]['deadline_seconds'], .1)
        self.assertEqual(driver.child_views[0]['root_deadline_seconds'], 180)

    def test_controlled_provider_checks_child_deadline_before_delayed_entry(self):
        session = self.session(max_model_calls=8, investigator_timeout_seconds=.1)
        driver = QuotaDriver('expired_before_provider')
        result, verify = self.run_policy(session, driver)
        self.assert_main_completed(session, result, verify)
        self.assertEqual(driver.child_calls, 0)
        self.assertEqual(len(driver.child_views), 1)
        self.assertEqual(session.delegation_history[0]['stop_reason'], 'investigator_deadline_exceeded')
        # One admitted request produced no response; never count unknown usage as zero.
        self.assertEqual(result['usage_missing_calls'], 1)
        self.assertFalse(result['usage_complete'])

    def test_child_deadline_reaps_owned_request_process_and_main_can_continue(self):
        session = self.session(max_model_calls=8, investigator_timeout_seconds=.2)
        driver = QuotaDriver()
        pid_file, late_file = self.root / 'local-worker.pid', self.root / 'late-worker-result'
        model = LocalWorkerQuotaModel(driver=driver, payload={'pid': str(pid_file), 'late': str(late_file)})
        with patch('repair_workbench.performance.verify_performance_candidate', return_value={
                'status': 'passed', 'reason': 'passed_performance_scope', 'cleanup_ok': True,
        }) as verify:
            result = run_agent(session, model, 'Diagnose and preserve export behavior.')
        self.assert_main_completed(session, result, verify)
        self.assertEqual(session.delegation_history[0]['stop_reason'], 'investigator_deadline_exceeded')
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(late_file.exists())
        events = [json.loads(line) for line in (session.output / 'events.jsonl').read_text().splitlines()]
        cleaned = [event for event in events if event['event'] == 'model_worker_finished']
        self.assertEqual(len(cleaned), 1)
        self.assertTrue(cleaned[0]['cleanup_ok'])
        self.assertEqual(cleaned[0]['agent_role'], 'investigator')
        self.assertEqual(result['usage_missing_calls'], 1)

    def test_root_global_model_exhaustion_during_child_remains_a_hard_stop(self):
        session = self.session(max_model_calls=8)
        driver = QuotaDriver()
        driver.child_hook = lambda: setattr(session, 'model_calls', session.max_model_calls)
        result, verify = self.run_policy(session, driver)
        self.assertEqual(result['stop_reason'], 'model_call_limit', result)
        self.assertEqual(session.delegation_history[0]['status'], 'stopped')
        self.assertEqual(session.delegation_history[0]['stop_reason'], 'model_call_limit')
        verify.assert_not_called()

    def test_root_cancel_has_priority_over_child_local_quota(self):
        flag = [False]
        session = self.session(max_model_calls=8, cancellation_check=lambda: flag[0],
                               investigator_timeout_seconds=.1)
        driver = QuotaDriver('time_after_provider')
        driver.child_hook = lambda: flag.__setitem__(0, True)
        result, verify = self.run_policy(session, driver)
        self.assertEqual(result['stop_reason'], 'cancel_requested', result)
        self.assertEqual(session.delegation_history[0]['status'], 'stopped')
        verify.assert_not_called()

    def test_strict_subbudget_configuration_rejects_invalid_or_incompatible_values(self):
        cases = (
            {'investigator_max_model_calls': True}, {'investigator_max_model_calls': '3'},
            {'investigator_max_tools': 0}, {'investigator_max_tools': 61},
            {'investigator_timeout_seconds': float('nan')}, {'investigator_timeout_seconds': True},
            {'main_model_reserve': 0}, {'main_tool_reserve': 60},
            {'main_time_reserve_seconds': float('inf')},
            {'max_model_calls': True}, {'max_model_calls': '8'},
            {'deadline_seconds': True},
            {'main_model_reserve': 3, 'max_model_calls': 2},
            {'main_time_reserve_seconds': 11, 'deadline_seconds': 10},
        )
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.session(**options)

    def test_default_single_agent_small_budgets_do_not_activate_new_reserves(self):
        session = DiagnosticSession(self.baseline, self.root / 'small-single', {'map_exporter.py'},
                                    max_model_calls=1, deadline_seconds=1)
        self.assertFalse(session.use_investigator)
        session.reserve_model()
        with self.assertRaisesRegex(BudgetExceeded, 'model_call_limit'):
            session.reserve_model()
        with self.assertRaises(ValueError):
            RepairSession(self.baseline, self.root / 'legacy', {'map_exporter.py'})


if __name__ == '__main__':
    unittest.main()
