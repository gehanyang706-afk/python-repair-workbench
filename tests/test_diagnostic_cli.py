"""Diagnostic CLI boundaries: offline tools and a closed-by-default model entry."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from repair_workbench.__main__ import main


class DiagnosticCLITests(unittest.TestCase):
    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_diagnose_refuses_before_loading_credentials_or_model(self):
        with patch('repair_workbench.settings.ModelSettings.load') as settings:
            code, stdout, stderr = self.invoke([
                'diagnose', '--baseline', '/nonexistent/baseline', '--output', '/nonexistent/run',
                '--allow', 'map_exporter.py', '--task', 'Investigate slow export'])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, '')
        self.assertEqual(json.loads(stderr)['error_type'], 'ValueError')
        settings.assert_not_called()

    def test_probe_cleanup_failure_cannot_exit_successfully_or_construct_model(self):
        with patch('repair_workbench.diagnostics.run_probe', return_value={
                'status': 'ok', 'cleanup_ok': False}) as probe, \
                patch('repair_workbench.settings.ModelSettings.load') as settings:
            code, stdout, _ = self.invoke([
                'probe', '--snapshot', '/baseline', '--output', '/probe',
                '--image', 'local:test', '--kind', 'cpu'])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stdout)['cleanup_ok'])
        self.assertEqual(probe.call_args.args[3:5], ('cpu', 'ugc-export-v1'))
        settings.assert_not_called()

    def test_performance_result_controls_exit_without_loading_model(self):
        with patch('repair_workbench.performance.verify_performance_candidate', return_value={
                'status': 'inconclusive', 'reason': 'measurement_noise_exceeded'}) as verifier, \
                patch('repair_workbench.settings.ModelSettings.load') as settings:
            code, stdout, _ = self.invoke([
                'verify-performance', '--baseline', '/baseline', '--candidate', '/candidate',
                '--output', '/verified', '--allow', 'map_exporter.py'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)['reason'], 'measurement_noise_exceeded')
        self.assertEqual(verifier.call_args.args[3], {'map_exporter.py'})
        settings.assert_not_called()

    def test_mcp_probe_uses_protocol_adapter_without_constructing_a_model(self):
        with patch('repair_workbench.diagnostic_mcp_client.run_mcp_probe', return_value={
                'status': 'ok', 'cleanup_ok': True}) as probe, \
                patch('repair_workbench.settings.ModelSettings.load') as settings:
            code, stdout, _ = self.invoke([
                'probe', '--snapshot', '/baseline', '--output', '/probe',
                '--image', 'local:test', '--kind', 'cpu', '--transport', 'mcp'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)['cleanup_ok'])
        self.assertEqual(probe.call_args.args[3], 'cpu')
        settings.assert_not_called()

    def test_diagnostic_transport_and_subtask_limits_reach_trusted_session(self):
        with patch('repair_workbench.settings.ModelSettings.load') as settings, \
                patch('repair_workbench.diagnosis.DiagnosticSession') as session, \
                patch('repair_workbench.agent.run_agent', return_value={
                    'status': 'inconclusive', 'reason': 'offline_cli_contract'}) as run:
            code, _, _ = self.invoke([
                'diagnose', '--baseline', '/baseline', '--output', '/run',
                '--allow', 'map_exporter.py', '--task', 'Investigate', '--allow-real-model',
                '--diagnostic-transport', 'mcp', '--investigator',
                '--investigator-max-model-calls', '2', '--investigator-max-tools', '7',
                '--investigator-timeout', '5', '--main-model-reserve', '3',
                '--main-tool-reserve', '6', '--main-time-reserve', '8'])
        self.assertEqual(code, 2)
        arguments = session.call_args.kwargs
        self.assertEqual(arguments['diagnostic_transport'], 'mcp')
        self.assertEqual(arguments['investigator_max_model_calls'], 2)
        self.assertEqual(arguments['investigator_max_tools'], 7)
        self.assertEqual(arguments['investigator_timeout_seconds'], 5)
        self.assertEqual(arguments['main_model_reserve'], 3)
        self.assertEqual(arguments['main_tool_reserve'], 6)
        self.assertEqual(arguments['main_time_reserve_seconds'], 8)
        run.assert_called_once()
        settings.assert_called_once()


if __name__ == '__main__':
    unittest.main()
