"""Offline checks for the live pilot's hard quota and controlled intervention."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from repair_workbench.public_live_evaluation import (
    TokenLedger, CONTEXT_RESERVATION, MeteredTongyi, PilotSession, validate_plan)
from repair_workbench.model_transport import ManagedTongyi, ProviderRequestError
from repair_workbench.provider_worker import classify_error, request
from repair_workbench.session import BudgetExceeded
from test_agent import fixture


class PublicPilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'ledger.json'
        self.ledger = TokenLedger(self.path, 'frozen')

    def message(self, inputs=100, outputs=20):
        return AIMessage(content='done', usage_metadata={
            'input_tokens': inputs, 'output_tokens': outputs, 'total_tokens': inputs + outputs})

    def test_reservation_is_durable_before_provider_send_and_settles_actuals(self):
        def provider(*args, **kwargs):
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved['calls'][0]['charged_tokens'], CONTEXT_RESERVATION)
            self.assertEqual(saved['calls'][0]['state'], 'reserved')
            return self.message()
        model = self.model()
        with patch.object(ManagedTongyi, 'invoke_controlled', side_effect=provider):
            model.invoke_controlled([HumanMessage(content='public task')], object())
        self.assertEqual(self.ledger.charged(), 120)
        self.assertEqual(json.loads(self.path.read_text())['calls'][0]['state'], 'settled')
        for trace in self.root.glob('call-*.json'):
            self.assertNotIn('offline-private-value', trace.read_text())

    def model(self, validate=lambda: None):
        return MeteredTongyi(api_key='offline-private-value', model_name='qwen-max',
            run_id='run-a', trace_dir=self.root, ledger=self.ledger, validate_frozen=validate,
            sampling_temperature=0)

    def test_bind_tools_preserves_one_shared_ledger(self):
        bound = self.model().bind_tools([{'name': 'read_source', 'description': 'read',
            'parameters': {'type': 'object', 'properties': {}}}])
        self.assertIs(bound.ledger, self.ledger)

    def test_incomplete_usage_is_reserved_and_blocks_all_following_calls(self):
        with patch.object(ManagedTongyi, 'invoke_controlled', return_value=AIMessage(content='unknown')) as provider:
            with self.assertRaisesRegex(BudgetExceeded, 'batch_usage_unknown'):
                self.model().invoke_controlled([], object())
            with self.assertRaisesRegex(BudgetExceeded, 'batch_usage_unknown'):
                self.model().invoke_controlled([], object())
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(self.ledger.charged(), CONTEXT_RESERVATION)
        self.assertEqual(self.ledger.state['calls'][0]['input_tokens'], None)

    def test_failed_send_keeps_reservation_without_retry(self):
        with patch.object(ManagedTongyi, 'invoke_controlled',
                          side_effect=ProviderRequestError('free_quota_exhausted')) as provider:
            with self.assertRaisesRegex(ProviderRequestError, 'free_quota_exhausted'):
                self.model().invoke_controlled([], object())
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(self.ledger.state['blocked'], 'batch_usage_unknown')
        self.assertEqual(self.ledger.charged(), CONTEXT_RESERVATION)

    def test_batch_and_run_caps_reject_before_reservation(self):
        for scope in ['batch_tokens', 'run_tokens']:
            with self.subTest(scope=scope):
                caps = {'batch_tokens': 300000, 'run_tokens': 100000}
                caps[scope] = CONTEXT_RESERVATION - 1
                ledger = TokenLedger(self.root / (scope + '.json'), 'frozen', caps)
                with self.assertRaisesRegex(BudgetExceeded,
                    'batch_token_limit' if scope == 'batch_tokens' else 'run_token_limit'):
                    ledger.reserve('x')
                self.assertEqual(ledger.state['calls'], [])

    def test_complete_usage_releases_unused_reservation_and_rejects_native_overrun(self):
        first = self.ledger.reserve('x')
        self.ledger.settle(first, self.message(200, 30))
        self.assertEqual(self.ledger.charged(), 230)
        second = self.ledger.reserve('x')
        with self.assertRaisesRegex(BudgetExceeded, 'batch_usage_unknown'):
            self.ledger.settle(second, self.message(30721, 30))
        self.assertEqual(self.ledger.charged(), 230 + CONTEXT_RESERVATION)

    def test_existing_ledger_cannot_be_reset(self):
        with self.assertRaises(FileExistsError):
            TokenLedger(self.path, 'another-plan')

    def test_drift_prevents_even_reserving_a_provider_request(self):
        def drift():
            raise ValueError('frozen evidence drift')
        with patch.object(ManagedTongyi, 'invoke_controlled') as provider:
            with self.assertRaisesRegex(ValueError, 'drift'):
                self.model(drift).invoke_controlled([], object())
        self.assertEqual(self.ledger.state['calls'], [])
        provider.assert_not_called()

    def test_variants_keep_identical_tool_catalog_prompt_and_edit_protocol(self):
        from repair_workbench.agent import build_agent
        baseline = fixture(self.root)
        calls = []
        with patch('repair_workbench.agent.create_deep_agent', side_effect=lambda **kw: calls.append(kw)):
            for i, variant in enumerate(['on_demand', 'failure_context']):
                session = PilotSession(baseline, self.root / ('s%d' % i), {'pricing.py'}, variant=variant)
                build_agent(session, self.model())
        self.assertEqual(calls[0]['system_prompt'], calls[1]['system_prompt'])
        self.assertEqual([t.name for t in calls[0]['tools']], [t.name for t in calls[1]['tools']])
        self.assertIn('submit_ref_edits', [t.name for t in calls[0]['tools']])

    def test_provider_free_only_error_is_distinct_without_exposing_response(self):
        self.assertEqual(classify_error(ValueError('status_code: 403, code: AllocationQuota.FreeTierOnly')),
                         'free_quota_exhausted')

    def test_provider_explicit_sampling_options_and_invalid_options(self):
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.invoke.return_value = self.message()
        payload = {'model': 'qwen-max', 'api_key': 'offline-private-value',
                   'tools': [], 'messages': [], 'stop': None,
                   'connect_timeout': 5, 'read_timeout': 40,
                   'provider_options': {'max_tokens': 4096, 'temperature': 0}}
        with patch('langchain_community.chat_models.ChatTongyi', return_value=fake) as constructor:
            request(payload)
        self.assertEqual(constructor.call_args.kwargs['model_kwargs']['temperature'], 0)
        self.assertEqual(constructor.call_args.kwargs['max_retries'], 0)
        payload['provider_options']['max_tokens'] = True
        with patch('langchain_community.chat_models.ChatTongyi') as constructor:
            with self.assertRaises(ValueError):
                request(payload)
        constructor.assert_not_called()

    def test_plan_validation_rejects_extra_fields_or_changed_evidence(self):
        with patch('repair_workbench.public_live_evaluation.make_plan', return_value={'qualification': 'q', 'x': 1}):
            validate_plan({'qualification': 'q', 'x': 1})
            with self.assertRaises(ValueError):
                validate_plan({'qualification': 'q', 'x': 2})

    def test_actual_public_qualification_and_plan_bind_exact_dataset(self):
        from repair_workbench.public_live_evaluation import make_plan, ROOT
        q = ROOT / 'runtime/bugsinpy-qualification-20261101-v2'
        if not q.exists():
            self.skipTest('local public-task qualification evidence absent')
        plan = make_plan(q)
        self.assertEqual(len(plan['schedule']), 12)
        self.assertEqual(plan['limits']['batch_tokens'], 300000)
        validate_plan(plan)
        plan['limits'] = {**plan['limits'], 'batch_tokens': 400000}
        with self.assertRaises(ValueError):
            validate_plan(plan)


if __name__ == '__main__':
    unittest.main()
