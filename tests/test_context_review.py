"""Requirements, scope and context contracts; no paid model involved."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_agent import fixture, ScriptedModel, call, AIMessage
from repair_workbench.agent import run_agent
from repair_workbench.session import RepairSession, BudgetExceeded
from repair_workbench.edits import EditRejected
from repair_workbench.repair_contract import validate_plan, review_changes
from repair_workbench.source_context import change_review, outline

TASK='修复折扣边界；保留其他金额行为。'

class ContextReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=fixture(self.root)
        self.s=self.make('full');self.s.task=TASK
        self.source=(self.base/'workspace/pricing.py').read_text()
        self.symbol=outline(self.source)['symbols'][0]['symbol']
        sha=hashlib.sha256(self.source.encode()).hexdigest()
        evidence=[{'path':'pricing.py','sha256':sha,'start_line':1,'end_line':2}]
        self.requirements=[{'id':'C','kind':'change','task_quote':'修复折扣边界','claim':'修复阈值','evidence':evidence},
                           {'id':'P','kind':'preserve','task_quote':'保留其他金额行为','claim':'保持其他输入行为','evidence':evidence}]
        self.scope=[{'path':'pricing.py','symbol':self.symbol}]
        self.fixed=self.source.replace('amount > 100','amount >= 100')
    def make(self,profile):
        return RepairSession(self.base,self.root/profile,{'pricing.py'},profile=profile)
    def plan(self):
        return self.s.set_repair_plan(self.requirements,self.scope)
    def assessment(self):
        return {'plan_id':self.s.repair_plan['plan_id'],'coverage':{'C':'只修复阈值等号','P':'负数校验与其他区间保持'},
                'risk_acknowledgements':[{'kind':'comparison_changed','requirement_ids':['P'],
                    'explanation':'只修改折扣阈值；未放宽负数边界'}]}
    def test_observe_does_not_change_read_response(self):
        old=self.make('v2');obs=self.make('observe')
        for _ in range(2):self.assertEqual(old.read_source('pricing.py'),obs.read_source('pricing.py'))
        self.assertEqual(obs.context_stats['repeated_reads'],1)
        self.assertEqual(obs.context_stats['suppressed_reads'],0)
    def test_repeat_suppression_force_and_overlap_accounting(self):
        first=self.s.read_source('pricing.py',1,2,raw=True)
        self.assertTrue(first['content'])
        self.assertEqual(self.s.read_source('pricing.py',1,2,raw=True)['content'],'')
        self.assertEqual(self.s.read_source('pricing.py',1,2,raw=True,force=True)['content'],first['content'])
        self.s.read_source('pricing.py',2,2,raw=True)
        self.assertEqual(self.s.context_stats['unique_lines'],3)
        self.assertEqual(self.s.context_stats['repeated_reads'],2)
        self.assertEqual(self.s.context_stats['suppressed_reads'],1)
        events=[json.loads(x) for x in (self.s.output/'events.jsonl').read_text().splitlines()]
        reads=[x for x in events if x['event']=='source_read']
        self.assertEqual(reads[1]['delivered_lines'],0)
        self.assertNotIn(self.source,(self.s.output/'events.jsonl').read_text())
    def test_navigation_returns_exact_symbol_and_literal_search(self):
        result=self.s.read_symbol('pricing.py',self.symbol)
        self.assertEqual(result['content'],self.source[self.source.index('def '):])
        self.assertEqual(self.s.tool_calls,1)
        self.assertTrue(self.s.search_source('amount')['matches'])
        self.assertEqual(self.s.search_source('.*')['matches'],[])
        for path in ['../pricing.py','oracle/pricing.py','holdout/test.py']:
            with self.assertRaises(EditRejected):self.s.source_outline(path)
        self.assertNotIn('oracle',str(self.s.initial_context()['files']))
    def test_invalid_symbol_still_consumes_tool_budget(self):
        with self.assertRaises(EditRejected):self.s.read_symbol('pricing.py','missing')
        self.assertEqual(self.s.tool_calls,1)
    def test_outline_does_not_execute_and_handles_invalid_python(self):
        self.assertTrue(outline('def broken(')['parse_error'])
        names=[x['symbol'] for x in outline('raise RuntimeError()\nclass C:\n def m(self): pass\n')['symbols']]
        self.assertEqual(names,['C.m'])
    def test_evidence_path_version_range_and_quote_rejected(self):
        cases=[('path','../pricing.py'),('sha256','bad'),('start_line',0),('end_line',999)]
        for key,value in cases:
            req=copy.deepcopy(self.requirements);req[0]['evidence'][0][key]=value
            with self.subTest(key=key),self.assertRaises(EditRejected):validate_plan(self.s,req,self.scope)
        req=copy.deepcopy(self.requirements);req[0]['task_quote']='invented'
        with self.assertRaises(EditRejected):validate_plan(self.s,req,self.scope)
    def test_malformed_nested_fields_are_rejections(self):
        for field,value in [('kind',{}),('id',[]),('evidence',{})]:
            req=copy.deepcopy(self.requirements);req[0][field]=value
            with self.assertRaises(EditRejected):validate_plan(self.s,req,self.scope)
        req=copy.deepcopy(self.requirements);req[0]['evidence'][0]['path']=[]
        with self.assertRaises(EditRejected):validate_plan(self.s,req,self.scope)
        with self.assertRaises(EditRejected):validate_plan(self.s,self.requirements,[{'path':'pricing.py','symbol':[]}])
    def test_preserved_requirements_and_existing_allowed_scope_required(self):
        req=copy.deepcopy(self.requirements);req[1]['kind']='change'
        with self.assertRaises(EditRejected):validate_plan(self.s,req,self.scope)
        for scope in [[{'path':'tests/test_pricing.py','symbol':'test_boundary'}], [{'path':'pricing.py','symbol':'missing'}]]:
            with self.assertRaises(EditRejected):validate_plan(self.s,self.requirements,scope)
    def test_missing_plan_coverage_and_risk_rejected_before_docker(self):
        with patch('repair_workbench.session.verify_candidate') as verify:
            self.assertEqual(self.s.submit_candidate({'pricing.py':self.fixed},'fix')['reason'],'repair_plan_required')
            self.plan();assessment=self.assessment();assessment['coverage'].pop('P')
            self.assertEqual(self.s.submit_candidate({'pricing.py':self.fixed},'fix',assessment)['reason'],'incomplete_requirement_coverage')
            assessment=self.assessment();assessment['risk_acknowledgements']=[]
            result=self.s.submit_candidate({'pricing.py':self.fixed},'fix',assessment)
            self.assertEqual(result['reason'],'risk_review_required');verify.assert_not_called()
            self.assertEqual(result['risks'][0]['kind'],'comparison_changed')
    def test_review_rejection_can_retry_same_code_with_bound_assessment(self):
        self.plan();assessment=self.assessment();assessment['risk_acknowledgements']=[]
        with patch('repair_workbench.session.verify_candidate',return_value={'status':'passed','reason':'passed_original_test_scope'}) as verify:
            self.assertEqual(self.s.submit_candidate({'pricing.py':self.fixed},'fix',assessment)['reason'],'risk_review_required')
            result=self.s.submit_candidate({'pricing.py':self.fixed},'fix',self.assessment())
            self.assertEqual(result['status'],'passed');self.assertEqual(verify.call_count,1)
        self.assertEqual(len(self.s.attempts),2)
        self.assertEqual(result['contract_review']['candidate_content_id'],result['content_id'])
    def test_unrelated_module_change_rejected(self):
        self.plan()
        with self.assertRaises(EditRejected) as ctx:
            review_changes(self.s,{'pricing.py':self.fixed+'\nUNRELATED=True\n'},self.assessment())
        self.assertEqual(ctx.exception.reason,'change_outside_declared_scope')
    def test_outside_comment_and_duplicate_symbols_not_silently_reviewed(self):
        self.plan()
        with self.assertRaises(EditRejected) as ctx:
            review_changes(self.s,{'pricing.py':self.fixed+'\n# unrelated comment\n'},self.assessment())
        self.assertEqual(ctx.exception.reason,'change_outside_declared_scope')
        _,risks=change_review('def f(): pass\n','def f(): pass\n# note\n','a.py')
        self.assertIn('text_only_change',{x['kind'] for x in risks})
        with self.assertRaises(ValueError):
            change_review('def f(): pass\ndef f(): pass\n','def f(): return 1\ndef f(): pass\n','a.py')
    def test_stale_plan_and_unlinked_risk_rejected(self):
        self.plan();assessment=self.assessment();assessment['plan_id']='old'
        with self.assertRaises(EditRejected):review_changes(self.s,{'pricing.py':self.fixed},assessment)
        assessment=self.assessment();assessment['risk_acknowledgements'][0]['requirement_ids']=['C']
        with self.assertRaises(EditRejected):review_changes(self.s,{'pricing.py':self.fixed},assessment)
        assessment['risk_acknowledgements'][0]['requirement_ids']=[{}]
        with self.assertRaises(EditRejected):review_changes(self.s,{'pricing.py':self.fixed},assessment)
    def test_review_is_not_semantic_oracle(self):
        self.plan()
        # A false model explanation must NOT be reported as proof of correctness.
        bad=self.source.replace('amount > 100','amount >= 0')
        report=review_changes(self.s,{'pricing.py':bad},self.assessment())
        self.assertIn('not a semantic proof',report['assurance'])
        self.assertTrue(report['risks'])
    def test_signatures_exceptions_and_definitions_flagged(self):
        _,risks=change_review('def f(x):\n raise ValueError()\n','async def f(x)->int:\n raise TypeError()\n','a.py')
        self.assertEqual({x['kind'] for x in risks},{'signature_changed','exception_changed'})
        _,risks=change_review('def f():pass\n','def f():pass\ndef g():pass\n','a.py')
        self.assertIn('definition_added_or_removed',{x['kind'] for x in risks})
    def test_profiles_expose_independent_tools(self):
        for profile,navigation,review in [('observe',False,False),('context',True,False),('review',False,True),('full',True,True)]:
            session=self.s if profile=='full' else self.make(profile)
            model=ScriptedModel(responses=[AIMessage(content='done')])
            run_agent(session,model,TASK)
            self.assertEqual('read_symbol' in model.exposed,navigation)
            self.assertEqual('set_repair_plan' in model.exposed,review)
            self.assertNotIn('execute',model.exposed)

@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS')=='1','requires Docker')
class FullFlowDockerTests(unittest.TestCase):
    def test_contract_feedback_recovery_and_isolated_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=fixture(root,real=True)
            session=RepairSession(base,root/'agent',{'pricing.py'},profile='full');session.task=TASK
            source=(base/'workspace/pricing.py').read_text();sha=hashlib.sha256(source.encode()).hexdigest()
            evidence=[{'path':'pricing.py','sha256':sha,'start_line':1,'end_line':2}]
            req=[{'id':'C','kind':'change','task_quote':'修复折扣边界','claim':'阈值','evidence':evidence},
                 {'id':'P','kind':'preserve','task_quote':'保留其他金额行为','claim':'保留其他行为','evidence':evidence}]
            scope=[{'path':'pricing.py','symbol':outline(source)['symbols'][0]['symbol']}]
            plan=validate_plan(session,req,scope)
            edit={'path':'pricing.py','sha256':sha,'start_line':source[:source.index('amount > 100')].count('\n')+1,'old_text':'amount > 100','new_text':'amount >= 100'}
            assessment={'plan_id':plan['plan_id'],'coverage':{'C':'阈值包含等号','P':'其他区间未变'},'risk_acknowledgements':[]}
            fixed=copy.deepcopy(assessment);fixed['risk_acknowledgements']=[{'kind':'comparison_changed','requirement_ids':['P'],'explanation':'只改变折扣阈值等号'}]
            model=ScriptedModel(responses=[call('set_repair_plan',{'requirements':req,'scope':scope},'plan'),
                call('submit_edits',{'edits':[edit],'diagnosis':'boundary','assessment':assessment},'reject'),
                call('submit_edits',{'edits':[edit],'diagnosis':'boundary','assessment':fixed},'retry'),AIMessage(content='done')])
            result=run_agent(session,model,TASK)
            self.assertEqual(result['status'],'passed',result)
            self.assertEqual(result['attempts'][0]['reason'],'risk_review_required')
            self.assertEqual(result['model_calls'],4)
            self.assertTrue((session.output/'attempt-02/verification/reports/pytest.xml').exists())
            self.assertFalse((session.output/'attempt-01/verification').exists())
            self.assertEqual((base/'workspace/pricing.py').read_text(),source)
