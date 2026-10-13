"""Simple edit tool contract, reference scope and bounded invalid-call handling."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from test_agent import ScriptedModel, call, fixture
from repair_workbench.agent import BudgetedModel, run_agent
from repair_workbench.edits import EditRejected
from repair_workbench.session import RepairSession,BudgetExceeded


class SchemaCaptureModel(ScriptedModel):
    schemas: dict = Field(default_factory=dict)

    def bind_tools(self, tools, **kwargs):
        self.schemas = {item.name: convert_to_openai_tool(item) for item in tools}
        self.exposed = list(self.schemas)
        return self


class RefReplayModel(ScriptedModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.calls == 0:
            response = call('read_source',{'path':'pricing.py'},'read')
        elif self.calls == 1:
            tool_message = next(message for message in reversed(messages)
                                if isinstance(message, ToolMessage))
            ref=json.loads(tool_message.content)['source_ref']
            response=call('submit_ref_edits', {'edits':[{'source_ref':ref,
                'old_text':'amount > 100','new_text':'amount >= 100'}],
                'diagnosis':'boundary should include 100'}, 'submit')
        else:
            response=AIMessage(content='Public test scope passed; human review is required.')
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class SimpleEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = fixture(self.root)
        self.session = RepairSession(self.baseline, self.root/'agent', {'pricing.py'}, profile='simple_edit')
        self.original = (self.baseline/'workspace/pricing.py').read_text()

    def read(self, **kwargs):
        return self.session.read_source('pricing.py', raw=True, **kwargs)

    def test_ref_edits_reuse_existing_preflight_and_verification(self):
        ref = self.read()['source_ref']
        edit = {'source_ref':ref,'old_text':'amount > 100','new_text':'amount >= 100'}
        with patch('repair_workbench.session.verify_candidate', return_value={
                'status':'failed','reason':'target_not_fixed'}) as verify:
            result = self.session.submit_ref_edits([edit], 'boundary diagnosis')
        self.assertEqual(result['phase'], 'verification')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(verify.call_count, 1)
        self.assertEqual((self.session.output/'attempt-01/candidate/workspace/pricing.py').read_text(),
                         self.original.replace('amount > 100','amount >= 100'))
        self.assertEqual(self.session.submission_counts['materialized'], 1)
        self.assertEqual(self.session.submission_counts['docker_verifications'], 1)

    def test_cross_session_unknown_and_unread_range_rejected(self):
        ref = self.read(start_line=1,line_count=1)['source_ref']
        result = self.session.submit_ref_edits([{'source_ref':ref,'old_text':'amount > 100',
                                                  'new_text':'amount >= 100'}], 'outside read')
        self.assertEqual(result['reason'], 'text_outside_read_range')
        other = RepairSession(self.baseline, self.root/'other', {'pricing.py'}, profile='simple_edit')
        result = other.submit_ref_edits([{'source_ref':ref,'old_text':'amount > 100',
                                          'new_text':'amount >= 100'}], 'foreign ref')
        self.assertEqual(result['reason'], 'invalid_source_ref')
        self.assertEqual(other.submission_counts['materialized'], 0)

    def test_ambiguous_overlap_path_escape_and_version_change(self):
        ref = self.read()['source_ref']
        base = {'source_ref':ref,'old_text':'amount > 100','new_text':'amount >= 100'}
        cases = [([dict(base,old_text=' ')], 'text_not_unique_in_read_range'),
                 ([base,base], 'overlapping_edits'),
                 ([dict(base,source_ref='../pricing.py')], 'invalid_source_ref')]
        for index, (edits, reason) in enumerate(cases):
            session = RepairSession(self.baseline,self.root/f'case-{index}',{'pricing.py'},profile='simple_edit')
            own_ref = session.read_source('pricing.py',raw=True)['source_ref']
            edits = [dict(item,source_ref=own_ref) if item['source_ref']==ref else item for item in edits]
            self.assertEqual(session.submit_ref_edits(edits,'boundary')['reason'], reason)
            self.assertFalse((session.output/'attempt-01/candidate').exists())
        (self.baseline/'workspace/pricing.py').write_text('tampered')
        # The existing reference cannot bypass snapshot integrity after baseline changes.
        with self.assertRaises(EditRejected) as error:
            self.session._resolve_ref_edits([base])
        self.assertEqual(error.exception.reason,'baseline_version_changed')

    def test_schema_sent_to_provider_is_typed_and_minimal(self):
        model = SchemaCaptureModel(responses=[AIMessage(content='cannot fix')])
        result = run_agent(self.session,model,'fix pricing')
        self.assertEqual(result['status'],'inconclusive')
        schema = model.schemas['submit_ref_edits']['function']['parameters']
        self.assertEqual(schema['required'], ['edits','diagnosis'])
        edit = schema['properties']['edits']['items']
        if '$ref' in edit:
            edit = schema['$defs'][edit['$ref'].split('/')[-1]]
        self.assertEqual(set(edit['required']), {'source_ref','old_text','new_text'})
        self.assertNotIn('sha256', json.dumps(schema))
        self.assertNotIn('start_line', json.dumps(schema))
        self.assertNotIn('submit_edits', model.exposed)
        self.assertNotIn('raw', model.schemas['read_source']['function']['parameters']['properties'])

    def test_invalid_json_recovers_inside_shared_budget(self):
        bad = AIMessage(content='',invalid_tool_calls=[{'name':'submit_ref_edits',
            'args':'{"edits":','id':'bad','error':'JSON parse failed'}],
            usage_metadata={'input_tokens':4,'output_tokens':3,'total_tokens':7})
        model=ScriptedModel(responses=[bad,AIMessage(content='unable to repair',
            usage_metadata={'input_tokens':5,'output_tokens':2,'total_tokens':7})])
        result=run_agent(self.session,model,'fix pricing')
        self.assertEqual(result['model_calls'],2)
        self.assertEqual(result['token_usage']['input_tokens'],9)
        self.assertEqual(result['submission_counts']['schema_rejections'],1)
        self.assertEqual(result['completion_diagnosis'],'no_candidate_cause_unconfirmed')

    def test_truncated_tool_output_is_not_repaired_or_submitted(self):
        bad=AIMessage(content='',response_metadata={'finish_reason':'length'},
            invalid_tool_calls=[{'name':'submit_ref_edits','args':'{"edits":',
                                 'id':'cut','error':'JSON parse failed'}])
        result=run_agent(self.session,ScriptedModel(responses=[bad]),'fix pricing')
        self.assertEqual(result['model_calls'],1)
        self.assertEqual(result['completion_diagnosis'],'no_candidate_length_finish_observed')
        self.assertEqual(result['submission_counts']['requests'],0)
        events=[json.loads(line) for line in (self.session.output/'events.jsonl').read_text().splitlines()]
        self.assertIn('model_output_truncated',[event.get('error_kind') for event in events])

    def test_repeated_invalid_call_stops_without_unbounded_retry(self):
        bad=AIMessage(content='',invalid_tool_calls=[{'name':'submit_ref_edits',
            'args':'secret','id':'bad','error':'JSON parse failed'}])
        result=run_agent(self.session,ScriptedModel(responses=[bad]),'fix pricing')
        self.assertEqual(result['stop_reason'],'repeated_invalid_tool_call')
        self.assertEqual(result['model_calls'],2)
        self.assertNotIn('secret',(self.session.output/'events.jsonl').read_text())

    def test_schema_error_feedback_and_repeated_error_stop(self):
        invalid = call('submit_ref_edits', {'edits':[{'source_ref':12,
            'old_text':'x','new_text':'y'}], 'diagnosis':'bad type'}, 'bad')
        result = run_agent(self.session, ScriptedModel(responses=[invalid]), 'fix pricing')
        self.assertEqual(result['stop_reason'], 'repeated_schema_error')
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['submission_counts']['schema_rejections'], 2)
        self.assertEqual(result['submission_counts']['requests'], 0)
        schema_events=[json.loads(line) for line in (self.session.output/'events.jsonl').read_text().splitlines()
                       if '"schema_rejected"' in line]
        self.assertEqual(schema_events[0]['field'],'edits.0.source_ref')
        self.assertEqual(schema_events[0]['expected_type'],'string')
        shape_events=[json.loads(line) for line in (self.session.output/'events.jsonl').read_text().splitlines()
                      if '"tool_call_shape"' in line]
        self.assertEqual(shape_events[0]['first_edit_fields'],['new_text','old_text','source_ref'])
        self.assertNotIn('bad type',json.dumps(shape_events))

    def test_authentication_failure_does_not_retry(self):
        with patch.object(ScriptedModel, 'invoke', side_effect=ValueError(
                'status_code: 401\ncode: InvalidApiKey\nrequest_id: secret-request')):
            result = run_agent(self.session, ScriptedModel(), 'fix pricing')
        self.assertEqual(result['stop_reason'], 'provider_account_unavailable')
        self.assertEqual(result['error_type'], 'AuthenticationFailed')
        self.assertEqual(result['model_calls'], 1)
        self.assertNotIn('secret-request',(self.session.output/'events.jsonl').read_text())

    def test_reference_expires_when_session_finishes(self):
        ref=self.read()['source_ref']
        self.session.save('completed')
        with self.assertRaises(EditRejected) as error:
            self.session.submit_ref_edits([{'source_ref':ref,
                'old_text':'amount > 100','new_text':'amount >= 100'}], 'late edit')
        self.assertEqual(error.exception.reason,'expired_source_ref')
        with self.assertRaisesRegex(BudgetExceeded,'session_closed'):
            self.session.read_symbol('pricing.py','discounted_price')
        self.assertEqual(json.loads((self.session.output/'result.json').read_text())['state'],'completed')

    def test_deterministic_agent_replay_uses_actual_read_reference(self):
        with patch('repair_workbench.session.verify_candidate', return_value={
                'status':'passed','reason':'passed_original_test_scope'}):
            result=run_agent(self.session,RefReplayModel(),'fix pricing boundary')
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['submission_counts']['requests'],1)
        self.assertEqual(result['submission_counts']['schema_rejections'],0)
        self.assertEqual(result['submission_counts']['materialized'],1)


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'requires local Docker image')
class SimpleEditDockerTests(unittest.TestCase):
    def test_ref_edit_reaches_isolated_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            baseline=fixture(root,real=True)
            original=(baseline/'workspace/pricing.py').read_text()
            session=RepairSession(baseline,root/'agent',{'pricing.py'},profile='simple_edit')
            result=run_agent(session,RefReplayModel(),'fix pricing boundary')
            self.assertEqual(result['status'],'passed',result)
            self.assertEqual(session.submission_counts['docker_verifications'],1)
            self.assertEqual((root/'source/pricing.py').read_text(),original)
            self.assertTrue((root/'agent/attempt-01/verification/patch.diff').exists())
