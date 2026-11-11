"""Native repository diagnosis/public evidence/sealing boundaries; no model API."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from test_agent import ScriptedModel, call, fixture
from test_workload_contract import case, contract
from repair_workbench.agent import run_agent
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.edits import EditRejected
from repair_workbench.repository_diagnosis import RepositoryDiagnosticSession
from repair_workbench.repository_performance import original_suite_sha256, registered_implementations
from repair_workbench.session import BudgetExceeded
from repair_workbench.snapshot import snapshot_id, verify_snapshot
from repair_workbench.workload_contract import WorkloadSpec, canonical_json


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


IMAGE = 'sha256:' + 'a' * 64


class PublicProbeStub:
    """No execution: fixed app-side observations exercise only Session wiring."""
    def __init__(self, snapshot, output, image, development_workload, *, max_runs=6,
                 execution_control=None, admit=None, **kwargs):
        self.snapshot_ref = snapshot_id(verify_snapshot(snapshot))
        self.data, self.image_id = development_workload, image
        self.public_workload_sha256 = digest(canonical_json(self.data).encode())
        self.runs, self.cleanup_ok, self.blocked_reason = 0, True, None
        self.control, self.admit, self.cache = execution_control, admit, {}
        self.max_runs, self.order = max_runs, []
        self.physical_admit = kwargs.get('physical_admit')

    def run_probe(self, probe, case_id=None):
        self.admit('run_probe')
        self.control.check()
        if probe not in ('cpu', 'memory', 'benchmark'):
            raise ValueError('unknown_probe')
        selected = next((c for c in self.data['development_inputs']
                         if c['case_id'] == (case_id or self.data['development_inputs'][0]['case_id'])), None)
        if selected is None:
            raise ValueError('unknown_public_case')
        key = (probe, selected['case_id'])
        if key in self.cache:
            return {**self.cache[key], 'cache_hit': True, 'dispatched': False}
        if self.runs >= self.max_runs:
            raise BudgetExceeded('repository_probe_limit')
        if self.physical_admit is not None:
            self.physical_admit()
        self.runs += 1
        self.order.append(probe)
        result = {'status': 'ok', 'cleanup_ok': True, 'input_integrity_ok': True,
            'snapshot_ref': self.snapshot_ref, 'image_id': self.image_id,
            'public_workload_sha256': self.public_workload_sha256,
            'workload_id': self.data['workload_id'], 'probe': probe,
            'case_id': selected['case_id'], 'input_sha256': selected['sha256'],
            'adapter_sha256': 'a' * 64, 'cache_hit': False, 'dispatched': True,
            'report_ref': 'public-report-' + self.snapshot_ref[:8] + '-' + str(self.runs), 'report_sha256': 'b' * 64,
            'summary': {'metric': 'offline_fixed_observation', 'median': 100}}
        self.cache[key] = result
        return result

    def read_report(self, ref, section='summary', offset=0, limit=5):
        self.admit('read_report')
        self.control.check()
        if ref not in {v['report_ref'] for v in self.cache.values()}:
            raise ValueError('unknown_report')
        return {'report_ref': ref, 'section': section, 'offset': offset, 'data': [100], 'next_offset': None}


class RepositoryPolicy(ScriptedModel):
    read: dict = {}
    hypothesis: str = ''
    payload: dict = {}
    system_text: str = ''
    schemas: dict = Field(default_factory=dict)

    def bind_tools(self, tools, **kwargs):
        self.schemas = {t.name: t.args for t in tools}
        return super().bind_tools(tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.calls == 0:
            self.payload = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
            self.system_text = str(next(m.content for m in messages if m.type == 'system'))
            response = call('read_source', {'path': 'pricing.py'}, 'read')
        else:
            receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            if self.calls == 1:
                self.read = receipt
                response = call('record_hypothesis', {'claim': 'The public boundary may be excluded.',
                    'evidence_refs': [receipt['evidence_ref']], 'next_action': 'Measure allocation.'}, 'hypothesis')
            elif self.calls == 2:
                self.hypothesis = receipt['hypothesis_id']
                response = call('run_probe', {'probe': 'memory', 'hypothesis_id': self.hypothesis}, 'probe')
            elif self.calls == 3:
                response = call('record_hypothesis', {'claim': 'Public observations support an investigation.',
                    'status': 'supported', 'hypothesis_id': self.hypothesis,
                    'evidence_refs': [receipt['evidence_ref']]}, 'update')
            else:
                response = call('submit_ref_edits', {'edits': [{
                    'source_ref': self.read['source_ref'], 'old_text': 'amount > 100',
                    'new_text': 'amount >= 100'}], 'diagnosis': 'Offline wiring candidate.'}, 'submit')
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class RepositoryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = fixture(self.root)
        self.manifest = verify_snapshot(self.baseline)
        outcomes = {'tests/test_pricing.py::test_boundary': 'passed',
                    'tests/test_pricing.py::test_historical_xfail': 'xfailed',
                    'tests/test_pricing.py::test_historical_xpass': 'xpassed'}
        report = {'status': 'tests_passed', 'snapshot_id': snapshot_id(self.manifest),
            'image_id': IMAGE, 'cleanup_ok': True, 'counts': {'passed': 1, 'xfailed': 1, 'xpassed': 1},
            'test_inventory': {'outcomes': outcomes}}
        self.report_path = self.baseline / 'reports/result.json'
        self.report_path.write_text(json.dumps(report))
        (self.baseline / 'reports/pytest.xml').write_text('<testsuite><testcase classname="pricing" name="boundary"/></testsuite>')
        data = contract()
        adapters, checkers = registered_implementations()
        data.update(base_commit=self.manifest['commit'], workload_id='sql-comments-v3',
                    adapter_sha256=adapters['sql-comments-v3'])
        data['semantics'].update(checker_id='canonical-output-v1',
            checker_sha256=checkers['canonical-output-v1'], output_comparison='canonical_json', metamorphic_checks=[])
        data['development_inputs'] = [case('public-small', {'size': 3, 'kind': 'line_comments'})]
        data['final_inputs'] = [case('not-handed-to-session', {'size': 337, 'kind': 'quoted_markers'})]
        self.full_spec = WorkloadSpec(data, adapters=adapters, checkers=checkers)
        self.public = self.full_spec.development_projection()
        self.qualification = {'schema_version': 1, 'task_id': self.public['task_id'],
            'repo': self.public['repo'], 'base_commit': self.public['base_commit'],
            'baseline_ref': snapshot_id(self.manifest), 'image_id': IMAGE,
            'suite_sha256': original_suite_sha256(self.manifest),
            'report_sha256': digest(self.report_path.read_bytes()),
            'inventory_sha256': digest(canonical_json(outcomes).encode()), 'original_tests_accepted': True}

    def session(self, *, public=None, qualification=None, **kwargs):
        return RepositoryDiagnosticSession(self.baseline, self.root / 'agent', {'pricing.py'},
            self.public if public is None else public,
            self.qualification if qualification is None else qualification, **kwargs)

    def test_qualified_repository_accepts_preserved_original_xfail_xpass(self):
        session = self.session()
        self.assertEqual(session.before['test_inventory']['outcomes']['tests/test_pricing.py::test_historical_xfail'], 'xfailed')
        self.assertEqual(session.before['test_inventory']['outcomes']['tests/test_pricing.py::test_historical_xpass'], 'xpassed')
        self.assertNotIn('map_exporter.py', session.files)
        self.assertEqual(session.workload, 'sql-comments-v3')

    def test_private_workload_fields_and_full_spec_are_not_accepted(self):
        for value in (self.full_spec, {**self.public, 'final_inputs': [{'secret': 'HIDDEN_SENTINEL'}]},
                      {**self.public, 'checker_sha256': 'a' * 64}):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                self.session(public=value)
        self.assertFalse((self.root / 'agent').exists())

    def test_changed_qualification_or_report_is_rejected_before_session_creation(self):
        for field, value in (('baseline_ref', '1' * 64), ('suite_sha256', '2' * 64),
                             ('inventory_sha256', '3' * 64), ('report_sha256', '4' * 64),
                             ('original_tests_accepted', False), ('base_commit', 'a' * 40)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.session(qualification={**self.qualification, field: value})
        self.report_path.write_text(self.report_path.read_text() + ' ')
        with self.assertRaises(ValueError):
            self.session()

    def test_unsupported_options_fail_instead_of_silent_fallback(self):
        for kwargs in ({'diagnostic_transport': 'mcp'}, {'use_skills': True},
                       {'use_investigator': True}, {'tool_concurrency': 2},
                       {'tool_concurrency': True}, {'minimum_speedup': .01}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.session(**kwargs)
        self.assertFalse((self.root / 'agent').exists())

    def test_memory_goal_has_no_fake_speed_target_and_ugc_validation_stays_strict(self):
        data = copy.deepcopy(self.public)
        data['goal'] = 'memory'
        data['thresholds'].update(min_speedup=0, min_memory_reduction=.3)
        session = self.session(public=data)
        self.assertEqual(session.minimum_speedup, 0)
        plain = object.__new__(DiagnosticSession)
        for workload, speed in (('sql-comments-v3', .15), ('ugc-export-v1', 0), ('ugc-export-v1', True)):
            with self.subTest(workload=workload, speed=speed), self.assertRaises(ValueError):
                plain._validate_workload_configuration(workload, speed, 1.25)

    def test_native_graph_updates_hypothesis_and_seals_without_hidden_evaluation(self):
        session, model = self.session(), RepositoryPolicy()
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', PublicProbeStub), \
             patch('repair_workbench.repository_performance.verify_repository_candidate') as final:
            result = run_agent(session, model, 'Inspect the public symptom and preserve existing behavior.')
        final.assert_not_called()
        self.assertEqual(result['status'], 'submitted', result)
        self.assertEqual(result['stop_reason'], 'repository_submission_sealed')
        self.assertEqual(result['model_calls'], 5)
        self.assertEqual(session.hypotheses['H001']['status'], 'supported')
        self.assertEqual(result['submission_counts']['docker_verifications'], 0)
        self.assertEqual(result['selected_attempt'], 1)
        receipt_path = session.output / result['final_submission_ref']
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(digest(receipt_path.read_bytes()), result['final_submission_sha256'])
        self.assertEqual(receipt['candidate_path'], 'attempt-01/verification')
        self.assertEqual(receipt['candidate_ref'], snapshot_id(verify_snapshot(session.output / receipt['candidate_path'])))
        self.assertNotIn('not-handed-to-session', json.dumps(model.payload))
        self.assertNotIn('自动独立重建并复验原测试', model.system_text)
        self.assertNotIn('attempt', model.schemas['run_probe'])
        self.assertNotIn('scale', model.schemas['read_probe_report'])
        self.assertFalse((session.output / 'attempt-01/verification/reports').exists())
        for action in (session.reserve_model, session.list_sources,
                       lambda: session.run_diagnostic('cpu')):
            with self.assertRaisesRegex(BudgetExceeded, 'repository_submission_sealed'):
                action()

    def test_same_probe_reuses_evidence_but_every_call_uses_root_budget(self):
        session = self.session()
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', PublicProbeStub):
            first = session.run_diagnostic('memory')
            second = session.run_diagnostic('memory')
            page = session.read_probe_report(first['evidence_ref'])
        self.assertEqual(first['evidence_ref'], second['evidence_ref'])
        self.assertTrue(second['reused'])
        self.assertEqual(session.probe_runs, 1)
        self.assertEqual(session.tool_calls, 3)
        self.assertEqual(page['data'], [100])

    def test_report_query_cache_cannot_skip_workspace_integrity_check(self):
        session = self.session()
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', PublicProbeStub):
            evidence = session.run_diagnostic('cpu')
            session.read_probe_report(evidence['evidence_ref'])
            with patch.object(session.repository_probe_workspace, 'read_report',
                              side_effect=ValueError('public_report_changed')):
                with self.assertRaisesRegex(ValueError, 'public_report_changed'):
                    session.read_probe_report(evidence['evidence_ref'])
        self.assertEqual(session.tool_calls, 3)

    def test_fixed_group_executes_three_real_registered_requests_before_model(self):
        session = self.session(strategy='fixed')
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', PublicProbeStub):
            session.prepare_fixed_diagnostics()
        self.assertEqual(session.repository_probe_workspace.order, ['cpu', 'memory', 'benchmark'])
        self.assertEqual(session.tool_calls, 3)
        self.assertEqual(session.probe_runs, 3)
        self.assertTrue(session.fixed_diagnostics_done)
        self.assertEqual([item['observation']['probe'] for item in session.initial_context()['diagnostic_summaries']],
                         ['cpu', 'memory', 'benchmark'])
        session.prepare_fixed_diagnostics()
        self.assertEqual(session.probe_runs, 3)

    def test_conclusion_seals_review_proposal_without_claiming_a_pass(self):
        session = self.session()
        ref = next(iter(session.evidence))
        result = session.conclude_diagnosis('insufficient_evidence', 'Need a representative runtime input.', [ref])
        self.assertEqual(result['status'], 'proposal_sealed')
        saved = session.save('completed')
        self.assertEqual(saved['status'], 'proposal_sealed')
        self.assertEqual(saved['reason'], 'awaiting_independent_review')
        self.assertEqual(session.final_submission['decision'], 'insufficient_evidence')
        with self.assertRaisesRegex(BudgetExceeded, 'repository_submission_sealed'):
            session.reserve_model()

    def test_sealing_preserves_candidate_crlf_bytes_and_exact_snapshot_identity(self):
        session = self.session()
        original = (session.baseline / 'workspace/pricing.py').read_bytes().decode('utf-8')
        changed = original.replace('\n', '\r\n') + '# candidate comment\r\n'
        result = session.submit_candidate({'pricing.py': changed}, 'Preserve exact candidate bytes.')
        self.assertEqual(result['status'], 'submitted', result)
        proposed = session.output / 'attempt-01/candidate'
        rebuilt = session.output / 'attempt-01/verification'
        self.assertEqual((rebuilt / 'workspace/pricing.py').read_bytes(), changed.encode())
        self.assertEqual(snapshot_id(verify_snapshot(proposed)), snapshot_id(verify_snapshot(rebuilt)))
        self.assertEqual(session.final_submission['candidate_ref'], snapshot_id(verify_snapshot(proposed)))

    def test_probe_cleanup_unknown_on_exception_remains_in_final_session_receipt(self):
        session = self.session()
        class InterruptedProbe(PublicProbeStub):
            def run_probe(manager, *args, **kwargs):
                manager.cleanup_ok = False
                raise KeyboardInterrupt()
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', InterruptedProbe):
            with self.assertRaises(KeyboardInterrupt):
                session.run_diagnostic('cpu')
        with self.assertRaisesRegex(BudgetExceeded, 'repository_probe_cleanup_unconfirmed'):
            session.reserve_model()
        result = session.save('stopped', stop_reason='cancel_requested')
        self.assertFalse(result['cleanup_ok'])
        self.assertFalse(result['probe_cleanup_ok'])
        with self.assertRaises(BudgetExceeded):
            session.reserve_model()

    def test_unissued_evidence_and_candidate_probe_attempt_cannot_dispatch(self):
        session = self.session()
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace') as factory:
            with self.assertRaises(EditRejected):
                session.run_diagnostic('memory', attempt=1)
            with self.assertRaises(EditRejected):
                session.read_probe_report('E9999')
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
