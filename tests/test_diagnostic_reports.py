"""Portable evidence and real MCP transport contracts; trusted fixture observations only."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repair_workbench import probe_worker
from repair_workbench.diagnosis import DiagnosticSession
from repair_workbench.diagnostic_reports import ReportStore, freeze_probe, write_bundle, read_json
from repair_workbench.snapshot import snapshot_id, verify_snapshot
from repair_workbench.tool_runtime import digest
from test_diagnosis import passing_baseline, AGENT_AVAILABLE
from test_diagnostics import correct_export

try:
    from mcp import Client
    from repair_workbench.diagnostic_report_mcp import build_server, query_mcp_report
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


def fixture_probe(snapshot, destination, image, probe, workload, timeout, *, execution_control):
    """Generate trusted test observations; never import source from the snapshot."""
    execution_control.check()
    destination.mkdir(parents=True)
    with patch.object(probe_worker, '_load_export', return_value=correct_export):
        measured = probe_worker.execute(probe, Path('/workspace'), destination)
    result = {**measured, 'status': 'ok', 'cleanup_ok': True, 'returncode': 0,
              'snapshot_id': snapshot_id(verify_snapshot(snapshot)), 'image_id': 'sha256:' + 'a' * 64,
              'harness_sha256': hashlib.sha256(Path(probe_worker.__file__).read_bytes()).hexdigest()}
    (destination / 'probe.json').write_text(json.dumps(measured))
    (destination / 'result.json').write_text(json.dumps(result))
    return result


class DiagnosticReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.baseline = passing_baseline(self.root)
        self.session = DiagnosticSession(self.baseline, self.root / 'agent', {'map_exporter.py'})
        with patch('repair_workbench.diagnostics.run_probe', side_effect=fixture_probe):
            observed = self.session.run_diagnostic('cpu')
        self.evidence = self.session.evidence[observed['evidence_ref']]
        self.record = freeze_probe(self.session.output, self.evidence)
        self.bundle = self.root / 'bundle.json'
        self.sha = write_bundle([self.record], self.bundle)
        self.store = ReportStore(self.bundle, self.sha)

    def test_export_is_portable_bounded_and_contains_no_execution_or_host_paths(self):
        raw = self.bundle.read_text()
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn('command', raw)
        page = self.store.query(self.record['report_id'], 'hotspots', limit=1)
        self.assertEqual(len(page['data']), 1)
        self.assertEqual(page['snapshot_id'], self.evidence['snapshot_id'])
        self.assertNotIn('top_functions', self.store.query(self.record['report_id'])['data'][0])
        # Source workspace can disappear; clients use only the frozen evidence bundle.
        self.assertEqual(ReportStore(self.bundle, self.sha).catalog(), self.store.catalog())

    def test_paths_forged_refs_boolean_offsets_and_cross_probe_sections_are_rejected(self):
        for identity in ('../../secret', '/etc/passwd', 'r-' + '0' * 32):
            with self.assertRaises(ValueError):
                self.store.query(identity)
        for arguments in ({'offset': True}, {'limit': 11}, {'scale': 'all'}, {'section': 'shell'}, {'section': 'samples'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.query(self.record['report_id'], **arguments)
        with self.assertRaises(ValueError):
            self.session.read_probe_report('E9999')

    def test_report_and_bundle_tampering_are_detected(self):
        with self.assertRaises(ValueError):
            ReportStore(self.bundle, '0' * 64)
        result = self.session.output / 'diagnostics/probe-01/result.json'
        result.write_text(result.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'changed_probe_report'):
            freeze_probe(self.session.output, self.evidence)

    def test_symlink_cannot_read_outside_the_frozen_bundle(self):
        link = self.root / 'linked.json'
        link.symlink_to(self.bundle)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            ReportStore(link, self.sha)

    def test_local_session_reads_charge_every_cache_hit_and_record_query_provenance(self):
        before = self.session.tool_calls
        first = self.session.read_probe_report(self.evidence['evidence_ref'], 'hotspots', limit=1)
        second = self.session.read_probe_report(self.evidence['evidence_ref'], 'hotspots', limit=1)
        self.assertEqual(first, second)
        self.assertEqual(self.session.tool_calls - before, 2)
        self.assertEqual(self.session.tool_runtime.summary()['cache_hits'], 1)
        events = [json.loads(line) for line in (self.session.output / 'events.jsonl').read_text().splitlines()]
        queries = [event for event in events if event['event'] == 'diagnostic_report_queried']
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]['result_sha256'], digest(first))

    @unittest.skipUnless(AGENT_AVAILABLE, 'optional Agent dependencies required')
    def test_product_agent_queries_pages_through_the_shared_runtime(self):
        from repair_workbench.agent import run_agent
        from langchain_core.messages import AIMessage
        from test_agent import ScriptedModel, call
        ref = self.evidence['evidence_ref']
        model = ScriptedModel(responses=[
            call('read_probe_report', {'evidence_ref': ref, 'section': 'hotspots', 'limit': 1}, 'page-one'),
            call('read_probe_report', {'evidence_ref': ref, 'section': 'hotspots', 'limit': 1}, 'same-page'),
            call('conclude_diagnosis', {'decision': 'insufficient_evidence', 'explanation': 'Read-only review.',
                                       'evidence_refs': [ref]}, 'conclude'),
            AIMessage(content='Measured evidence remains subject to independent verification.')])
        result = run_agent(self.session, model, 'Review the measured hotspot without modifying code.')
        self.assertEqual(result['stop_reason'], 'agent_finished', result)
        self.assertEqual(result['tool_runtime']['cache_hits'], 1)
        self.assertEqual(result['tool_statistics']['by_tool']['read_probe_report']['calls'], 2)
        self.assertEqual(result['attempts'], [])

    def test_visible_cpu_view_omits_details_but_keeps_the_complete_evidence_artifact(self):
        visible = self.session.read_evidence(self.evidence['evidence_ref'])
        self.assertLessEqual(len(visible['observation']['summary']['scales']['large']['top_functions']), 3)
        self.assertGreater(len(self.evidence['observation']['summary']['scales']['large']['top_functions']), 3)
        self.assertIn('detail_navigation', visible)

    @unittest.skipUnless(MCP_AVAILABLE, 'optional MCP SDK required')
    def test_independent_mcp_client_discovers_resources_and_cannot_execute_or_expand_parameters(self):
        async def run():
            async with Client(build_server(self.store), cache=None) as client:
                tools = await client.list_tools()
                self.assertEqual({tool.name for tool in tools.tools}, {'list_reports', 'query_report'})
                resources = await client.list_resources()
                uris = {str(resource.uri) for resource in resources.resources}
                self.assertEqual(uris, {'diagnostic://catalog', f'diagnostic://reports/{self.record["report_id"]}'})
                resource = await client.read_resource('diagnostic://catalog')
                self.assertEqual(json.loads(resource.contents[0].text), self.store.catalog())
                args = {'report_id': self.record['report_id'], 'section': 'hotspots', 'limit': 1}
                result = await client.call_tool('query_report', args)
                self.assertFalse(result.is_error, result)
                self.assertEqual(result.structured_content, self.store.query(**args))
                for extra in ({'path': '/etc/passwd'}, {'offset': True}, {'limit': 11}):
                    rejected = await client.call_tool('query_report', {**args, **extra})
                    self.assertTrue(rejected.is_error)
        asyncio.run(run())

    @unittest.skipUnless(MCP_AVAILABLE, 'optional MCP SDK required')
    def test_real_stdio_service_works_without_the_original_agent_and_filters_credentials(self):
        from repair_workbench.diagnostic_report_mcp import server_parameters
        args = {'report_id': self.record['report_id'], 'section': 'hotspots', 'limit': 1}
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'test-secret-never-forward', 'DOCKER_HOST': 'test-only'}):
            params = server_parameters(self.bundle, self.sha)
        self.assertNotIn('DASHSCOPE_API_KEY', params.env)
        self.assertNotIn('DOCKER_HOST', params.env)
        result = query_mcp_report(self.bundle, self.sha, args)
        self.assertEqual(result, self.store.query(**args))
        # A session can opt into MCP after receiving the same registered evidence.
        self.session.diagnostic_transport = 'mcp'
        page = self.session.read_probe_report(self.evidence['evidence_ref'], 'hotspots', limit=1)
        self.assertEqual(page['data'], result['data'])


if __name__ == '__main__':
    unittest.main()
