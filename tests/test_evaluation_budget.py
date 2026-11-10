"""Quota, persistence and failure tests without provider requests."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from langchain_core.messages import AIMessage
from repair_workbench.evaluation_budget import EvaluationLedger
from repair_workbench.evaluation_model import MeteredEvaluationTongyi
from repair_workbench.model_transport import ManagedTongyi
from repair_workbench.session import BudgetExceeded


class EvaluationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def ledger(self, name='ledger', **kwargs):
        limits = kwargs.pop('limits', {'batch_tokens': 10000, 'run_tokens': 5000})
        return EvaluationLedger(self.root / (name + '.json'), 'plan-v1', limits,
                                input_token_limit=kwargs.pop('input_token_limit', 100),
                                output_token_limit=kwargs.pop('output_token_limit', 50), **kwargs)

    @staticmethod
    def message(**usage):
        return SimpleNamespace(usage_metadata=usage, response_metadata={})

    def test_bounds_and_caps_are_explicit_strict_and_defensively_copied(self):
        for bad in (True, 0, -1, 1.5, '100', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.ledger(input_token_limit=bad)
        limits = {'batch_tokens': 10000, 'run_tokens': 5000}
        ledger = self.ledger(limits=limits)
        limits['run_tokens'] = 1
        ledger.limits['run_tokens'] = 1
        ledger.state['blocked'] = 'fake'
        self.assertEqual(ledger.reserve('a')['charged_tokens'], 150)
        self.assertEqual(ledger.state['limits']['run_tokens'], 5000)

    def test_changed_model_bounds_change_real_reservation_and_usage_validation(self):
        ledger = self.ledger(input_token_limit=250, output_token_limit=70)
        call = ledger.reserve('a', role='reproducer')
        self.assertEqual(json.loads(ledger.path.read_text())['calls'][0]['charged_tokens'], 320)
        ledger.settle(call, self.message(input_tokens=200, output_tokens=60, total_tokens=260))
        self.assertEqual(ledger.charged(), 260)
        self.assertEqual(ledger.state['calls'][0]['role'], 'reproducer')

    def test_roles_share_run_and_batch_tokens(self):
        ledger = self.ledger(limits={'batch_tokens': 500, 'run_tokens': 250})
        ledger.reserve('a', role='main')
        with self.assertRaisesRegex(BudgetExceeded, 'run_token_limit'):
            ledger.reserve('a', role='investigator')
        ledger.reserve('b')
        ledger.reserve('c')
        with self.assertRaisesRegex(BudgetExceeded, 'batch_token_limit'):
            ledger.reserve('d')
        self.assertEqual(len(ledger.state['calls']), 3)

    def test_role_calls_share_request_caps_even_with_zero_usage(self):
        ledger = self.ledger(limits={'batch_tokens': 10000, 'run_tokens': 5000,
                                    'requests_per_run': 2, 'batch_requests': 3})
        for role in ('main', 'reproducer'):
            ledger.settle(ledger.reserve('a', role=role), self.message(input_tokens=0, output_tokens=0))
        with self.assertRaisesRegex(BudgetExceeded, 'run_request_limit'):
            ledger.reserve('a', role='investigator')
        ledger.reserve('b')
        with self.assertRaisesRegex(BudgetExceeded, 'batch_request_limit'):
            ledger.reserve('c')

    def test_concurrent_roles_cannot_overreserve(self):
        ledger = self.ledger(limits={'batch_tokens': 1500, 'run_tokens': 1500})
        def attempt(index):
            try:
                ledger.reserve('shared', role='child-%d' % index)
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(30))), 10)
        saved = json.loads(ledger.path.read_text())
        self.assertEqual([c['index'] for c in saved['calls']], list(range(1, 11)))
        self.assertEqual(ledger.charged(), 1500)

    def test_incomplete_inconsistent_and_over_bound_usage_stops_every_run(self):
        cases = ({}, {'input_tokens': True, 'output_tokens': 2},
                 {'input_tokens': 2, 'output_tokens': -1},
                 {'input_tokens': 101, 'output_tokens': 2},
                 {'input_tokens': 1, 'output_tokens': 51},
                 {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 11})
        for index, usage in enumerate(cases):
            with self.subTest(usage=usage):
                ledger = self.ledger(name=str(index))
                with self.assertRaisesRegex(BudgetExceeded, 'batch_usage_unknown'):
                    ledger.settle(ledger.reserve('a'), self.message(**usage))
                with self.assertRaisesRegex(BudgetExceeded, 'batch_usage_unknown'):
                    ledger.reserve('b')
                self.assertEqual(ledger.charged(), 150)
                self.assertIsNone(ledger.state['calls'][0]['input_tokens'])

    def test_usage_details_preserved_without_double_counting_cached_tokens(self):
        ledger = self.ledger()
        usage = {'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100,
                 'input_token_details': {'cache_read': 60},
                 'output_token_details': {'reasoning': 10}}
        call = ledger.reserve('a')
        ledger.settle(call, self.message(**usage))
        self.assertEqual(ledger.charged(), 100)
        self.assertEqual(ledger.state['calls'][0]['provider_usage'], usage)
        with self.assertRaises(ValueError):
            ledger.settle(call, self.message(input_tokens=0, output_tokens=0))
        with self.assertRaises(ValueError):
            ledger.settle(dict(call), self.message(input_tokens=0, output_tokens=0))

    def test_existing_and_symlinked_ledgers_rejected(self):
        ledger = self.ledger()
        self.assertEqual(ledger.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.ledger()
        linked = self.root / 'linked.json'
        linked.symlink_to(ledger.path)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            self.ledger(name='linked')

    def test_process_owner_change_cannot_send(self):
        ledger = self.ledger()
        with patch('repair_workbench.evaluation_budget.os.getpid', return_value=os.getpid() + 1):
            with self.assertRaisesRegex(RuntimeError, 'owner_changed'):
                ledger.reserve('a')
        self.assertEqual(ledger.state['calls'], [])

    def test_metered_transport_checks_configuration_before_provider_send(self):
        ledger = self.ledger(output_token_limit=4096)
        model = MeteredEvaluationTongyi(api_key='offline-private-value', model_name='offline-test',
            run_id='run-a', trace_dir=self.root, ledger=ledger, validate_frozen=lambda: None,
            max_output_tokens=4097)
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            with self.assertRaisesRegex(ValueError, 'reserved_bound'):
                model.invoke_controlled([], object())
        provider.assert_not_called()
        self.assertEqual(ledger.state['calls'], [])

    def test_trace_failure_blocks_batch_before_provider_execution(self):
        ledger = self.ledger(output_token_limit=4096)
        model = MeteredEvaluationTongyi(api_key='offline-private-value', model_name='offline-test',
            run_id='a', trace_dir=self.root / 'missing', ledger=ledger, validate_frozen=lambda: None)
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            with self.assertRaises(FileNotFoundError):
                model.invoke_controlled([], object())
        provider.assert_not_called()
        self.assertEqual(ledger.state['blocked'], 'batch_usage_unknown')
        self.assertEqual(ledger.charged(), 4196)

    def test_failed_persistence_prevents_all_following_reservations(self):
        ledger = self.ledger()
        with patch('repair_workbench.evaluation_budget._atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                ledger.reserve('a')
        with self.assertRaisesRegex(BudgetExceeded, 'ledger_persistence_failure'):
            ledger.reserve('b')
        self.assertEqual(ledger.charged(), 150)

    def test_provider_endpoint_is_explicit_and_restored_without_network(self):
        from unittest.mock import MagicMock
        import dashscope
        from repair_workbench.provider_worker import request
        fake = MagicMock()
        fake.invoke.side_effect = lambda *a, **k: (
            self.assertEqual(dashscope.base_http_api_url, 'https://dashscope.aliyuncs.com/api/v1')
            or AIMessage(content='offline'))
        payload = {'model': 'offline', 'api_key': 'offline-private-value', 'tools': [],
                   'messages': [], 'stop': None, 'connect_timeout': 1, 'read_timeout': 1,
                   'provider_base_url': 'https://dashscope.aliyuncs.com/api/v1'}
        with patch.object(dashscope, 'base_http_api_url', 'https://unexpected.invalid'), \
             patch('langchain_community.chat_models.ChatTongyi', return_value=fake):
            request(payload)
            self.assertEqual(dashscope.base_http_api_url, 'https://unexpected.invalid')
        payload['provider_base_url'] = 'https://unexpected.invalid'
        with self.assertRaisesRegex(ValueError, 'unsupported explicit'):
            request(payload)


if __name__ == '__main__':
    unittest.main()
