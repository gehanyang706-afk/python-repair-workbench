"""Opt-in Skills and delegation through the actual product graph, without APIs."""
import json
from pathlib import Path
import tempfile
import unittest

try:
    from repair_workbench.agent import run_agent
    from repair_workbench.diagnosis import DiagnosticSession
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field
except ImportError:
    raise unittest.SkipTest('optional Agent dependencies are not installed')

from test_diagnosis import passing_baseline


class ExtensionPolicy(BaseChatModel):
    calls: int = 0
    current_tools: list = Field(default_factory=list)
    child_tool_sets: list = Field(default_factory=list)
    saw_skill_metadata: bool = False
    saw_skill_body: bool = False
    findings: dict = Field(default_factory=dict)

    @property
    def _llm_type(self):
        return 'offline-extension-policy'

    def bind_tools(self, tools, **kwargs):
        self.current_tools = [item.name for item in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        systems = '\n'.join(str(item.content) for item in messages if item.type == 'system')
        child = '你是只读代码调查子Agent' in systems
        previous = next((item for item in reversed(messages) if isinstance(item, ToolMessage)), None)
        value = json.loads(previous.content) if previous and previous.name != 'read_file' else None
        if child:
            self.child_tool_sets.append(list(self.current_tools))
            if previous is None:
                action, arguments = 'read_source', {'path': 'map_exporter.py'}
            else:
                self.findings = {'summary': 'The requested function is available; this is a source observation.',
                                 'evidence_refs': [value['evidence_ref']],
                                 'source_refs': [value['source_ref']],
                                 'limitations': 'No performance experiment has been run.'}
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content=json.dumps(self.findings)))])
        elif previous is None:
            self.saw_skill_metadata = 'cpu-hotspot' in systems
            action, arguments = 'read_file', {'file_path': '/skills/cpu-hotspot/SKILL.md'}
        elif previous.name == 'read_file':
            self.saw_skill_body = len(str(previous.content)) > 300
            action, arguments = 'task', {
                'subagent_type': 'investigator',
                'description': 'Read export_map and return actual evidence references and limitations.'}
        elif previous.name == 'task':
            action, arguments = 'conclude_diagnosis', {
                'decision': 'insufficient_evidence', 'explanation': value['limitations'],
                'evidence_refs': value['evidence_refs']}
        else:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content='Source investigation completed; performance remains unverified.'))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[{
            'name': action, 'args': arguments, 'id': f'extension-{self.calls}'}]))])


class DiagnosticExtensionIntegrationTests(unittest.TestCase):
    def test_skills_and_readonly_child_share_actual_session_and_keep_proposals_unaccepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = passing_baseline(root)
            session = DiagnosticSession(baseline, root / 'run', {'map_exporter.py'},
                                        use_skills=True, use_investigator=True, max_model_calls=12)
            model = ExtensionPolicy()
            result = run_agent(session, model, 'Investigate the exporter without changing it yet.')
            self.assertEqual(result['stop_reason'], 'agent_finished', result)
            self.assertTrue(model.saw_skill_metadata)
            self.assertTrue(model.saw_skill_body)
            self.assertEqual(result['model_calls'], model.calls)
            self.assertEqual(result['delegation_count'], 1)
            self.assertEqual(result['delegation_history'][0]['status'], 'investigator_proposal')
            expected = {'list_sources', 'read_source', 'source_outline', 'read_symbol',
                        'search_source', 'read_evidence'}
            self.assertTrue(model.child_tool_sets)
            self.assertTrue(all(set(tools) == expected for tools in model.child_tool_sets))
            self.assertEqual(result['status'], 'inconclusive')
            self.assertEqual(result['attempts'], [])
            self.assertEqual(result['diagnostic_probe_runs'], 0)
            self.assertEqual(result['final_decision']['decision'], 'insufficient_evidence')
            events = [json.loads(line) for line in (root / 'run/events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(item['event'] == 'diagnostic_file_read' for item in events), 1)
            child_calls = [item for item in events if item['event'] == 'model_started'
                           and item.get('agent_role') == 'investigator']
            self.assertEqual(len(child_calls), 2)
            self.assertFalse(result['skill_manifest']['generalization_verified'])


if __name__ == '__main__':
    unittest.main()
