"""Real container acceptance of the diagnostic loop with a known scripted patch.

This verifies execution and evidence, never LLM repair ability or unseen success.
The first measured verdict is preserved even when noise makes it inconclusive.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    from repair_workbench.agent import run_agent
except ImportError:
    raise unittest.SkipTest('optional Agent dependencies are not installed')
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.runner import run_tests
from repair_workbench.snapshot import create_snapshot
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


ROOT = Path(__file__).resolve().parents[1]
KNOWN_CANDIDATE = '''def export_map(objects, resources):
    first = {}
    for resource in resources:
        first.setdefault(resource["id"], resource["path"])
    result = []
    for item in objects:
        if item["resource_id"] not in first:
            raise ValueError("unknown resource")
        result.append({"object_id": item["id"], "resource_path": first[item["resource_id"]]})
    return result
'''


class KnownPatchModel(BaseChatModel):
    """Test-only policy: issued references and real evidence, a known fixture fix."""
    calls: int = 0
    source: dict = Field(default_factory=dict)
    hypothesis: str = ''

    @property
    def _llm_type(self):
        return 'scripted-diagnostic-docker-acceptance'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        previous = next((message for message in reversed(messages) if isinstance(message, ToolMessage)), None)
        value = json.loads(previous.content) if previous else {}
        name = previous.name if previous else None
        if name is None:
            action, arguments = 'read_source', {'path': 'map_exporter.py'}
        elif name == 'read_source':
            self.source = value
            action, arguments = 'record_hypothesis', {
                'claim': 'Repeated resource lookup may dominate export work.',
                'evidence_refs': [value['evidence_ref']], 'next_action': 'Inspect CPU calls.'}
        elif name == 'record_hypothesis' and value['status'] == 'open':
            self.hypothesis = value['hypothesis_id']
            action, arguments = 'run_probe', {'probe': 'cpu', 'hypothesis_id': self.hypothesis}
        elif name == 'run_probe':
            if value['observation']['status'] != 'ok':
                action, arguments = 'conclude_diagnosis', {
                    'decision': 'insufficient_evidence', 'explanation': 'Diagnostic execution failed.',
                    'evidence_refs': [value['evidence_ref']]}
            else:
                action, arguments = 'record_hypothesis', {
                    'hypothesis_id': self.hypothesis, 'status': 'supported',
                    'claim': 'The real CPU report supports examining export_map.',
                    'evidence_refs': [value['evidence_ref']], 'next_action': 'Verify the known test candidate.'}
        elif name == 'record_hypothesis':
            action, arguments = 'submit_ref_edits', {
                'edits': [{'source_ref': self.source['source_ref'],
                           'old_text': self.source['content'], 'new_text': KNOWN_CANDIDATE}],
                'diagnosis': 'Test-only known candidate; preserve first-match and ordered semantics.'}
        else:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content='The independent tool verdict is preserved. This was a scripted acceptance run.'))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[{
            'name': action, 'args': arguments, 'id': f'acceptance-{self.calls}'}]))])


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'explicit local Docker acceptance only')
class DiagnosticDockerAcceptanceTests(unittest.TestCase):
    def test_scripted_agent_real_cpu_patch_semantics_and_performance_receipts(self):
        configured = os.environ.get('DIAGNOSTIC_ACCEPTANCE_DIR')
        if configured:
            root = Path(configured).resolve()
            root.mkdir(parents=True, exist_ok=False)
        else:
            (ROOT / 'runtime').mkdir(exist_ok=True)
            temporary = tempfile.TemporaryDirectory(dir=ROOT / 'runtime')
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        repo = root / 'source'
        shutil.copytree(ROOT / 'benchmarks/ugc-diagnostics-dev-v1/case-01/source', repo,
                        ignore=shutil.ignore_patterns('__pycache__'))
        for args in (('init', '-q'), ('add', '--all'), ('commit', '-qm', 'Public synthetic acceptance fixture')):
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                            '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            '-C', str(repo), *args], check=True, capture_output=True, timeout=30)
        original = (repo / 'map_exporter.py').read_bytes()
        baseline = root / 'baseline'
        create_snapshot(repo, baseline)
        before = run_tests(baseline, os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'))
        self.assertEqual(before['status'], 'tests_passed', before)
        self.assertTrue(before['cleanup_ok'])
        session = DiagnosticSession(baseline, root / 'agent', {'map_exporter.py'},
                                    max_model_calls=12, deadline_seconds=180, test_timeout=30)
        result = run_agent(session, KnownPatchModel(), 'Diagnose slow map export while preserving behavior.')
        audit = {'mode': 'scripted_known_candidate_real_container', 'real_model_calls': 0,
                 'unseen_evaluation': False, 'result': result}
        (root / 'acceptance.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual((repo / 'map_exporter.py').read_bytes(), original)
        self.assertEqual(len(result['attempts']), 1, result)
        verdict = result['attempts'][0]
        self.assertIn(verdict['reason'], {
            'passed_registered_performance_scope', 'measurement_noise_exceeded',
            'memory_regression', 'small_workload_regression', 'minimum_speedup_not_met'}, verdict)
        self.assertEqual(len(verdict['measurements']), 8)
        self.assertTrue(all(row['report']['cleanup_ok'] for row in verdict['measurements']))
        self.assertEqual(verdict['candidate_test_result']['counts']['failed'], 0)
        self.assertEqual(result['hypotheses']['H001']['revision'], 2)
        self.assertEqual(result['diagnostic_probe_runs'], 1)
        self.assertTrue((root / 'agent/attempt-01/verification/patch.diff').is_file())
        print(json.dumps({'acceptance_directory': str(root), 'measured_status': result['status'],
                          'measured_reason': result['reason'], 'real_model_calls': 0}))


if __name__ == '__main__':
    unittest.main()
