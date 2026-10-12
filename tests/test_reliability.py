"""Version-bound editing, preflight rejection and bounded recovery contracts."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agent import fixture, ScriptedModel, call, AIMessage
from repair_workbench.agent import BudgetedModel, run_agent
from repair_workbench.edits import EditRejected, apply_edits
from repair_workbench.session import RepairSession, BudgetExceeded


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = fixture(self.root)
        self.session = RepairSession(self.baseline, self.root/'agent', {'pricing.py'})
        self.source = (self.baseline/'workspace/pricing.py').read_text()

    def edit(self, old='amount > 100', new='amount >= 100', **extra):
        return {'path':'pricing.py', 'sha256':hashlib.sha256(self.source.encode()).hexdigest(),
                'start_line':self.source[:self.source.index(old)].count('\n')+1,
                'old_text':old, 'new_text':new, **extra}

    def test_exact_edit_preserves_all_other_bytes(self):
        result = apply_edits(self.baseline, [self.edit()], {'pricing.py'})
        self.assertEqual(result['pricing.py'], self.source.replace('amount > 100','amount >= 100'))
        self.assertEqual((self.baseline/'workspace/pricing.py').read_text(),self.source)

    def test_raw_content_and_version_match(self):
        result=self.session.read_source('pricing.py',raw=True)
        self.assertEqual(result['content'],self.source)
        self.assertEqual(result['sha256'],self.edit()['sha256'])
        self.assertIn('1:', self.session.read_source('pricing.py')['content'])

    def test_versions_lines_paths_and_matches_rejected(self):
        cases=[({'sha256':'0'*64},'file_version_mismatch'),
               ({'start_line':999},'line_mismatch'),
               ({'old_text':'missing'},'text_not_found'),
               ({'old_text':' '},'text_not_unique'),
               ({'path':'../pricing.py'},'path_not_allowed'),
               ({'path':'tests/test_pricing.py'},'path_not_allowed'),
               ({'path':'other.py'},'path_not_allowed'),
               ({'start_line':True},'invalid_edit_content')]
        for fields,reason in cases:
            with self.subTest(reason=reason), self.assertRaises(EditRejected) as ctx:
                apply_edits(self.baseline,[self.edit(**fields)],{'pricing.py'})
            self.assertEqual(ctx.exception.reason,reason)

    def test_overlap_and_noop_rejected(self):
        with self.assertRaises(EditRejected) as ctx:
            apply_edits(self.baseline,[self.edit(),self.edit()],{'pricing.py'})
        self.assertEqual(ctx.exception.reason,'overlapping_edits')
        with self.assertRaises(EditRejected) as ctx:
            apply_edits(self.baseline,[self.edit(new='amount > 100')],{'pricing.py'})
        self.assertEqual(ctx.exception.reason,'empty_patch')

    def test_nonoverlapping_edits_use_original_coordinates(self):
        first=self.edit(new='amount >= 100')
        # Use the unique function name as a second, independent context anchor.
        name=self.source.split('def ')[1].split('(')[0]
        second=self.edit(old='def '+name,new='# unchanged API\ndef '+name)
        result=apply_edits(self.baseline,[second,first],{'pricing.py'})['pricing.py']
        self.assertEqual(result,self.source.replace('amount > 100','amount >= 100').replace('def '+name,'# unchanged API\ndef '+name))

    def test_indentation_and_duplicate_consume_attempts_without_docker(self):
        bad=' '+self.source
        with patch('repair_workbench.session.verify_candidate') as verify:
            first=self.session.submit_candidate({'pricing.py':bad},'indent mistake')
            second=self.session.submit_candidate({'pricing.py':bad},'same mistake')
            verify.assert_not_called()
        self.assertEqual(first['reason'],'syntax_error')
        self.assertEqual(first['errors'][0]['error_type'],'IndentationError')
        self.assertEqual(second['reason'],'duplicate_candidate')
        self.assertEqual(second['previous_attempt'],1)
        self.assertEqual(len(self.session.attempts),2)
        self.assertIn('preflight',self.session.read_test_log(attempt=1))

    def test_line_number_pollution_is_not_repaired_silently(self):
        bad='\n'.join(f'{i}: {line}' for i,line in enumerate(self.source.splitlines(),1))
        with patch('repair_workbench.session.verify_candidate') as verify:
            result=self.session.submit_candidate({'pricing.py':bad},'numbered source')
            verify.assert_not_called()
        self.assertEqual(result['reason'],'syntax_error')
        self.assertEqual((self.session.output/'attempt-01/candidate/workspace/pricing.py').read_text(),bad)

    def test_stale_edit_consumes_budget_and_returns_specific_feedback(self):
        with patch('repair_workbench.session.verify_candidate') as verify:
            for _ in range(3):
                result=self.session.submit_edits([self.edit(sha256='stale')],'stale edit')
                self.assertEqual(result['reason'],'file_version_mismatch')
            with self.assertRaises(BudgetExceeded):
                self.session.submit_edits([self.edit()],'too late')
            verify.assert_not_called()

    def test_syntax_precheck_never_executes_candidate(self):
        marker=self.root/'must-not-exist'
        source=f"open({str(marker)!r}, 'w').write('executed')\n"
        with patch('repair_workbench.session.verify_candidate', return_value={'status':'failed','reason':'target_not_fixed'}):
            self.session.submit_candidate({'pricing.py':source},'precheck must only compile')
        self.assertFalse(marker.exists())

    def test_observations_record_length_usage_without_sensitive_arguments(self):
        message=AIMessage(content='unfinished', response_metadata={'finish_reason':'length'},
            usage_metadata={'input_tokens':10,'output_tokens':20,'total_tokens':30},
            invalid_tool_calls=[{'name':'submit_edits','args':'secret-marker','id':'bad','error':'secret-error'}])
        model=ScriptedModel(responses=[message])
        BudgetedModel(inner=model,session=self.session).invoke('diagnose')
        events=(self.session.output/'events.jsonl').read_text()
        self.assertNotIn('secret-marker',events);self.assertNotIn('secret-error',events)
        observed=self.session.last_model_observation
        self.assertEqual(observed['finish_reason'],'length')
        self.assertEqual(observed['invalid_tool_call_count'],1)
        self.assertEqual(observed['input_tokens'],10)
        self.assertEqual(json.loads((self.session.output/'result.json').read_text())['model_calls'],1)

    def test_no_candidate_stop_length_unknown_do_not_auto_retry(self):
        for reason in ['stop','length',None]:
            with self.subTest(reason=reason):
                session=RepairSession(self.baseline,self.root/str(reason),{'pricing.py'})
                result=run_agent(session,ScriptedModel(responses=[AIMessage(content='will fix',
                    response_metadata={} if reason is None else {'finish_reason':reason})]),'fix')
                self.assertEqual(result['model_calls'],1)
                self.assertEqual(result['reason'],'no_candidate')
                self.assertIn(reason or 'unconfirmed',result['completion_diagnosis'])

    def test_provider_arrearage_stops_without_leaking_response(self):
        secret='secret-provider-request-id'
        failure=ValueError(f'request_id: {secret}\nstatus_code: 400\ncode: Arrearage\nmessage: confidential')
        with patch.object(ScriptedModel,'invoke',side_effect=failure):
            result=run_agent(self.session,ScriptedModel(responses=[AIMessage(content='never')]),'fix boundary')
        self.assertEqual(result['stop_reason'],'provider_account_unavailable')
        self.assertEqual(result['error_type'],'Arrearage')
        self.assertEqual(result['model_calls'],1)
        self.assertEqual(result['token_usage']['reports'],0)
        self.assertEqual(result['attempts'],[])
        events=(self.session.output/'events.jsonl').read_text()
        self.assertIn('Arrearage',events)
        self.assertNotIn(secret,events)
        self.assertNotIn('confidential',events)

    def test_tongyi_parser_exposes_invalid_json_tool_call(self):
        from langchain_community.chat_models.tongyi import convert_dict_to_message
        message=convert_dict_to_message({'role':'assistant','content':'', 'tool_calls':[
            {'id':'x','function':{'name':'submit_edits','arguments':'{"edits":'}}]})
        self.assertEqual(len(message.invalid_tool_calls),1)
        self.assertEqual(message.tool_calls,[])


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS')=='1','requires Docker')
class ReliabilityDockerTests(unittest.TestCase):
    def test_syntax_feedback_then_local_fix_and_real_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=fixture(root,real=True)
            source=(base/'workspace/pricing.py').read_text()
            sha=hashlib.sha256(source.encode()).hexdigest()
            edit={'path':'pricing.py','sha256':sha,'start_line':source[:source.index('amount > 100')].count('\n')+1,
                  'old_text':'amount > 100','new_text':'amount >= 100'}
            responses=[call('submit_candidate',{'changes':{'pricing.py':' '+source},'diagnosis':'bad indent'},'bad'),
                call('read_test_log',{'attempt':1},'feedback'),
                call('submit_edits',{'edits':[edit],'diagnosis':'exact local fix'},'good'),AIMessage(content='done')]
            result=run_agent(RepairSession(base,root/'agent',{'pricing.py'}),ScriptedModel(responses=responses),'fix boundary')
            self.assertEqual([a['reason'] for a in result['attempts']],['syntax_error','passed_original_test_scope'])
            self.assertEqual(result['status'],'passed')
            self.assertFalse((root/'agent/attempt-01/verification').exists())
            self.assertTrue((root/'agent/attempt-02/verification/reports/pytest.xml').exists())
            self.assertEqual((root/'source/pricing.py').read_text(),source)


class AdditionalBoundaryTests(unittest.TestCase):
    setUp = ReliabilityTests.setUp
    edit = ReliabilityTests.edit

    def test_batch_rejection_is_atomic(self):
        result=self.session.submit_edits([self.edit(),self.edit(path='../outside.py')],'atomic edit')
        self.assertEqual(result['reason'],'path_not_allowed')
        self.assertFalse((self.session.output/'attempt-01/candidate').exists())
        self.assertEqual((self.baseline/'workspace/pricing.py').read_text(),self.source)

    def test_duplicate_across_compatibility_and_local_tool(self):
        bad=self.source.replace('amount > 100','amount >')
        with patch('repair_workbench.session.verify_candidate') as verify:
            first=self.session.submit_candidate({'pricing.py':bad},'broken expression')
            second=self.session.submit_edits([self.edit(new='amount >')],'same via local tool')
            verify.assert_not_called()
        self.assertEqual(first['reason'],'syntax_error')
        self.assertEqual(second['reason'],'duplicate_candidate')

    def test_noop_full_file_and_protected_file_feedback(self):
        self.assertEqual(self.session.submit_candidate({'pricing.py':self.source},'noop')['reason'],'empty_patch')
        self.assertEqual(self.session.submit_candidate({'tests/test_pricing.py':'pass'},'bad path')['reason'],'path_not_allowed')

    def test_tampered_baseline_cannot_be_locally_edited(self):
        (self.baseline/'workspace/pricing.py').write_text('tampered')
        with self.assertRaises(ValueError):
            apply_edits(self.baseline,[self.edit()],{'pricing.py'})
