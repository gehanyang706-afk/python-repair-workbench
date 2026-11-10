"""Issue retrieval changes model evidence, never authority or the evaluator."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from test_agent import ScriptedModel, call
from test_swebench_adapter import fixture_module
from repair_workbench.agent import run_agent
from repair_workbench.issue_context import MAX_CHARACTERS, MAX_EXCERPTS, MAX_QUERIES, issue_queries
from repair_workbench.swebench_adapter import (
    SWEbenchSession, import_dataset, prepare_workspace, run_qualified_agent, validate_qualification)


class CaptureIssueModel(ScriptedModel):
    payload: dict = Field(default_factory=dict)
    prompts: list = Field(default_factory=list)
    catalog: list = Field(default_factory=list)
    submit: bool = False

    def bind_tools(self, tools, **kwargs):
        self.catalog = [{'name': tool.name, 'description': tool.description,
                         'schema': tool.args_schema.model_json_schema()} for tool in tools]
        return super().bind_tools(tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.prompts = [message.content for message in messages if isinstance(message, SystemMessage)]
        self.payload = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        response = AIMessage(content='No final candidate proposed.')
        if self.submit:
            excerpt = self.payload['initial_context']['issue_evidence']['source_excerpts'][0]
            response = call('submit_ref_edits', {'edits': [{
                'source_ref': excerpt['source_ref'], 'old_text': 'return a - b',
                'new_text': 'return a + b'}], 'diagnosis': 'Preserve the requested addition behavior.'}, 'edit')
        return ChatResult(generations=[ChatGeneration(message=response)])


class IssueContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / 'fixture'
        self.row = fixture_module.prepare_fixture(self.fixture)
        self.workspace = self.fixture / 'agent-workspace'
        self.sequence = 0

    def session(self, policy='issue_evidence', **kwargs):
        self.sequence += 1
        return SWEbenchSession(self.workspace, self.root / f'agent-{self.sequence}',
                               swe_policy=policy, **kwargs)

    def test_large_permission_catalog_is_projected_and_paged_without_widening_authority(self):
        session = self.session(policy='on_demand')
        # Synthetic catalog tests prompt size and paging, not repository/model quality.
        paths = {'package/module_%03d.py' % index for index in range(400)}
        session.files |= paths
        session.allowed_paths = frozenset(set(session.allowed_paths) | paths)
        first = session.list_sources(limit=50)
        following = session.list_sources(offset=first['next_offset'], limit=50)
        self.assertLessEqual(len(first['allowed_paths']), 50)
        self.assertEqual(first['allowed_path_count'], 401)
        self.assertTrue(set(first['files']).isdisjoint(following['files']))
        with self.assertRaises(ValueError):
            session.list_sources(offset=True)
        model = CaptureIssueModel()
        result = run_agent(session, model, 'Inspect the public issue.')
        self.assertEqual(result['stop_reason'], 'agent_finished')
        self.assertEqual(len(model.payload['allowed_paths']), 32)
        self.assertTrue(model.payload['allowed_paths_may_be_truncated'])
        self.assertEqual(model.payload['allowed_path_count'], 401)
        self.assertEqual(len(session.allowed_paths), 401)

    def with_issue(self, issue, *, source=None, sentinels=False):
        row = {**self.row, 'problem_statement': issue}
        if source is not None:
            repo = self.fixture / 'repo'
            (repo / 'calculator.py').write_text(source)
            subprocess.run(['git', '-C', str(repo), 'add', 'calculator.py'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@localhost', '-c', 'commit.gpgsign=false',
                            '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Alternate public source'],
                           check=True, capture_output=True)
            row['base_commit'] = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
        if sentinels:
            row.update(patch='HIDDEN_GOLD_SENTINEL', test_patch='HIDDEN_TEST_SENTINEL',
                       hints_text='HIDDEN_HINT_SENTINEL', FAIL_TO_PASS=['HIDDEN_ACCEPTANCE_SENTINEL'])
        raw = self.root / 'public-task.json'
        raw.write_text(json.dumps([row]))
        dataset = self.root / 'private-data'
        import_dataset(raw, dataset, dataset_name='fixture', revision='1' * 40)
        self.workspace = self.root / 'public-workspace'
        prepare_workspace(dataset, row['instance_id'], self.fixture / 'repo', self.workspace)
        self.row = row

    def qualification(self, qualified=True):
        directory = self.root / 'qualification'
        directory.mkdir()
        results = {}
        for role, status in [('negative', 'unresolved'), ('gold', 'resolved' if qualified else 'unresolved')]:
            folder = directory / role
            folder.mkdir()
            (folder / 'source-binding.json').write_text(json.dumps({
                'upstream_commit': self.row['base_commit'], 'content_match': True, 'dirty_returncode': 0}))
            results[role] = {'status': status, 'instance_id': self.row['instance_id'], 'cleanup_ok': True,
                             'image_id': 'sha256:' + 'a' * 64, 'execution_policy': {'network': 'none'}}
        (directory / 'qualification.json').write_text(json.dumps({'qualified': qualified, **results}))
        return directory

    def test_default_policy_preserves_issue_only_input_without_prefetch(self):
        session = SWEbenchSession(self.workspace, self.root / 'default')
        packet = session.initial_context()
        self.assertEqual(packet['mode'], 'issue_only')
        self.assertFalse(packet['tests_executed'])
        self.assertNotIn('issue_evidence', packet)
        self.assertEqual(session.tool_calls, 0)
        self.assertEqual(session.save('completed')['swe_policy'], 'on_demand')

    def test_same_native_graph_catalog_and_system_prompt_only_initial_evidence_differs(self):
        models = [CaptureIssueModel(), CaptureIssueModel()]
        sessions = [self.session('on_demand'), self.session('issue_evidence')]
        for session, model in zip(sessions, models):
            run_agent(session, model, self.row['problem_statement'])
        self.assertTrue(models[0].prompts)
        self.assertEqual(models[0].prompts, models[1].prompts)
        self.assertEqual(models[0].catalog, models[1].catalog)
        self.assertIn('search_source', models[1].exposed)
        self.assertNotIn('read_test_log', models[1].exposed)
        initial = models[1].payload['initial_context']
        evidence = initial.pop('issue_evidence')
        self.assertEqual(evidence['status'], 'matched')
        self.assertEqual(models[0].payload, models[1].payload)
        self.assertEqual(sessions[0].tool_calls, 0)
        self.assertGreater(sessions[1].tool_calls, 0)

    def test_prefetched_source_ref_can_submit_through_existing_graph_tool(self):
        session = self.session()
        model = CaptureIssueModel(submit=True)
        result = run_agent(session, model, self.row['problem_statement'])
        self.assertEqual(result['status'], 'submitted', result)
        self.assertEqual(result['evaluation_status'], 'not_run')
        self.assertEqual(model.calls, 1)
        excerpts = model.payload['initial_context']['issue_evidence']['source_excerpts']
        for excerpt in excerpts:
            self.assertEqual(excerpt['content'], session.source_refs[excerpt['source_ref']]['content'])
        self.assertIn('return a + b', (session.output / 'attempt-01/verification/workspace/calculator.py').read_text())
        self.assertIn('return a - b', (session.baseline / 'workspace/calculator.py').read_text())

    def test_unmatched_issue_falls_back_without_inventing_source(self):
        self.with_issue('`unseen_symbol_one` and `unseen_symbol_two` behave unexpectedly.')
        session = self.session()
        packet = session.initial_context()['issue_evidence']
        self.assertEqual(packet['status'], 'no_match')
        self.assertEqual(packet['source_excerpts'], [])
        self.assertEqual(session.source_refs, {})
        self.assertGreater(session.tool_calls, 0)
        self.assertLessEqual(session.tool_calls, MAX_QUERIES)

    def test_substrings_are_not_injected_as_symbol_matches_but_reads_remain_charged(self):
        self.with_issue('`add`', source='def padding():\n    return 7\n')
        session = self.session()
        packet = session.initial_context()['issue_evidence']
        self.assertEqual(packet['source_excerpts'], [])
        self.assertEqual(session.tool_calls, 1)
        self.assertGreater(session.issue_context_stats['source_reads'], 0)
        self.assertGreater(session.issue_context_stats['retrieved_characters'], 0)

    def test_many_hits_are_bounded_and_all_prefetch_work_is_recorded(self):
        source = '\n'.join(f'def target_{i}():\n    return target_symbol\n' for i in range(16))
        self.with_issue('`target_symbol` produces an unexpected result.', source=source)
        session = self.session()
        packet = session.initial_context()['issue_evidence']
        stats = session.issue_context_stats
        self.assertLessEqual(len(packet['source_excerpts']), MAX_EXCERPTS)
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False)), MAX_CHARACTERS)
        self.assertLessEqual(stats['tool_calls'], MAX_QUERIES)
        self.assertEqual(stats['tool_calls'], session.tool_calls)
        self.assertEqual(stats['tool_calls'], session.tool_statistics['by_tool']['search_source']['calls'])
        self.assertEqual(stats['source_reads'], session.context_stats['reads'])
        self.assertGreater(stats['source_reads'], stats['excerpt_count'])
        self.assertGreaterEqual(stats['retrieved_characters'], stats['excerpt_characters'])
        self.assertTrue(stats['complete'])

    def test_no_private_fields_or_outside_paths_are_used_in_initial_input(self):
        self.with_issue('`add` produces the wrong result; inspect /outside/SECRET_EXTERNAL_NAME.py.', sentinels=True)
        (self.root / 'SECRET_EXTERNAL_NAME.py').write_text('HIDDEN_HOST_SENTINEL')
        model = CaptureIssueModel()
        with patch('repair_workbench.swebench_adapter.task_record', side_effect=AssertionError('private access')):
            run_agent(self.session(), model, self.row['problem_statement'])
        encoded = json.dumps(model.payload)
        self.assertNotIn('HIDDEN_', encoded)
        self.assertNotIn('test_patch', encoded)
        self.assertNotIn('FAIL_TO_PASS', encoded)

    def test_tool_exhaustion_stops_before_model_without_free_prefetch(self):
        self.with_issue('`unseen_first` and `unseen_second`')
        for used, admitted in [(60, 0), (59, 1)]:
            with self.subTest(used=used):
                session = self.session()
                session.tool_calls = used
                model = CaptureIssueModel()
                result = run_agent(session, model, self.row['problem_statement'])
                self.assertEqual(result['stop_reason'], 'tool_call_limit')
                self.assertEqual(session.tool_calls, 60)
                self.assertEqual(result['issue_context_stats']['tool_calls'], admitted)
                self.assertFalse(result['issue_context_stats']['complete'])
                self.assertEqual(model.calls, 0)

    def test_cancel_stops_before_model_and_retrieval(self):
        session = self.session(cancellation_check=lambda: True)
        model = CaptureIssueModel()
        result = run_agent(session, model, self.row['problem_statement'])
        self.assertEqual(result['stop_reason'], 'cancel_requested')
        self.assertEqual(session.tool_calls, 0)
        self.assertEqual(model.calls, 0)

    def test_qualified_entry_forwards_policy_without_acceptance_feedback(self):
        model = CaptureIssueModel(submit=True)
        result = run_qualified_agent(self.workspace, self.root / 'qualified-agent', model,
                                     self.qualification(), swe_policy='issue_evidence')
        self.assertEqual(result['swe_policy'], 'issue_evidence')
        self.assertEqual(result['status'], 'submitted')
        self.assertNotIn('qualification', json.dumps(model.payload))

    def test_failed_qualification_never_starts_prefetch_or_model(self):
        model = CaptureIssueModel()
        with patch('repair_workbench.issue_context.issue_evidence') as retrieve:
            with self.assertRaisesRegex(ValueError, 'do not start inference'):
                run_qualified_agent(self.workspace, self.root / 'blocked-agent', model,
                                    self.qualification(False), swe_policy='issue_evidence')
        retrieve.assert_not_called()
        self.assertEqual(model.calls, 0)
        self.assertFalse((self.root / 'blocked-agent').exists())

    def test_reusable_qualification_gate_returns_public_metadata_and_keeps_checks(self):
        directory = self.qualification()
        task_path = self.workspace / 'task.json'
        public = json.loads(task_path.read_text())
        task_path.write_text(json.dumps({**public, 'extra_private_field': 'HIDDEN_SENTINEL'}))
        checked = validate_qualification(self.workspace, directory)
        self.assertEqual(set(checked), {'task', 'image_id', 'execution_policy', 'qualification_sha256'})
        self.assertEqual(set(checked['task']), {'instance_id', 'repo', 'base_commit', 'problem_statement'})
        self.assertNotIn('HIDDEN_SENTINEL', json.dumps(checked))
        self.assertEqual(len(checked['qualification_sha256']), 64)
        binding = directory / 'gold/source-binding.json'
        original = json.loads(binding.read_text())
        binding.write_text(json.dumps({**original, 'dirty_returncode': 1}))
        with self.assertRaisesRegex(ValueError, 'clean task'):
            validate_qualification(self.workspace, directory)

    def test_query_limit_and_invalid_policy(self):
        queries = issue_queries('Failure in `Response.iter_content()` followed by one two three four five six seven.')
        self.assertEqual(queries[0], 'iter_content')
        self.assertLessEqual(len(queries), MAX_QUERIES)
        with self.assertRaisesRegex(ValueError, 'SWE policy'):
            self.session('oracle')


if __name__ == '__main__':
    unittest.main()
