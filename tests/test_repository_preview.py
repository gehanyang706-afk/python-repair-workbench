"""Public trial isolation, shared budget and single immutable final selection."""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import test_repository_diagnosis as helpers
from test_agent import ScriptedModel, call
from repair_workbench.agent import run_agent
from repair_workbench.edits import EditRejected
from repair_workbench.session import BudgetExceeded


def edits(source_ref):
    return [{'source_ref': source_ref, 'old_text': 'amount > 100', 'new_text': 'amount >= 100'}]


class PreviewPolicy(ScriptedModel):
    preview_ref: str = ''

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tool = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
        previous = json.loads(tool.content) if tool is not None else {}
        if self.calls == 0:
            response = call('read_source', {'path': 'pricing.py'}, 'read')
        elif self.calls == 1:
            response = call('preview_ref_edits', {'edits': edits(previous['source_ref']),
                'diagnosis': 'Controlled public candidate test.'}, 'stage')
        elif self.calls == 2:
            assert previous['status'] == 'preview_ready'
            self.preview_ref = previous['preview_ref']
            response = call('run_preview_probe', {'preview_ref': self.preview_ref, 'probe': 'cpu'}, 'probe')
        elif self.calls == 3:
            assert previous['observation']['preview_ref'] == self.preview_ref
            response = call('read_probe_report', {'evidence_ref': previous['evidence_ref'],
                                                 'section': 'top_functions'}, 'recall')
        else:
            assert previous['report_ref'].startswith('public-report-')
            response = call('finalize_preview', {'preview_ref': self.preview_ref}, 'seal')
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


class RepositoryPreviewTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.RepositoryDiagnosisTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def stage(self, session):
        source = session.read_source('pricing.py', raw=True)
        return session.preview_ref_edits(edits(source['source_ref']), 'Public trial fixture.')

    def test_preview_is_opt_in_and_rejects_direct_disabled_invocation(self):
        session = self.h.session()
        model = ScriptedModel()
        from repair_workbench.diagnostic_agent import build_diagnostic_agent
        build_diagnostic_agent(session, model)
        self.assertNotIn('preview_ref_edits', model.exposed)
        with self.assertRaisesRegex(EditRejected, 'candidate_preview_disabled'):
            session.preview_ref_edits([], 'disabled')

    def test_native_trial_report_and_finalization_do_not_run_hidden_grader(self):
        session, model = self.h.session(enable_candidate_preview=True, max_attempts=1), PreviewPolicy()
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', helpers.PublicProbeStub), \
             patch('repair_workbench.repository_performance.verify_repository_candidate') as grade:
            result = run_agent(session, model, 'Check a public boundary and keep the chosen candidate.')
        grade.assert_not_called()
        self.assertEqual(result['status'], 'submitted', result)
        self.assertEqual(result['selected_attempt'], 1)
        self.assertEqual(model.calls, 5)
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['diagnostic_probe_runs'], 1)
        self.assertNotIn('final_inputs', json.dumps(result))
        self.assertTrue(result['cleanup_ok'])
        with self.assertRaisesRegex(BudgetExceeded, 'repository_submission_sealed'):
            session.reserve_model()

    def test_baseline_and_candidate_share_physical_limit_but_cached_read_is_charged(self):
        session = self.h.session(enable_candidate_preview=True, max_probe_runs=3)
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace', helpers.PublicProbeStub):
            trial = self.stage(session)
            self.assertEqual(trial['status'], 'preview_ready')
            self.assertFalse((session.output / 'final-submission.json').exists())
            session.run_diagnostic('cpu');session.run_diagnostic('memory')
            first = session.run_preview_probe(trial['preview_ref'], 'benchmark')
            with self.assertRaisesRegex(EditRejected, 'repository_shared_probe_limit'):
                session.run_preview_probe(trial['preview_ref'], 'memory')
            before = session.tool_calls
            cached = session.run_preview_probe(trial['preview_ref'], 'benchmark')
            self.assertTrue(cached['reused'])
            self.assertEqual(cached['evidence_ref'], first['evidence_ref'])
            self.assertEqual(session.tool_calls, before + 1)
            self.assertEqual(session.probe_runs, 3)
            self.assertEqual(first['snapshot_id'], trial['candidate_ref'])
            result = session.finalize_preview(trial['preview_ref'])
        self.assertEqual(result['status'], 'submitted')
        self.assertIn('amount > 100', (session.baseline / 'workspace/pricing.py').read_text())

    def test_patch_drift_cannot_be_finalized_even_when_source_is_unchanged(self):
        session = self.h.session(enable_candidate_preview=True)
        trial = self.stage(session)
        patch_path = session.output / 'attempt-01/verification/patch.diff'
        patch_path.write_bytes(patch_path.read_bytes() + b'\n')
        with self.assertRaisesRegex(BudgetExceeded, 'repository_preview_changed'):
            session.finalize_preview(trial['preview_ref'])
        self.assertFalse((session.output / 'final-submission.json').exists())
        with self.assertRaises(BudgetExceeded):
            session.reserve_model()

    def test_manifest_only_drift_blocks_before_public_probe_dispatch(self):
        session = self.h.session(enable_candidate_preview=True)
        trial = self.stage(session)
        manifest = session.output / 'attempt-01/verification/manifest.json'
        manifest.write_bytes(manifest.read_bytes() + b' ')
        with patch('repair_workbench.repository_probes.RepositoryProbeWorkspace') as workspace:
            with self.assertRaisesRegex(BudgetExceeded, 'repository_preview_changed'):
                session.run_preview_probe(trial['preview_ref'], 'cpu')
        workspace.assert_not_called()

    def test_reference_cannot_select_a_path_or_unregistered_preview(self):
        session = self.h.session(enable_candidate_preview=True)
        for ref in ('../../evaluator', 'rpv-not-issued'):
            with self.subTest(ref=ref), self.assertRaisesRegex(EditRejected, 'unknown_candidate_preview'):
                session.run_preview_probe(ref, 'cpu')
        self.assertEqual(session.probe_runs, 0)
        self.assertEqual(session.tool_calls, 2)

    def test_trial_can_be_abandoned_and_abstention_is_only_a_proposal(self):
        session = self.h.session(enable_candidate_preview=True)
        self.assertEqual(self.stage(session)['status'], 'preview_ready')
        result = session.conclude_diagnosis('insufficient_evidence', 'The trial is not justified.', ['E0001'])
        self.assertEqual(result['status'], 'proposal_sealed')
        self.assertEqual(session.save('completed')['status'], 'proposal_sealed')
        self.assertIsNone(session.submitted_number)


if __name__ == '__main__':
    unittest.main()
