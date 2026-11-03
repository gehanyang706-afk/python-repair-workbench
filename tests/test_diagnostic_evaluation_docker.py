"""Actual serial development batches; scripted control flow is not LLM quality."""
import json
import os
from pathlib import Path
import tempfile
import unittest

try:
    import repair_workbench.diagnostic_eval_policy
except ImportError:
    raise unittest.SkipTest('optional Agent dependencies required')

from repair_workbench.diagnostic_evaluation import create_plan, run_plan

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'benchmarks/ugc-diagnostics-dev-v1'


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'explicit actual diagnostic batch acceptance')
class DiagnosticEvaluationDockerTests(unittest.TestCase):
    def run_batch(self, transport):
        with tempfile.TemporaryDirectory(dir=ROOT / 'runtime') as temporary:
            root = Path(temporary).resolve()
            plan = create_plan(DATASET, os.environ.get('REPAIR_TEST_IMAGE', 'study-agent-deepagents:0.1'),
                               tasks=['case-01'], transport=transport, seed=27)
            record = run_plan(plan, DATASET, root / 'batch')
            self.assertEqual(record['batch_state'], 'completed', record)
            self.assertTrue(record['containers_confirmed_cleaned'])
            self.assertEqual(record['summary']['planned_runs'], 2)
            self.assertEqual(record['summary']['audited_runs'], 2)
            self.assertEqual(record['summary']['comparable_pairs'], 1)
            self.assertEqual(record['summary']['real_model_api_calls'], 0)
            self.assertIsNone(record['summary']['all_calls_total_tokens'])
            self.assertIsNone(record['summary']['optimization_success_rate'])
            self.assertIsNone(record['summary']['health_decision_accuracy'])
            rows = {row['condition']: row for row in record['rows']}
            self.assertEqual(rows['adaptive']['metrics']['exploratory_probe_runs'], 2)
            self.assertEqual(rows['fixed']['metrics']['exploratory_probe_runs'], 3)
            self.assertEqual(rows['fixed']['metrics']['reused_observations'], 2)
            self.assertEqual(rows['adaptive']['baseline_id'], rows['fixed']['baseline_id'])
            for row in record['rows']:
                self.assertEqual(row['candidate_status'], 'inconclusive')
                self.assertEqual(row['metrics']['decision_proposal'], 'insufficient_evidence')
                self.assertEqual(row['metrics']['missing_usage_calls'], row['metrics']['model_wrapper_calls'])
                directory = root / 'batch/runs' / row['run_id']
                saved = json.loads((directory / 'audit.json').read_text())
                self.assertEqual(saved['audit_status'], 'passed')
                self.assertEqual(saved['counts']['mcp_jobs'], row['metrics']['exploratory_probe_runs'] if transport == 'mcp' else 0)
                self.assertIn('progress/main.json', row['artifact_sha256'])
            self.assertTrue((root / 'batch/runs.csv').is_file())

    def test_local_pair_is_audited_and_never_promotes_abstention_to_pass(self):
        self.run_batch('local')

    def test_mcp_pair_has_bound_stdio_archives_for_each_physical_probe(self):
        try:
            import mcp
        except ImportError:
            self.skipTest('optional MCP SDK required')
        self.run_batch('mcp')


if __name__ == '__main__':
    unittest.main()
