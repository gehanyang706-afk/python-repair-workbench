"""MCP client evidence/lifecycle contracts without models or Docker execution."""
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from repair_workbench import diagnostic_mcp_client as client, probe_worker
from repair_workbench.diagnostics import _validated_report, get_workload
from repair_workbench.session import BudgetExceeded
from repair_workbench.snapshot import create_snapshot, snapshot_id, verify_snapshot
from test_diagnostics import correct_export


IMAGE = "sha256:" + "a" * 64
CONTAINER = "repair-probe-" + "a" * 16


@unittest.skipUnless(importlib.util.find_spec("mcp") is not None, "requires the optional pinned MCP SDK")
class MCPClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "map_exporter.py").write_text("raise RuntimeError('must not execute host source')\n")
        for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "MCP contract fixture")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "-C", str(self.source), *args], check=True, capture_output=True, timeout=30)
        self.snapshot = self.root / "baseline"
        create_snapshot(self.source, self.snapshot)
        with patch.object(probe_worker, "_load_export", return_value=correct_export):
            self.worker = probe_worker.execute("benchmark", Path("/workspace"), self.root)
        self.destination = self.root / "result"
        self.config = None

    def exchange(self, *, mutate_report=None, mutate_worker=None, archive_mismatch=False):
        async def operation(parameters, probe, report_id, discovery):
            self.config = json.loads(Path(parameters.args[-1]).read_text())
            directory = Path(self.config["destination"])
            directory.mkdir()
            worker = copy.deepcopy(self.worker)
            report = {**copy.deepcopy(worker), "status": "ok", "cleanup_ok": True,
                      "snapshot_id": self.config["snapshot_id"], "image_id": IMAGE, "returncode": 0,
                      "harness_sha256": hashlib.sha256(Path(probe_worker.__file__).read_bytes()).hexdigest()}
            if mutate_worker:
                mutate_worker(worker)
            if mutate_report:
                mutate_report(report)
            (directory / "probe.json").write_text(json.dumps(worker))
            archived = {**report, "returncode": 19} if archive_mismatch else report
            (directory / "result.json").write_text(json.dumps(archived))
            entries = [{"event": "probe_started", "report_id": report_id},
                       {"name": CONTAINER, "state": "reserved"}, {"name": CONTAINER, "state": "cleaned"},
                       {"event": "probe_finished", "report_id": report_id, "status": "ok"}]
            Path(self.config["events_file"]).write_text("".join(json.dumps(item) + "\n" for item in entries))
            discovery.extend({"name": name} for name in ("list_diagnostics", "run_probe", "read_report"))
            return {"report_id": report_id, "report": report}
        return operation

    def run_mock(self, exchange=None, **kwargs):
        with patch.object(client, "_exchange", side_effect=exchange or self.exchange()), \
                patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, IMAGE)) as process:
            result = client.run_mcp_probe(self.snapshot, self.destination, kwargs.pop("image", IMAGE),
                                          "benchmark", **kwargs)
        return result, process

    def transport(self):
        return json.loads((self.destination.with_name(self.destination.name + "-mcp") / "transport.json").read_text())

    def test_success_matches_archived_result_and_independent_host_validator(self):
        report, process = self.run_mock()
        self.assertEqual(report, json.loads((self.destination / "result.json").read_text()))
        verified = _validated_report(self.destination / "probe.json", "benchmark", get_workload("ugc-export-v1"))
        self.assertTrue(all(report[key] == value for key, value in verified.items()))
        self.assertEqual(self.transport()["status"], "completed")
        self.assertTrue(self.transport()["cleanup_ok"])
        self.assertEqual(len(self.transport()["tools"]), 3)
        self.assertFalse(any(call.args[0][1:3] == ["rm", "-f"] for call in process.call_args_list))

    def test_archived_response_cannot_disagree_with_protocol_result(self):
        with self.assertRaisesRegex(client.MCPProbeError, "mcp_archived_result_mismatch"):
            self.run_mock(self.exchange(archive_mismatch=True))
        self.assertEqual(self.transport()["status"], "stopped")

    def test_binding_tampering_is_rejected(self):
        for field, value in (("snapshot_id", "0" * 64), ("image_id", "sha256:" + "b" * 64),
                             ("workload_id", "other"), ("workload_fingerprint", "0" * 64),
                             ("harness_sha256", "0" * 64), ("probe", "memory")):
            with self.subTest(field=field):
                self.destination = self.root / ("bad-" + field)
                with self.assertRaises(client.MCPProbeError):
                    self.run_mock(self.exchange(mutate_report=lambda report: report.update({field: value})))

    def test_worker_measurement_tampering_is_rejected_even_if_outer_report_is_valid(self):
        with self.assertRaises(client.MCPProbeError):
            self.run_mock(self.exchange(mutate_worker=lambda worker: worker["summary"]["scales"]["large"].update(median_seconds=99)))

    def test_tag_input_cannot_accept_a_nonimmutable_returned_image(self):
        with self.assertRaises(client.MCPProbeError):
            self.run_mock(self.exchange(mutate_report=lambda report: report.update(image_id="not-an-image")),
                          image="local:test")

    def test_tag_is_pinned_with_local_inspect_without_pull_and_immutable_ids_are_strict(self):
        with patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, IMAGE + "\n")) as process:
            self.assertEqual(client._pin_image("local:test", 3), IMAGE)
        self.assertEqual(process.call_args.args[0], ["docker", "image", "inspect", "local:test", "--format", "{{.Id}}"])
        self.assertEqual(process.call_args.kwargs["timeout"], 3)
        with patch.object(client.subprocess, "run") as process:
            self.assertEqual(client._pin_image(IMAGE, 30), IMAGE)
            for invalid in ("sha256:short", "sha256:" + "A" * 64):
                with self.assertRaises(ValueError):
                    client._pin_image(invalid, 30)
            process.assert_not_called()

    def test_invalid_parameters_and_destinations_do_not_launch_transport(self):
        with patch.object(client, "_exchange") as exchange:
            for timeout in (True, 0, -1, 121, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    client.run_mcp_probe(self.snapshot, self.destination, IMAGE, "cpu", timeout_seconds=timeout)
            for image in ("", "-flag", "bad image", "$(command)"):
                with self.subTest(image=image), self.assertRaises(ValueError):
                    client.run_mcp_probe(self.snapshot, self.destination, image, "cpu")
            for directory in (self.source / "out", self.snapshot / "out", self.source):
                with self.subTest(directory=directory), self.assertRaises((ValueError, FileExistsError)):
                    client.run_mcp_probe(self.snapshot, directory, IMAGE, "cpu")
            for probe in ("shell", "os.system"):
                with self.assertRaises(ValueError):
                    client.run_mcp_probe(self.snapshot, self.destination, IMAGE, probe)
            exchange.assert_not_called()

    def test_existing_and_symlink_outputs_cannot_be_overwritten(self):
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            client.run_mcp_probe(self.snapshot, self.destination, IMAGE, "cpu")
        self.destination.rmdir()
        self.destination.symlink_to(self.root / "absent")
        with self.assertRaises(FileExistsError):
            client.run_mcp_probe(self.snapshot, self.destination, IMAGE, "cpu")

    def test_server_environment_excludes_credentials_and_trace_settings(self):
        secrets = {"DASHSCOPE_API_KEY": "test-only-secret", "OPENAI_API_KEY": "test-only-secret",
                   "LANGSMITH_API_KEY": "test-only-secret", "LANGCHAIN_TRACING_V2": "true",
                   "OTEL_EXPORTER_OTLP_ENDPOINT": "https://invalid.example", "PYTHONPATH": "/untrusted"}
        with patch.dict(os.environ, {**secrets, "PATH": "/bin", "HOME": str(self.root)}, clear=True):
            parameters = client._server_parameters(self.root / "config.json")
        self.assertEqual(parameters.command, os.sys.executable)
        self.assertEqual(parameters.args[:2], ["-m", "repair_workbench.diagnostic_mcp_server"])
        self.assertTrue(set(secrets).isdisjoint(parameters.env))
        self.assertNotIn("test-only-secret", str(parameters))

    def test_journal_partial_rows_wait_for_newline_and_do_not_duplicate_receipts(self):
        path = self.root / "journal"
        control = Mock()
        journal = client._Journal(path, control)
        payload = json.dumps({"name": CONTAINER, "state": "reserved"})
        path.write_text(payload[:20])
        journal.drain()
        self.assertEqual(journal.containers, {})
        with path.open("a") as stream:
            stream.write(payload[20:] + "\n")
        journal.drain()
        journal.drain()
        self.assertEqual(journal.containers, {CONTAINER: "reserved"})
        control.container.assert_called_once_with(CONTAINER, "reserved")

    def test_invalid_container_names_never_reach_emergency_rm(self):
        for number, name in enumerate(("postgres", "repair-probe-" + "A" * 16,
                                       "repair-probe-../../other", "--all", "repair-probe-" + "a" * 17)):
            path = self.root / f"journal-{number}"
            path.write_text(json.dumps({"name": name, "state": "reserved"}) + "\n")
            journal = client._Journal(path, None)
            with self.assertRaises(client.MCPProbeError):
                journal.drain()
            with patch.object(client.subprocess, "run") as process:
                self.assertTrue(journal.cleanup_outstanding())
            process.assert_not_called()

    def test_journal_unknown_cleanup_and_duplicate_reservation_are_rejected(self):
        path = self.root / "journal"
        path.write_text(json.dumps({"name": CONTAINER, "state": "cleaned"}) + "\n")
        with self.assertRaises(client.MCPProbeError):
            client._Journal(path, None).drain()
        path.write_text((json.dumps({"name": CONTAINER, "state": "reserved"}) + "\n") * 2)
        with self.assertRaises(client.MCPProbeError):
            client._Journal(path, None).drain()

    def test_failed_emergency_cleanup_does_not_mark_container_cleaned(self):
        path = self.root / "journal"
        path.write_text(json.dumps({"name": CONTAINER, "state": "reserved"}) + "\n")
        journal = client._Journal(path, None)
        journal.drain()
        with patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")) as process:
            self.assertFalse(journal.cleanup_outstanding())
        self.assertEqual(journal.containers, {CONTAINER: "reserved"})
        self.assertEqual(process.call_args.args[0], ["docker", "rm", "-f", CONTAINER])

    def test_disconnect_cleans_only_recorded_outstanding_container_and_saves_transport(self):
        async def disconnected(parameters, probe, report_id, discovery):
            config = json.loads(Path(parameters.args[-1]).read_text())
            Path(config["events_file"]).write_text(json.dumps({"name": CONTAINER, "state": "reserved"}) + "\n")
            raise OSError("synthetic disconnection")
        with patch.object(client, "_exchange", side_effect=disconnected), \
                patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")) as process, \
                self.assertRaises(client.MCPProbeError):
            client.run_mcp_probe(self.snapshot, self.destination, IMAGE, "benchmark")
        removals = [call for call in process.call_args_list if call.args[0][1:3] == ["rm", "-f"]]
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].args[0], ["docker", "rm", "-f", CONTAINER])
        self.assertTrue(self.transport()["cleanup_ok"])
        self.assertEqual(self.transport()["containers"], {CONTAINER: "cleaned"})

    def test_disconnect_with_incomplete_journal_tail_cannot_claim_confirmed_cleanup(self):
        async def disconnected(parameters, probe, report_id, discovery):
            config = json.loads(Path(parameters.args[-1]).read_text())
            Path(config["events_file"]).write_text('{"name":"repair-probe-')
            raise OSError("synthetic disconnection during journal write")
        with patch.object(client, "_exchange", side_effect=disconnected), \
                patch.object(client.subprocess, "run") as process, \
                self.assertRaises(client.MCPProbeError):
            client.run_mcp_probe(self.snapshot, self.destination, IMAGE, "benchmark")
        self.assertFalse(self.transport()["cleanup_ok"])
        self.assertFalse(self.transport()["journal_valid"])
        self.assertFalse(any(call.args[0][1:3] == ["rm", "-f"] for call in process.call_args_list))

    def test_parent_cancellation_and_deadline_keep_original_reason_after_server_cleanup(self):
        for reason in ("cancel_requested", "deadline_exceeded"):
            with self.subTest(reason=reason):
                self.destination = self.root / reason
                control = Mock()
                configs = []

                def check():
                    if configs:
                        raise BudgetExceeded(reason)
                control.check.side_effect = check

                async def until_cancel(parameters, probe, report_id, discovery):
                    config = json.loads(Path(parameters.args[-1]).read_text())
                    configs.append(config)
                    while not Path(config["cancel_file"]).exists():
                        await asyncio.sleep(0)
                    Path(config["events_file"]).write_text(
                        json.dumps({"name": CONTAINER, "state": "reserved"}) + "\n"
                        + json.dumps({"name": CONTAINER, "state": "cleaned"}) + "\n")
                    raise RuntimeError("server observed parent cancellation")

                with self.assertRaisesRegex(BudgetExceeded, reason):
                    self.run_mock(until_cancel, execution_control=control)
                self.assertTrue(Path(configs[0]["cancel_file"]).exists())
                self.assertTrue(self.transport()["cleanup_ok"])
                control.container.assert_any_call(CONTAINER, "cleaned")

    def test_cleanup_journal_error_cannot_replace_parent_budget_stop(self):
        journal = Mock()
        journal.drain.side_effect = [None, client.MCPProbeError("mcp_invalid_journal")]
        control = Mock()
        control.check.side_effect = BudgetExceeded("cancel_requested")
        config = {"timeout_seconds": 1, "report_id": "test", "cancel_file": str(self.root / "cancel")}

        async def exchange(*args):
            return {}
        with patch.object(client, "_exchange", side_effect=exchange), \
                self.assertRaisesRegex(BudgetExceeded, "cancel_requested"):
            asyncio.run(client._controlled_exchange(None, "cpu", config, journal, [], control))

    def test_local_timeout_writes_cancellation_and_saves_transport(self):
        async def until_cancel(parameters, probe, report_id, discovery):
            config = json.loads(Path(parameters.args[-1]).read_text())
            while not Path(config["cancel_file"]).exists():
                await asyncio.sleep(.001)
            return {}
        with self.assertRaisesRegex(client.MCPProbeError, "mcp_probe_timeout"):
            self.run_mock(until_cancel, timeout_seconds=.2)
        self.assertTrue((self.destination.with_name("result-mcp") / "cancel").exists())
        self.assertEqual(self.transport()["status"], "stopped")

    def test_protocol_payload_rejects_tool_errors_nonstructured_and_oversized_results(self):
        for result in (SimpleNamespace(is_error=True, structured_content={}),
                       SimpleNamespace(is_error=False, structured_content="not structured"),
                       SimpleNamespace(is_error=False, structured_content={"large": "x" * (256 * 1024)})):
            with self.assertRaises(client.MCPProbeError):
                client._payload(result)

    def test_real_stdio_discovery_exposes_fixed_schemas_without_executing_a_probe(self):
        from mcp import Client
        control_directory = self.root / "discovery-control"
        control_directory.mkdir()
        config = {"schema_version": 1, "snapshot": str(self.snapshot),
                  "snapshot_id": snapshot_id(verify_snapshot(self.snapshot)),
                  "destination": str(self.destination), "image": IMAGE,
                  "workload": "ugc-export-v1", "timeout_seconds": 30,
                  "cancel_file": str(control_directory / "cancel"),
                  "events_file": str(control_directory / "journal.jsonl"),
                  "report_id": "probe-" + "a" * 32}
        config_path = control_directory / "config.json"
        config_path.write_text(json.dumps(config))

        async def discover():
            async with Client(client._server_parameters(config_path), cache=None) as connection:
                catalog = await connection.list_tools()
                tools = {tool.name: tool.model_dump(mode="json", by_alias=True) for tool in catalog.tools}
                self.assertEqual(set(tools), {"list_diagnostics", "run_probe", "read_report"})
                self.assertEqual(set(tools["run_probe"]["inputSchema"]["properties"]), {"probe"})
                self.assertEqual(set(tools["read_report"]["inputSchema"]["properties"]), {"report_id"})
                description = client._payload(await connection.call_tool("list_diagnostics", {}))
                self.assertEqual(description["snapshot_id"], config["snapshot_id"])
                self.assertEqual(description["report_id"], config["report_id"])
                self.assertEqual(description["diagnostics"], ["cpu", "memory", "benchmark"])
                with self.assertRaises(client.MCPProbeError):
                    client._payload(await connection.call_tool("read_report", {"report_id": "foreign"}))

        asyncio.run(discover())
        self.assertFalse(self.destination.exists())
        self.assertFalse((control_directory / "journal.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
