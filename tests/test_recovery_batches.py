"""Tool-round errors must survive mixed results and shared budget limits."""
import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from test_agent import ScriptedModel, fixture
from repair_workbench.agent import BudgetedModel,run_agent
from repair_workbench.session import RepairSession
from repair_workbench.tool_recovery import collect_errors


def mixed(identity, reverse=False, failures=1):
    calls=[{'name':'submit_ref_edits','args':{'edits':[{'source_ref':12,'old_text':'x','new_text':'y'}],
             'diagnosis':'invalid type'},'id':f'{identity}-bad-{index}'} for index in range(failures)]
    calls.append({'name':'read_source','args':{'path':'pricing.py'},'id':f'{identity}-read'})
    return AIMessage(content='',tool_calls=list(reversed(calls)) if reverse else calls)


class RecoveryBatchTests(unittest.TestCase):
    def run_case(self,responses,limit=12):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);base=fixture(root)
        session=RepairSession(base,root/'agent',{'pricing.py'},profile='simple_edit',max_model_calls=limit)
        result=run_agent(session,ScriptedModel(responses=responses),'fix pricing')
        events=[json.loads(line) for line in (session.output/'events.jsonl').read_text().splitlines()]
        return result,events

    def test_mixed_order_and_multiple_failures(self):
        for reverse in [False,True]:
            with self.subTest(reverse=reverse):
                result,events=self.run_case([mixed('a',reverse,2),AIMessage(content='done')])
                self.assertEqual(result['submission_counts']['schema_rejections'],2)
                self.assertEqual(result['invalid_tool_recoveries'],1)
                self.assertEqual(sum(e['event']=='schema_rejected' for e in events),2)

    def test_same_error_with_successful_read_does_not_reset_loop_guard(self):
        result,_=self.run_case([mixed('a'),mixed('b',True)])
        self.assertEqual(result['stop_reason'],'repeated_schema_error')
        self.assertEqual(result['model_calls'],2)
        self.assertEqual(result['submission_counts']['schema_rejections'],2)

    def test_errors_are_counted_when_model_budget_prevents_recovery(self):
        result,_=self.run_case([mixed('a')],limit=1)
        self.assertEqual(result['stop_reason'],'model_call_limit')
        self.assertEqual(result['submission_counts']['schema_rejections'],1)
        self.assertEqual(result['invalid_tool_recoveries'],0)

    def test_different_errors_exhaust_shared_recovery_budget(self):
        responses=[]
        for index,field in enumerate(['source_ref','old_text','new_text']):
            edit={'source_ref':'ref','old_text':'old','new_text':'new'};edit[field]=[]
            responses.append(AIMessage(content='',tool_calls=[{'name':'submit_ref_edits',
                'args':{'edits':[edit],'diagnosis':'fix'},'id':f'bad-{index}'},
                {'name':'list_sources','args':{},'id':f'good-{index}'}]))
        result,_=self.run_case(responses)
        self.assertEqual(result['stop_reason'],'invalid_tool_recovery_limit')
        self.assertEqual(result['model_calls'],3)
        self.assertEqual(result['invalid_tool_recoveries'],2)
        self.assertEqual(result['submission_counts']['schema_rejections'],3)

    def test_receipts_ignore_old_unknown_and_duplicate_results(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);s=RepairSession(fixture(root),root/'agent',{'pricing.py'},profile='simple_edit')
        message=mixed('a');s.active_tool_round={'call':1,'ids':{call['id']:call['name'] for call in message.tool_calls}}
        bad=ToolMessage(content=' edits.0.source_ref: Input should be a valid string',
                        tool_call_id='a-bad-0',name='submit_ref_edits',status='error')
        unknown=bad.model_copy(update={'tool_call_id':'outside-round'})
        history=[unknown,message,bad,bad,ToolMessage(content='ok',tool_call_id='a-read')]
        self.assertEqual(len(collect_errors(s,history)),1)
        self.assertEqual(collect_errors(s,history),[])
        self.assertEqual(s.submission_counts['schema_rejections'],1)

    def test_mixed_valid_and_unparseable_calls_recover_once(self):
        first=AIMessage(content='',tool_calls=[{'name':'list_sources','args':{},'id':'good'}],
                        invalid_tool_calls=[{'name':'submit_ref_edits','args':'{','id':'bad','error':'JSON parse failed'}])
        result,_=self.run_case([first,AIMessage(content='done')])
        self.assertEqual(result['submission_counts']['schema_rejections'],1)
        self.assertEqual(result['invalid_tool_recoveries'],1)

    def test_framework_call_does_not_overwrite_unresolved_tool_round(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);s=RepairSession(fixture(root),root/'agent',{'pricing.py'},profile='simple_edit')
        s.active_tool_round={'call':1,'ids':{'pending':'submit_ref_edits'}}
        BudgetedModel(inner=ScriptedModel(responses=[AIMessage(content='summary')]),session=s).invoke('summarize')
        self.assertEqual(s.active_tool_round['ids'],{'pending':'submit_ref_edits'})
        self.assertEqual(s.model_calls,1)

    def test_source_integer_fields_do_not_coerce_booleans(self):
        bad=AIMessage(content='',tool_calls=[{'name':'read_source',
            'args':{'path':'pricing.py','start_line':True},'id':'bad-read'}])
        result,_=self.run_case([bad,AIMessage(content='done')])
        self.assertEqual(result['submission_counts']['schema_rejections'],1)
        self.assertEqual(result['invalid_tool_recoveries'],1)

    def test_schema_errors_and_call_denominators_are_classified(self):
        message=AIMessage(content='',tool_calls=[
            {'name':'submit_ref_edits','args':{'edits':False,'diagnosis':'fix'},'id':'edit'},
            {'name':'read_source','args':{'path':'pricing.py','start_line':True},'id':'read'},
            {'name':'search_source','args':{'query':[]},'id':'search'},
            {'name':'list_sources','args':{},'id':'ok'}])
        result,events=self.run_case([message,AIMessage(content='done')])
        stats=result['tool_statistics']
        self.assertEqual(stats['calls'],4)
        self.assertEqual(stats['schema_rejections'],3)
        self.assertEqual(stats['by_category']['edit'],{'calls':1,'schema_rejections':1})
        self.assertEqual(stats['by_category']['read_search'],{'calls':3,'schema_rejections':2})
        self.assertEqual(stats['by_tool']['search_source'],{'calls':1,'schema_rejections':1})
        self.assertEqual(result['submission_counts']['requests'],0)
        self.assertEqual(result['submission_counts']['schema_rejections'],3)
        self.assertEqual(len([e for e in events if e['event']=='schema_rejected']),3)

    def test_invalid_json_is_counted_under_its_tool_category(self):
        message=AIMessage(content='',invalid_tool_calls=[
            {'name':'read_source','args':'{','id':'bad','error':'invalid JSON'}])
        result,_=self.run_case([message,AIMessage(content='done')])
        self.assertEqual(result['tool_statistics']['by_category']['read_search'],
                         {'calls':1,'schema_rejections':1})
