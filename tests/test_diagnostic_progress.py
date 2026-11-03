"""Offline diagnostic progress control; no model API or optimization scores."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field
    from deepagents import create_deep_agent
    from langchain.agents.middleware import AgentMiddleware
    from repair_workbench.agent import run_agent
except ImportError:
    raise unittest.SkipTest('optional Agent dependencies are not installed')
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.diagnostic_progress import observe_tool_round
from repair_workbench.session import BudgetExceeded, RepairSession
from test_diagnosis import passing_baseline
from test_diagnosis import probe_report
from test_agent import fixture as repair_baseline


class RepeatedReadModel(BaseChatModel):
    calls: int = 0
    references: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return 'offline-repeated-read'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.name == 'read_source':
                data = json.loads(message.content)
                self.references.append((data['source_ref'], data['evidence_ref']))
                break
        self.calls += 1
        response = AIMessage(content='', tool_calls=[{
            'name': 'read_source', 'args': {'path': 'map_exporter.py'}, 'id': f'read-{self.calls}',
        }], usage_metadata={'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3})
        return ChatResult(generations=[ChatGeneration(message=response)])


class PatternModel(BaseChatModel):
    mode: str = 'read'
    calls: int = 0
    references: list = Field(default_factory=list)
    stop_after: int = 9
    recover: bool = False
    recovered: bool = False
    cancellation: object = Field(default=None, exclude=True)
    path: str = 'map_exporter.py'

    @property
    def _llm_type(self):
        return 'offline-progress-pattern'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        warning = any(isinstance(message, HumanMessage) and '连续三轮重复' in str(message.content)
                      for message in messages)
        if self.cancellation is not None and self.calls == 5:
            self.cancellation()
        if self.calls > self.stop_after or self.recovered:
            response = AIMessage(content='Offline policy ended without claiming an optimization.')
        else:
            name, arguments = 'read_source', {'path': self.path}
            if self.recover and warning:
                arguments.update(start_line=2, line_count=1)
                self.recovered = True
            elif self.mode == 'alternating':
                arguments.update(start_line=1 + self.calls % 2, line_count=1)
            elif self.mode == 'probe':
                name, arguments = 'run_probe', {'probe': 'cpu'}
            elif self.mode == 'hypothesis':
                name, arguments = 'record_hypothesis', {
                    'claim': f'Different wording {self.calls}', 'next_action': f'Rephrased plan {self.calls}',
                }
            elif self.mode == 'negative_search':
                name, arguments = 'search_source', {'query': f'absent-literal-{self.calls}'}
            elif self.mode == 'recall' and self.calls > 1:
                previous = next(message for message in reversed(messages) if isinstance(message, ToolMessage))
                name, arguments = 'read_evidence', {'evidence_ref': json.loads(previous.content)['evidence_ref']}
            response = AIMessage(content='', tool_calls=[{
                'name': name, 'args': arguments, 'id': f'pattern-{self.calls}',
            }])
        response.usage_metadata = {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3}
        return ChatResult(generations=[ChatGeneration(message=response)])


class LatestRoundOnly(AgentMiddleware):
    """Test-only compaction: previous observations truly leave the model request."""
    def wrap_model_call(self, request, handler):
        indices = [index for index, message in enumerate(request.messages)
                   if isinstance(message, AIMessage) and message.tool_calls]
        return handler(request.override(messages=request.messages[indices[-1]:])) if indices else handler(request)


class ChildLoopModel(BaseChatModel):
    main_calls: int = 0
    child_calls: int = 0

    @property
    def _llm_type(self):
        return 'offline-progress-child-isolation'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any('你是只读代码调查子Agent' in str(message.content) for message in messages):
            self.child_calls += 1
            name, arguments, identity = 'read_source', {'path': 'map_exporter.py'}, f'child-{self.child_calls}'
        else:
            self.main_calls += 1
            if self.main_calls > 2:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='Main continued.'))])
            name, arguments, identity = 'task', {
                'description': 'Read the source and return cited findings.', 'subagent_type': 'investigator',
            }, f'delegation-{self.main_calls}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[{
            'name': name, 'args': arguments, 'id': identity,
        }]))])


class DelegatedRecoveryModel(BaseChatModel):
    child_calls: int = 0
    task_sent: bool = False
    recalled: bool = False
    calls: int = 0

    @property
    def _llm_type(self):
        return 'offline-delegated-progress-recovery'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        child = any('你是只读代码调查子Agent' in str(message.content) for message in messages)
        if child:
            self.child_calls += 1
            reads = [json.loads(message.content) for message in messages
                     if isinstance(message, ToolMessage) and message.name == 'read_source']
            if reads:
                read = reads[-1]
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps({
                    'summary': 'Read-only source findings need main review.',
                    'evidence_refs': [read['evidence_ref']], 'source_refs': [read['source_ref']],
                    'limitations': 'Offline wiring only.',
                })))])
            name, arguments = 'read_source', {'path': 'map_exporter.py'}
        elif self.recalled:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='Main recovered after investigation.'))])
        elif self.task_sent:
            self.recalled = True
            name, arguments = 'read_source', {'path': 'map_exporter.py'}
        elif any(isinstance(message, HumanMessage) and '连续三轮重复' in str(message.content) for message in messages):
            self.task_sent = True
            name, arguments = 'task', {'description': 'Investigate export source and cite actual findings.',
                                      'subagent_type': 'investigator'}
        else:
            name, arguments = 'read_source', {'path': 'map_exporter.py'}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[{
            'name': name, 'args': arguments, 'id': f'delegated-recovery-{self.calls}',
        }]))])


class TwoTaskRecoveryModel(BaseChatModel):
    main_calls: int = 0
    child_calls: int = 0

    @property
    def _llm_type(self):
        return 'offline-same-round-task-recovery'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        child = any('你是只读代码调查子Agent' in str(message.content) for message in messages)
        if child:
            self.child_calls += 1
            valid = any('SECOND_VALID' in str(message.content) for message in messages)
            reads = [json.loads(message.content) for message in messages
                     if isinstance(message, ToolMessage) and message.name == 'read_source']
            if valid and reads:
                read = reads[-1]
                response = AIMessage(content=json.dumps({
                    'summary': 'A different source range was inspected.',
                    'evidence_refs': [read['evidence_ref']], 'source_refs': [read['source_ref']],
                    'limitations': 'Offline mechanism only.',
                }))
            else:
                args = {'path': 'map_exporter.py'}
                if valid:
                    args.update(start_line=2, line_count=1)
                response = AIMessage(content='', tool_calls=[{
                    'name': 'read_source', 'args': args, 'id': f'child-task-{self.child_calls}',
                }])
        else:
            self.main_calls += 1
            if self.main_calls == 5:
                response = AIMessage(content='', tool_calls=[{
                    'name': 'task', 'args': {'description': f'{marker}: investigate source using real citations.',
                                             'subagent_type': 'investigator'}, 'id': identity,
                } for marker, identity in (('FIRST_LOOP', 'first-task'), ('SECOND_VALID', 'second-task'))])
            elif self.main_calls <= 6:
                response = AIMessage(content='', tool_calls=[{
                    'name': 'read_source', 'args': {'path': 'map_exporter.py'}, 'id': f'main-{self.main_calls}',
                }])
            else:
                response = AIMessage(content='Main recovered after the second cited investigation.')
        return ChatResult(generations=[ChatGeneration(message=response)])


class DiagnosticProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = passing_baseline(self.root)

    def test_repeated_source_reads_with_new_references_stop_before_global_cap(self):
        session = DiagnosticSession(self.baseline, self.root / 'diagnosis', {'map_exporter.py'})
        model = RepeatedReadModel()
        result = run_agent(session, model, 'Inspect the exporter using source evidence.')
        self.assertEqual(result['stop_reason'], 'diagnostic_no_progress')
        self.assertEqual(model.calls, 5)
        self.assertEqual(len(set(model.references)), len(model.references))
        self.assertEqual(result['diagnostic_progress']['warning_count'], 1)
        self.assertTrue(result['diagnostic_progress']['stopped'])
        self.assertEqual(result['diagnostic_stop_reason'], 'diagnostic_no_progress')
        report = (session.output / result['diagnostic_progress']['report']).read_text()
        self.assertNotIn('def export_map', report)
        self.assertNotIn(model.references[0][0], report)
        self.assertTrue(all(len(item['fingerprint']) == 64 for row in json.loads(report)['records']
                            for item in row['observations']))

    def session(self, **kwargs):
        return DiagnosticSession(self.baseline, self.root / 'diagnosis', {'map_exporter.py'}, **kwargs)

    def test_warning_can_recover_with_new_range_and_reach_provider_again(self):
        session = self.session()
        model = PatternModel(recover=True)
        result = run_agent(session, model, 'Inspect the source.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(model.calls, 6)
        self.assertTrue(model.recovered)
        self.assertEqual(result['diagnostic_progress']['warning_count'], 1)
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 0)
        self.assertFalse(result['diagnostic_progress']['stopped'])

    def test_cached_probe_repetition_stops_without_new_physical_probes(self):
        session = self.session()
        model = PatternModel(mode='probe')
        with patch('repair_workbench.diagnostics.run_probe', return_value=probe_report('cpu')) as probe:
            result = run_agent(session, model, 'Inspect the registered CPU observation.')
        self.assertEqual(result['stop_reason'], 'diagnostic_no_progress')
        self.assertEqual(model.calls, 5)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(session.probe_runs, 1)

    def test_hypothesis_rewording_and_new_ids_cannot_reset_progress(self):
        session = self.session()
        model = PatternModel(mode='hypothesis')
        result = run_agent(session, model, 'Investigate the source.')
        self.assertEqual(result['stop_reason'], 'diagnostic_no_progress')
        self.assertEqual(model.calls, 5)
        self.assertEqual(len(session.hypotheses), 5)
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 4)

    def test_alternating_visible_read_a_b_cycle_is_detected(self):
        session = self.session()
        model = PatternModel(mode='alternating')
        result = run_agent(session, model, 'Inspect two source ranges.')
        self.assertEqual(result['stop_reason'], 'diagnostic_no_progress')
        self.assertEqual(model.calls, 6)

    def test_new_negative_searches_and_one_evidence_recall_are_allowed(self):
        for mode, length in (('negative_search', 5), ('recall', 2)):
            with self.subTest(mode=mode):
                output = self.root / mode
                session = DiagnosticSession(self.baseline, output, {'map_exporter.py'})
                model = PatternModel(mode=mode, stop_after=length)
                result = run_agent(session, model, 'Inspect available evidence.')
                self.assertEqual(result['stop_reason'], 'agent_finished', result)
                self.assertFalse(result['diagnostic_progress']['stopped'])

    def test_compacted_observations_no_longer_visible_do_not_penalize_rereads(self):
        session = self.session()
        model = PatternModel(stop_after=7)

        def compact(*args, **kwargs):
            return create_deep_agent(*args, **{**kwargs,
                'middleware': [*kwargs.get('middleware', []), LatestRoundOnly()]})

        with patch('repair_workbench.diagnostic_agent.create_deep_agent', side_effect=compact):
            result = run_agent(session, model, 'Inspect the source after compaction.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(model.calls, 8)
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 0)

    def test_root_cancel_wins_over_the_next_no_progress_stop(self):
        cancelled = [False]
        session = self.session(cancellation_check=lambda: cancelled[0])
        model = PatternModel(cancellation=lambda: cancelled.__setitem__(0, True))
        result = run_agent(session, model, 'Inspect the source.')
        self.assertEqual(result['stop_reason'], 'cancel_requested', result)
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 3)
        self.assertFalse(result['diagnostic_progress']['stopped'])

    def test_two_child_dispatches_reset_local_sticky_stop_without_polluting_main(self):
        session = self.session(use_investigator=True, max_delegations=2,
                               max_model_calls=20, investigator_max_model_calls=8)
        model = ChildLoopModel()
        result = run_agent(session, model, 'Delegate two bounded source investigations.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(model.child_calls, 10)
        self.assertEqual(model.main_calls, 3)
        self.assertEqual(len(session.delegation_history), 2)
        for record in session.delegation_history:
            self.assertEqual(record['status'], 'delegation_limited')
            self.assertEqual(record['stop_reason'], 'investigator_no_progress')
            self.assertEqual(record['progress']['warning_count'], 1)
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 0)
        self.assertIsNone(result['diagnostic_stop_reason'])

    def test_legitimate_investigator_receipt_breaks_main_streak_before_reasonable_recall(self):
        session = self.session(use_investigator=True)
        model = DelegatedRecoveryModel()
        result = run_agent(session, model, 'Investigate the source and review cited findings.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(model.child_calls, 2)
        self.assertTrue(model.recalled)
        self.assertEqual(result['diagnostic_progress']['warning_count'], 1)
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 1)
        self.assertEqual(session.delegation_history[0]['status'], 'investigator_proposal')

    def test_second_task_in_same_parent_round_can_restore_main_after_first_child_limited(self):
        session = self.session(use_investigator=True, max_delegations=2,
                               max_model_calls=30, investigator_max_model_calls=8)
        model = TwoTaskRecoveryModel()
        result = run_agent(session, model, 'Investigate the source using bounded cited child tasks.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(model.main_calls, 7)
        self.assertEqual(model.child_calls, 7)
        records = session.delegation_history
        self.assertEqual([record['status'] for record in records],
                         ['delegation_limited', 'investigator_proposal'])
        self.assertEqual([record['model_calls_before'] for record in records], [5, 10])
        self.assertEqual([record['parent_model_round'] for record in records], [5, 5])
        self.assertNotEqual(records[0]['tool_call_id_sha256'], records[1]['tool_call_id_sha256'])
        self.assertEqual(result['diagnostic_progress']['no_progress_streak'], 1)

    def test_mixed_round_new_source_resets_streak_and_schema_round_is_not_reconsumed(self):
        session = self.session()
        messages = []

        def read_round(number, ranges):
            calls, replies = [], []
            for index, line in enumerate(ranges):
                call = {'name': 'read_source', 'args': {'path': 'map_exporter.py', 'start_line': line,
                                                       'line_count': 1}, 'id': f'mixed-{number}-{index}'}
                calls.append(call)
                replies.append(ToolMessage(json.dumps(session.read_source(
                    'map_exporter.py', start_line=line, line_count=1, raw=True)),
                    tool_call_id=call['id'], name='read_source'))
            messages.extend([AIMessage(content='', tool_calls=calls), *replies])
            session.active_tool_round = {'call': number, 'ids': {call['id']: call['name'] for call in calls}}
            return observe_tool_round(session, messages)

        for number in range(1, 5):
            read_round(number, [1])
        self.assertEqual(session.diagnostic_progress['no_progress_streak'], 3)
        read_round(5, [1, 2])
        self.assertEqual(session.diagnostic_progress['no_progress_streak'], 0)
        self.assertTrue(session.diagnostic_progress['records'][-1]['new_observation'])
        self.assertTrue(session.diagnostic_progress['records'][-1]['visible_repeat'])
        call = {'name': 'read_source', 'args': {'path': 'map_exporter.py', 'start_line': 'invalid'},
                'id': 'schema-error'}
        messages.extend([AIMessage(content='', tool_calls=[call]), ToolMessage(
            'start_line: Input should be a valid integer', tool_call_id=call['id'],
            name='read_source', status='error')])
        session.active_tool_round = {'call': 6, 'ids': {call['id']: call['name']}}
        counted = session.diagnostic_progress['rounds_observed']
        observe_tool_round(session, messages)
        observe_tool_round(session, messages)
        self.assertEqual(session.diagnostic_progress['rounds_observed'], counted)

    def test_one_complete_round_is_consumed_once_and_sticky_stop_blocks_direct_operations(self):
        session = self.session()
        messages = []
        for number in range(1, 6):
            call = {'name': 'read_source', 'args': {'path': 'map_exporter.py'}, 'id': f'round-{number}'}
            response = session.read_source('map_exporter.py', raw=True)
            messages.extend([AIMessage(content='', tool_calls=[call]), ToolMessage(
                json.dumps(response), tool_call_id=call['id'], name='read_source')])
            session.active_tool_round = {'call': number, 'ids': {call['id']: call['name']}}
            if number == 5:
                with self.assertRaisesRegex(BudgetExceeded, 'diagnostic_no_progress'):
                    observe_tool_round(session, messages)
            else:
                observe_tool_round(session, messages)
                counted = session.diagnostic_progress['rounds_observed']
                self.assertIsNone(observe_tool_round(session, messages))
                self.assertEqual(session.diagnostic_progress['rounds_observed'], counted)
        for operation in (lambda: session.reserve_model(),
                          lambda: session.read_source('map_exporter.py'),
                          lambda: session.submit_candidate({'map_exporter.py': 'x=1\n'}, 'late')):
            with self.assertRaisesRegex(BudgetExceeded, 'diagnostic_no_progress'):
                operation()

    def test_legacy_repair_session_does_not_enable_progress_policy(self):
        directory = self.root / 'legacy'
        directory.mkdir()
        baseline = repair_baseline(directory)
        session = RepairSession(baseline, directory / 'agent', {'pricing.py'}, max_model_calls=7)
        result = run_agent(session, PatternModel(path='pricing.py'), 'Inspect the pricing source.')
        self.assertEqual(result['stop_reason'], 'model_call_limit', result)
        self.assertNotIn('diagnostic_progress', result)


if __name__ == '__main__':
    unittest.main()
