"""No model/network calls: public/evaluator isolation and patch lifecycle."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import io
import tarfile
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_agent import ScriptedModel, call
from repair_workbench.agent import run_agent
from repair_workbench.session import RepairSession, BudgetExceeded
from repair_workbench.swebench_adapter import (
    import_dataset, task_record, prepare_workspace, SWEbenchSession,
    export_prediction, validate_record, evaluate, image_identity, isolated_env,
    prepare_archive_workspace, validate_candidate_patch, run_qualified_agent)

spec = importlib.util.spec_from_file_location('swe_fixture',
    Path(__file__).resolve().parents[1] / 'scripts/qualify_swebench_fixture.py')
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


class SourceReplay(ScriptedModel):
    visible_payload: dict = {}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.calls == 0:
            first = next(m for m in messages if isinstance(m, HumanMessage))
            self.visible_payload = json.loads(first.content)
            response = call('search_source', {'query': 'return a - b'}, 'search')
        else:
            data = json.loads(next(m for m in reversed(messages) if isinstance(m, ToolMessage)).content)
            response = call('submit_ref_edits', {'edits': [{
                'source_ref': data['matches'][0]['source_ref'],
                'old_text': 'return a - b', 'new_text': 'return a + b'}],
                'diagnosis': 'Use addition as requested by the issue.'}, 'submit')
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class SWEAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = fixture_module.prepare_fixture(self.root / 'fixture')
        self.fixture = self.root / 'fixture'
        self.workspace = self.fixture / 'agent-workspace'
        self.dataset = self.fixture / 'evaluator-dataset'

    def session(self, **kw):
        return SWEbenchSession(self.workspace, self.root / 'agent', **kw)

    def test_task_projection_excludes_all_evaluator_fields(self):
        public = json.loads((self.dataset / self.row['instance_id'] / 'public.json').read_text())
        self.assertEqual(set(public), {'instance_id', 'repo', 'base_commit', 'problem_statement'})
        receipt = json.loads((self.workspace / 'workspace.json').read_text())
        self.assertEqual(receipt['allowed_paths'], ['calculator.py'])
        self.assertNotIn('test_patch', (self.workspace / 'task.json').read_text())
        self.assertFalse((self.workspace / 'baseline/workspace/evaluator.json').exists())

    def test_issue_only_mode_does_not_weaken_ordinary_repair_gate(self):
        with self.assertRaises(ValueError):
            RepairSession(self.workspace / 'baseline', self.root / 'ordinary', {'calculator.py'})
        session = self.session()
        self.assertEqual(session.before['status'], 'issue_only')
        self.assertEqual(session.read_test_log()['status'], 'unavailable')

    def test_real_graph_stages_one_patch_without_claiming_test_success(self):
        session = self.session()
        model = SourceReplay()
        result = run_agent(session, model, self.row['problem_statement'])
        self.assertEqual(result['status'], 'submitted', result)
        self.assertEqual(result['stop_reason'], 'candidate_submitted')
        self.assertEqual(result['evaluation_status'], 'not_run')
        self.assertEqual(result['submission_counts']['docker_verifications'], 0)
        self.assertEqual(result['model_calls'], 2)
        self.assertNotIn('read_test_log', model.exposed)
        self.assertFalse(model.visible_payload['initial_context']['tests_executed'])
        target = self.root / 'predictions.jsonl'
        pred = export_prediction(self.workspace, session.output, target)
        self.assertEqual(set(pred), {'instance_id', 'model_name_or_path', 'model_patch'})
        self.assertIn('+    return a + b', pred['model_patch'])
        self.assertNotIn('tests/test_calculator.py', pred['model_patch'])
        self.assertIn('return a - b', (self.workspace / 'baseline/workspace/calculator.py').read_text())
        with self.assertRaises(BudgetExceeded):
            session.reserve_model()

    def test_evaluator_sentinels_never_reach_agent_input(self):
        row = {**self.row, 'patch': self.row['patch'] + 'HIDDEN_GOLD_SENTINEL',
               'test_patch': 'HIDDEN_TEST_SENTINEL', 'hints_text': 'HIDDEN_HINT_SENTINEL',
               'FAIL_TO_PASS': ['HIDDEN_ACCEPTANCE_SENTINEL']}
        path = self.root / 'raw.json';path.write_text(json.dumps([row]))
        dataset = self.root / 'private'
        import_dataset(path, dataset, dataset_name='fixture', revision='1' * 40)
        workspace = self.root / 'public'
        prepare_workspace(dataset, row['instance_id'], self.fixture / 'repo', workspace)
        model = SourceReplay()
        run_agent(SWEbenchSession(workspace, self.root / 'other-agent'), model, row['problem_statement'])
        encoded = json.dumps(model.visible_payload)
        self.assertNotIn('HIDDEN_', encoded)
        self.assertNotIn('HIDDEN_', (workspace / 'task.json').read_text())

    def test_task_tampering_and_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            task_record(self.dataset, '../outside')
        private = self.dataset / self.row['instance_id'] / 'evaluator.json'
        private.write_text(private.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            task_record(self.dataset, self.row['instance_id'])

    def test_duplicate_ids_and_unsupported_schema_rejected(self):
        path = self.root / 'duplicates.json';path.write_text(json.dumps([self.row, self.row]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            import_dataset(path, self.root / 'bad', dataset_name='fixture', revision='a' * 40)
        for update in ({'base_commit': 'HEAD'}, {'eval_type': 'fail_only'},
                       {'FAIL_TO_PASS': []}, {'instance_id': '../escape'},
                       {'repo': 'other/repo'}, {'image_assets': {'patch': [{'url': 'https://x'}]}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_record({**self.row, **update})

    def test_worktree_changes_do_not_enter_base_snapshot(self):
        (self.fixture / 'repo/calculator.py').write_text('DIRTY_WORKTREE')
        prepare_workspace(self.dataset, self.row['instance_id'], self.fixture / 'repo', self.root / 'clean')
        self.assertNotIn('DIRTY', (self.root / 'clean/baseline/workspace/calculator.py').read_text())

    def test_test_file_edits_and_nonexistent_refs_remain_forbidden(self):
        session = self.session()
        read = session.read_source('tests/test_calculator.py', raw=True)
        result = session.submit_ref_edits([{'source_ref': read['source_ref'],
            'old_text': 'assert add(2, 3) == 5', 'new_text': 'assert True'}], 'skip assertion')
        self.assertEqual(result['reason'], 'path_not_allowed')
        self.assertIsNone(session.submitted_number)
        self.assertEqual(session.submission_counts['materialized'], 0)

    def test_export_recomputes_patch_and_refuses_changed_candidate(self):
        session = self.session()
        run_agent(session, SourceReplay(), self.row['problem_statement'])
        candidate = session.output / 'attempt-01/verification'
        (candidate / 'patch.diff').write_text('FORGED PATCH')
        pred = export_prediction(self.workspace, session.output, self.root / 'valid.jsonl')
        self.assertNotIn('FORGED', pred['model_patch'])
        (candidate / 'workspace/tests/test_calculator.py').write_text('assert True')
        with self.assertRaises(ValueError):
            export_prediction(self.workspace, session.output, self.root / 'invalid.jsonl')

    def test_no_model_key_is_inherited_by_official_worker(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'private', 'OPENAI_API_KEY': 'private',
                                    'DOCKER_HOST': 'unix:///fixture.sock'}):
            env = isolated_env()
        self.assertNotIn('DASHSCOPE_API_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')

    def test_local_image_preflight_never_pulls(self):
        failed = subprocess.CompletedProcess([], 1, '', 'No such image')
        with patch('repair_workbench.swebench_adapter.subprocess.run', return_value=failed) as run:
            with self.assertRaisesRegex(ValueError, 'never pulls'):
                image_identity('missing-image:tag')
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('pull', run.call_args.args[0])

    def test_virtualenv_python_symlink_is_preserved_and_absent_report_is_not_success(self):
        python = self.root / 'venv/bin/python';python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        process = MagicMock();process.poll.return_value=0
        absent = subprocess.CompletedProcess([], 1, '', 'Error: No such object: container')
        actual_run = subprocess.run
        def commands(args, **kwargs):
            return absent if args[0] == 'docker' else actual_run(args, **kwargs)
        pred = {'instance_id': self.row['instance_id'], 'model_name_or_path': 'test', 'model_patch': self.row['patch']}
        with patch('repair_workbench.swebench_adapter.image_identity', return_value='sha256:' + 'a' * 64), \
             patch('repair_workbench.swebench_adapter.isolated_env', return_value={}), \
             patch('repair_workbench.swebench_adapter.start_worker', return_value=process) as popen, \
             patch('repair_workbench.swebench_adapter.subprocess.run', side_effect=commands):
            result = evaluate(self.row, pred, self.root / 'evaluation', python, workspace=self.workspace, timeout=1)
        self.assertEqual(popen.call_args.args[0][0], str(python.absolute()))
        self.assertNotEqual(popen.call_args.args[0][0], str(python.resolve()))
        self.assertEqual(result['status'], 'inconclusive')
        self.assertTrue(result['cleanup_ok'])

    def test_external_predictions_cannot_change_tests_or_configuration(self):
        validate_candidate_patch(self.row['patch'])
        for path in ('tests/test_calculator.py', '../escape.py', 'setup.py', '.github/config.py'):
            bad = self.row['patch'].replace('calculator.py', path)
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_candidate_patch(bad)
        with self.assertRaises(ValueError):
            validate_candidate_patch(self.row['patch'] + '\nnew file mode 100644\n')

    def archive(self, name, members):
        path = self.root / name
        with tarfile.open(path, 'w:gz') as tar:
            for member_name, content, kind in members:
                m = tarfile.TarInfo(member_name)
                if kind == 'link':
                    m.type = tarfile.SYMTYPE;m.linkname = '/outside'
                    tar.addfile(m)
                else:
                    data = content.encode();m.size = len(data);m.mode = 0o644
                    tar.addfile(m, io.BytesIO(data))
        return path

    def metadata(self, tree=None):
        tree = tree or subprocess.check_output(['git', '-C', str(self.fixture / 'repo'),
                                               'rev-parse', 'HEAD^{tree}']).decode().strip()
        p = self.root / 'metadata.json'
        p.write_text(json.dumps({'sha': self.row['base_commit'], 'tree': {'sha': tree}}))
        return p

    def test_archive_source_matches_entire_upstream_tree_without_history(self):
        source = self.fixture / 'repo'
        members = [('repo/' + str(p.relative_to(source)), p.read_text(), 'file')
                   for p in [source / 'calculator.py', source / 'tests/test_calculator.py']]
        archive = self.archive('source.tar.gz', members)
        prepared = prepare_archive_workspace(self.dataset, self.row['instance_id'],
            archive, self.metadata(), self.root / 'archive-workspace')
        self.assertEqual(prepared['task']['base_commit'], self.row['base_commit'])
        m = json.loads((self.root / 'archive-workspace/baseline/manifest.json').read_text())
        self.assertEqual(m['commit'], self.row['base_commit'])
        self.assertEqual(m['source_proof']['tree'], self.metadata_tree())
        self.assertNotIn('.git', str([p['path'] for p in m['files']]))

    def metadata_tree(self):
        return subprocess.check_output(['git', '-C', str(self.fixture / 'repo'),
                                        'rev-parse', 'HEAD^{tree}']).decode().strip()

    def test_archive_mismatch_traversal_and_links_are_rejected(self):
        for i, members in enumerate([
            [('repo/../escape', 'bad', 'file')],
            [('repo/link', '', 'link')],
            [('repo/calculator.py', 'tampered', 'file')],
        ]):
            with self.subTest(i=i):
                archive = self.archive('bad%d.tar.gz' % i, members)
                with self.assertRaises(ValueError):
                    prepare_archive_workspace(self.dataset, self.row['instance_id'],
                        archive, self.metadata(), self.root / ('bad-arc%d' % i))

    def test_official_worker_keeps_restrictions_on_sdk_dynamic_collections(self):
        from repair_workbench.swebench_worker import HarnessClient
        class DynamicClient:
            api = 'delegated'
            @property
            def containers(self):
                return MagicMock()
            @property
            def images(self):
                return MagicMock()
        raw = DynamicClient();client = HarnessClient(raw)
        client.containers.create = 'guarded-create'
        client.images.pull = 'guarded-pull'
        self.assertEqual(client.containers.create, 'guarded-create')
        self.assertEqual(client.images.pull, 'guarded-pull')
        self.assertNotEqual(raw.containers.create, 'guarded-create')
        self.assertEqual(client.api, 'delegated')

    def test_rebuilt_image_history_accepts_same_blobs_but_rejects_links_and_duplicates(self):
        from repair_workbench.swebench_worker import parse_source_blobs
        oid = b'a' * 40
        a = b'100644 blob ' + oid + b'\tmodule.py\0'
        b = b'100755 blob ' + oid + b'\tmodule.py\0'
        self.assertEqual(parse_source_blobs(a), parse_source_blobs(b))
        self.assertNotEqual(parse_source_blobs(a), parse_source_blobs(b.replace(oid, b'b' * 40)))
        with self.assertRaises(ValueError):parse_source_blobs(a + b)
        with self.assertRaises(ValueError):parse_source_blobs(a.replace(b'100644', b'120000'))

    def test_failed_environment_qualification_stops_before_agent_or_model(self):
        q = self.root / 'qualification';q.mkdir()
        (q / 'qualification.json').write_text(json.dumps({'qualified': False,
            'negative': {'status': 'unresolved'}, 'gold': {'status': 'unresolved'}}))
        model = SourceReplay()
        with self.assertRaisesRegex(ValueError, 'do not start inference'):
            run_qualified_agent(self.workspace, self.root / 'blocked-agent', model, q)
        self.assertEqual(model.calls, 0)
        self.assertFalse((self.root / 'blocked-agent').exists())

    def test_qualified_entry_runs_graph_and_does_not_feed_acceptance_to_model(self):
        q = self.root / 'qualification';q.mkdir();results = {}
        for role, status in [('negative', 'unresolved'), ('gold', 'resolved')]:
            d = q / role;d.mkdir()
            (d / 'source-binding.json').write_text(json.dumps({
                'upstream_commit': self.row['base_commit'], 'content_match': True, 'dirty_returncode': 0}))
            results[role] = {'status': status, 'instance_id': self.row['instance_id'], 'cleanup_ok': True,
                             'image_id': 'sha256:' + 'a' * 64, 'execution_policy': {'network': 'none'}}
        (q / 'qualification.json').write_text(json.dumps({'qualified': True, **results}))
        model = SourceReplay()
        result = run_qualified_agent(self.workspace, self.root / 'qualified-agent', model, q)
        self.assertEqual(result['status'], 'submitted')
        self.assertNotIn('qualification', json.dumps(model.visible_payload))
        self.assertEqual(model.calls, 2)


if __name__ == '__main__':
    unittest.main()
