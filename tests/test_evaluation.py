"""Evaluation integrity, isolation, accounting and real-container contracts."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repair_workbench.evaluation import (load_dataset, selected_tasks, prepare,
    summarize, estimate, evaluate, changes_from)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'benchmarks/real-bugs-v1'


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest, self.digest = load_dataset(DATA)

    def test_dataset_hash_and_task_selection(self):
        self.assertEqual(len(self.manifest['tasks']), 4)
        self.assertEqual(load_dataset(DATA)[1], self.digest)
        name = self.manifest['tasks'][0]['id']
        self.assertEqual(len(selected_tasks(self.manifest, [name])), 1)
        for names in ([name, name], ['unknown']):
            with self.assertRaises(ValueError):
                selected_tasks(self.manifest, names)

    def test_cross_project_cases_are_pinned_and_hide_acceptance(self):
        corpus=ROOT / 'benchmarks/cross-project-v1'
        manifest,digest=load_dataset(corpus)
        self.assertEqual(digest,'4734629af8b6f5bab9953a089e257c7fa6251925d163d099a892795974ed8708')
        self.assertEqual({x['id'] for x in manifest['tasks']},
                         {'bucket-falsey-validator','batches-nonpositive-count'})
        for task in manifest['tasks']:
            snapshot=prepare(corpus,task,self.root/task['id'])
            files={item['path'] for item in json.loads((snapshot/'manifest.json').read_text())['files']}
            self.assertIn('more_itertools/more.py',files)
            self.assertIn('LICENSE',files)
            self.assertIn('tests/test_regression.py',files)
            self.assertNotIn('tests/test_holdout.py',files)
            self.assertNotIn('oracle/more_itertools/more.py',files)
            self.assertNotEqual((snapshot/'workspace/more_itertools/more.py').read_bytes(),
                                (corpus/task['id']/'oracle/more_itertools/more.py').read_bytes())

    def test_provider_account_block_stops_batch_without_score(self):
        from types import SimpleNamespace
        outcome={'status':'inconclusive','stop_reason':'provider_account_unavailable',
                 'model_calls':1,'attempts':[],'token_usage':{'input_tokens':0,'output_tokens':0,'reports':0}}
        with patch('repair_workbench.evaluation.subprocess.run',return_value=SimpleNamespace(stdout='sha256:test')) as image, \
             patch('repair_workbench.evaluation.platform.platform',return_value='test-platform'), \
             patch('repair_workbench.evaluation.qualify',return_value={'qualified':True,'checks':{}}) as qualify, \
             patch('repair_workbench.session.RepairSession'), \
             patch('repair_workbench.agent.run_agent',return_value=outcome) as run:
            record=evaluate(DATA,self.root/'blocked','study-agent-deepagents:0.1',mode='run',
                            model_factory=lambda: object(),profile='full')
        self.assertEqual(record['interrupted_reason'],'provider_account_unavailable')
        self.assertEqual(record['summary']['model_calls'],1)
        self.assertIsNone(record['summary']['success_rate_all_selected'])
        self.assertEqual(run.call_count,1)
        self.assertEqual(qualify.call_count,1)
        self.assertEqual(sum(r['outcome']=='not_run_provider_account_unavailable' for r in record['rows']),3)
        self.assertFalse(record['summary']['usage_complete'])

    def test_tampering_and_extra_file_rejected(self):
        copied = self.root / 'dataset'
        shutil.copytree(DATA, copied)
        task = self.manifest['tasks'][0]
        path = copied / task['id'] / next(iter(task['files']))
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            load_dataset(copied)
        path.write_bytes(original)
        (copied / task['id'] / 'extra.txt').write_text('extra')
        with self.assertRaises(ValueError):
            load_dataset(copied)

    def test_symlink_rejected(self):
        copied = self.root / 'dataset'
        shutil.copytree(DATA, copied)
        task = self.manifest['tasks'][0]
        (copied / task['id'] / 'outside').symlink_to(self.root)
        with self.assertRaises(ValueError):
            load_dataset(copied)

    def test_input_repeatable_and_oracle_holdout_not_exposed(self):
        task = self.manifest['tasks'][0]
        first = prepare(DATA, task, self.root / 'first')
        second = prepare(DATA, task, self.root / 'second')
        a = json.loads((first / 'manifest.json').read_text())
        b = json.loads((second / 'manifest.json').read_text())
        self.assertEqual(a['commit'], b['commit'])
        self.assertEqual(a['files'], b['files'])
        files = {f['path'] for f in a['files']}
        self.assertEqual(files, {'boltons/__init__.py', 'boltons/mathutils.py',
                                 'LICENSE', 'tests/test_regression.py'})
        self.assertNotEqual((first / 'workspace/boltons/mathutils.py').read_text(),
                            changes_from(DATA, task)['boltons/mathutils.py'])
        with self.assertRaises(FileExistsError):
            prepare(DATA, task, self.root / 'first')

    def test_statistics_keep_errors_and_pending_in_denominator(self):
        rows = [
            {'id':'a', 'state':'completed', 'outcome':'passed', 'agent':{
                'status':'passed', 'model_calls':2, 'attempts':[{'status':'passed'}],
                'token_usage':{'input_tokens':100, 'output_tokens':20, 'reports':1}}},
            {'id':'b', 'state':'completed', 'outcome':'evaluation_error'},
            {'id':'c', 'state':'pending'}]
        result = summarize(rows, 'run')
        self.assertEqual(result['success_rate_all_selected'], 1/3)
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['first_attempt_holdout_passed'], 1)
        self.assertIsNone(summarize(rows, 'validate')['success_rate_all_selected'])
        self.assertEqual(result['context_totals']['reads'],0)
        self.assertEqual(result['candidate_failure_reasons'],{})

    def test_invalid_limits_rejected_before_docker(self):
        with patch('repair_workbench.evaluation.subprocess.run') as command:
            for kwargs in ({'max_calls':0}, {'deadline':float('nan')}, {'timeout':-1}):
                with self.assertRaises(ValueError):
                    evaluate(DATA, self.root / 'output', 'unused', **kwargs)
            command.assert_not_called()

    def test_estimate_is_explicit_about_assumptions(self):
        result = estimate(4)
        self.assertEqual(result['max_model_calls'],48)
        self.assertAlmostEqual(result['planning_cny'][1],1.622,places=3)
        self.assertIn('not a hard cost cap', result['assumption'])


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'requires Docker')
class EvaluationDockerTests(unittest.TestCase):
    def test_oracle_qualification_and_overfit_candidate_rejected(self):
        from test_agent import ScriptedModel, call, AIMessage
        manifest, _ = load_dataset(DATA)
        task = manifest['tasks'][0]
        source = (DATA / task['id'] / 'source/boltons/mathutils.py').read_text()
        # Satisfies visible last-bit example but fails independent negative-index cases.
        overfit = source.replace('if type(k) is int:',
                                'if type(k) is int:\n            if k == -1: return True')
        model = ScriptedModel(responses=[call('submit_candidate', {
            'changes':{'boltons/mathutils.py':overfit}, 'diagnosis':'test-only overfit'}, 'c1'),
            AIMessage(content='done')])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'evaluation'
            result = evaluate(DATA, output, os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'),
                mode='run', names=[task['id']], model_factory=lambda:model, model_name='test-scripted')
            row = result['rows'][0]
            self.assertTrue(row['qualification']['qualified'])
            self.assertEqual(row['agent']['status'], 'passed')
            self.assertTrue(row['outcome'].startswith('holdout_'), row)
            self.assertEqual(result['summary']['holdout_passed'],0)
            self.assertEqual(result['summary']['visible_passed'],1)
            for file in ('results.json','results.csv','report.md'):
                self.assertTrue((output / file).exists())
            with self.assertRaises(FileExistsError):
                evaluate(DATA, output, os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'))

    def test_invalid_task_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('repair_workbench.evaluation.qualify', return_value={'qualified':False}), \
                 patch('repair_workbench.settings.ModelSettings.create_model') as factory:
                result = evaluate(DATA, Path(tmp)/'run', os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'),
                    mode='run', names=['bits-negative-index'], model_factory=factory)
                factory.assert_not_called()
                self.assertEqual(result['rows'][0]['outcome'], 'invalid_task')

    def test_official_candidate_passes_independent_acceptance(self):
        from test_agent import ScriptedModel, call, AIMessage
        manifest, _ = load_dataset(DATA)
        task = manifest['tasks'][0]
        model = ScriptedModel(responses=[call('submit_candidate', {
            'changes':changes_from(DATA, task), 'diagnosis':'test-only known fix'}, 'c1'),
            AIMessage(content='done')])
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate(DATA, Path(tmp)/'run', os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'),
                mode='run', names=[task['id']], model_factory=lambda:model, model_name='test-scripted')
            self.assertEqual(result['rows'][0]['outcome'], 'passed')
            self.assertEqual(result['summary']['holdout_passed'], 1)
            self.assertEqual(result['summary']['first_attempt_holdout_passed'], 1)
            self.assertFalse(result['summary']['usage_complete'])
