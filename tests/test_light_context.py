"""Visible failure context, actionable read errors, and advisory review isolation."""
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
import os
from unittest.mock import patch

from test_agent import fixture, ScriptedModel, AIMessage
from repair_workbench.agent import run_agent
from repair_workbench.edits import EditRejected
from repair_workbench.session import RepairSession
from repair_workbench.snapshot import create_snapshot,snapshot_id
from test_simple_edit import RefReplayModel


class LightContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=fixture(self.root)

    def make(self,profile='simple_full'):
        return RepairSession(self.base,self.root/profile,{'pricing.py'},profile=profile)

    def test_specific_read_feedback_and_no_raw_exception(self):
        s=self.make()
        for args,reason,field in [({'path':'../private-key'},'invalid_source_path','path'),
              ({'path':'oracle/pricing.py'},'path_not_in_baseline','path'),
              ({'path':'pricing.py','line_count':201},'invalid_line_count','line_count'),
              ({'path':'pricing.py','start_line':999},'start_line_out_of_range','start_line'),
              ({'path':'pricing.py','start_line':True},'invalid_start_line','start_line')]:
            with self.subTest(reason=reason),self.assertRaises(EditRejected) as error:s.read_source(**args)
            self.assertEqual(error.exception.reason,reason)
            self.assertEqual(error.exception.details['field'],field)
            self.assertIn('allowed',error.exception.details)
            self.assertNotIn('private-key',json.dumps(error.exception.details))
        with patch('repair_workbench.session.verify_snapshot',side_effect=PermissionError('secret-marker')):
            with self.assertRaises(EditRejected) as error:s.read_source('pricing.py')
        self.assertEqual(error.exception.reason,'source_unavailable')
        self.assertNotIn('secret-marker',str(error.exception.details))
        (self.base/'reports/pytest.log').unlink()
        with self.assertRaises(EditRejected) as error:s.read_test_log()
        self.assertEqual(error.exception.reason,'test_log_unavailable')

    def test_initial_context_uses_only_visible_anchors_and_read_references(self):
        (self.root/'oracle').mkdir();(self.root/'oracle/secret.py').write_text('HIDDEN_ORACLE_SENTINEL')
        (self.base/'reports/pytest.log').write_text('> assert discounted_price(100) == 90\n'
            'E AssertionError\npricing.py:5: AssertionError\n'
            'File "../../oracle/secret.py", line 1\nFile "/workspace/holdout/test_secret.py", line 1\n')
        s=self.make();s.task='Fix discounted_price boundary and preserve other inputs.'
        packet=s.initial_context();encoded=json.dumps(packet,ensure_ascii=False)
        self.assertLessEqual(len(encoded),12000)
        self.assertNotIn('HIDDEN_ORACLE_SENTINEL',encoded)
        self.assertNotIn('holdout/test_secret.py',encoded)
        self.assertTrue(packet['failed_tests']);self.assertTrue(packet['source_excerpts'])
        for item in packet['source_excerpts']:
            self.assertIn(item['path'],s.files)
            self.assertEqual(s.source_refs[item['source_ref']]['content'],item['content'])
        self.assertEqual(s.tool_calls,0)

    def test_search_and_symbol_reads_return_usable_exact_refs(self):
        s=self.make('simple_context')
        match=s.search_source('amount > 100')['matches'][0]
        self.assertEqual(match['owner']['symbol'],'discounted_price')
        self.assertIn('amount > 100',match['content']);self.assertIn(match['source_ref'],s.source_refs)
        first=s.read_symbol('pricing.py','discounted_price')
        second=s.read_symbol('pricing.py','discounted_price')
        self.assertEqual(first['content'],second['content'])
        self.assertIn(second['source_ref'],s.source_refs)
        self.assertNotEqual(first['source_ref'],second['source_ref'])

    def test_features_are_independent_and_do_not_reintroduce_plan_forms(self):
        for profile,context,review in [('simple_edit',False,False),('simple_context',True,False),
                                        ('simple_review',False,True),('simple_full',True,True)]:
            with self.subTest(profile=profile):
                s=self.make(profile);model=ScriptedModel(responses=[AIMessage(content='done')])
                result=run_agent(s,model,'fix pricing')
                self.assertEqual(result['features']['context'],context)
                self.assertEqual('read_symbol' in model.exposed,context)
                self.assertEqual('record_behavior_summary' in model.exposed,review)
                self.assertNotIn('set_repair_plan',model.exposed)
                self.assertFalse(s.review_enabled)

    def test_risks_are_real_diff_advice_and_do_not_block_valid_patch(self):
        s=self.make('simple_review');read=s.read_source('pricing.py',raw=True)
        with patch('repair_workbench.session.verify_candidate',return_value={
                    'status':'passed','reason':'passed_original_test_scope'}) as verify:
            result=s.submit_ref_edits([{'source_ref':read['source_ref'],
                'old_text':'amount > 100','new_text':'amount >= 0'}],'changed condition')
        self.assertEqual(verify.call_count,1)
        self.assertIn('comparison_changed',{risk['kind'] for risk in result['risk_review']['risks']})
        self.assertEqual(result['risk_review']['changed_paths'],['pricing.py'])
        self.assertIsNone(result['risk_review']['behavior_summary'])
        self.assertIn('neither model summaries nor risks prove',result['risk_review']['assurance'])
        self.assertIn('amount > 100',(self.base/'workspace/pricing.py').read_text())

    def test_optional_behavior_summary_is_model_analysis(self):
        s=self.make('simple_review')
        result=s.record_behavior_summary('Change boundary','Keep invalid-input checks',['discounted_price'])
        self.assertIn('may be incomplete',result['trust'])
        self.assertEqual(s.save('running')['behavior_summary'],result)
        with self.assertRaises(EditRejected):self.make('simple_edit').record_behavior_summary('change','preserve',[])

    def test_non_utf8_and_oversized_sources_have_specific_bounds(self):
        source=self.root/'source'
        (source/'encoding.py').write_bytes(b'\xff')
        (source/'oversized.py').write_bytes(b'#'+b'x'*(1024*1024))
        for args in [('add','.'),('commit','-qm','Boundary source files')]:
            subprocess.run(['git','-C',str(source),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                '-c','user.name=Test','-c','user.email=test@example.invalid',*args],check=True,capture_output=True)
        base=self.root/'boundary-baseline';manifest=create_snapshot(source,base)
        shutil.copytree(self.base/'reports',base/'reports')
        result=json.loads((base/'reports/result.json').read_text());result['snapshot_id']=snapshot_id(manifest)
        (base/'reports/result.json').write_text(json.dumps(result))
        s=RepairSession(base,self.root/'boundary-agent',{'pricing.py'},profile='simple_full')
        for path,reason in [('encoding.py','source_not_utf8'),('oversized.py','source_too_large')]:
            with self.subTest(path=path),self.assertRaises(EditRejected) as error:s.read_source(path)
            self.assertEqual(error.exception.reason,reason)
            self.assertEqual(error.exception.details['field'],'path')

    def test_missing_optional_log_is_a_warning_not_an_agent_gate(self):
        (self.base/'reports/pytest.log').unlink()
        s=self.make('simple_context');packet=s.initial_context()
        self.assertIn('baseline_log_unavailable',packet['warnings'])
        self.assertTrue(packet['failed_tests'])

    def test_assertion_without_source_stack_finds_public_call_target(self):
        (self.base/'reports/pytest.log').write_text('> assert discounted_price(100) == 90\n'
            'E AssertionError\ntests/test_pricing.py:9: AssertionError\n')
        packet=self.make('simple_context').initial_context()
        self.assertIn('pricing.py',[item['path'] for item in packet['source_excerpts']])
        identities=[(item['path'],item['start_line']) for item in packet['source_excerpts']]
        self.assertEqual(len(identities),len(set(identities)))


class SymbolReplayModel(RefReplayModel):
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        if self.calls==0:
            from test_agent import call
            from langchain_core.outputs import ChatGeneration,ChatResult
            self.calls+=1
            return ChatResult(generations=[ChatGeneration(message=call('read_symbol',
                {'path':'pricing.py','symbol':'discounted_price'},'read'))])
        return super()._generate(messages,stop,run_manager,**kwargs)


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS')=='1','requires local Docker image')
class LightContextDockerTests(unittest.TestCase):
    def test_failure_context_symbol_reference_and_advice_reach_real_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=fixture(root,real=True)
            original=(root/'source/pricing.py').read_text()
            session=RepairSession(base,root/'agent',{'pricing.py'},profile='simple_full')
            result=run_agent(session,SymbolReplayModel(),'Fix discounted_price boundary and preserve other behavior')
            self.assertEqual(result['status'],'passed',result)
            self.assertIn('risk_review',result['attempts'][0])
            self.assertEqual(result['submission_counts']['requests'],1)
            self.assertTrue((session.output/result['patch']).exists())
            self.assertEqual((root/'source/pricing.py').read_text(),original)
