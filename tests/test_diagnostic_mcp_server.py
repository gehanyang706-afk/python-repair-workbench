"""Official in-process MCP client contracts with a controlled probe executor."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from mcp import Client
    from repair_workbench import diagnostic_mcp_server as adapter
except ImportError:
    raise unittest.SkipTest("optional pinned MCP dependencies are required")

from repair_workbench.snapshot import snapshot_id


IMAGE = "sha256:" + "a" * 64


class DiagnosticMCPServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "snapshot"
        (self.snapshot / "workspace").mkdir(parents=True)
        content = b"raise RuntimeError('target source must never execute on host')\n"
        (self.snapshot / "workspace/map_exporter.py").write_bytes(content)
        self.source = self.root / "source"
        self.source.mkdir()
        manifest = {"source_repo": str(self.source), "commit": "b" * 40,
                    "files": [{"path": "map_exporter.py", "sha256": hashlib.sha256(content).hexdigest()}]}
        (self.snapshot / "manifest.json").write_text(json.dumps(manifest))
        self.output = self.root / "probe"
        self.control = self.root / "probe-mcp"
        self.control.mkdir()
        self.config_path = self.control / "config.json"
        self.config = {"schema_version": 1, "snapshot": str(self.snapshot),
                       "snapshot_id": snapshot_id(manifest), "destination": str(self.output),
                       "image": IMAGE, "workload": "ugc-export-v1", "timeout_seconds": 30,
                       "cancel_file": str(self.control / "cancel"),
                       "events_file": str(self.control / "containers.jsonl"),
                       "report_id": "probe-" + "c" * 32}
        self.config_path.write_text(json.dumps(self.config))
        self.started = threading.Event()
        self.cleaned = threading.Event()

    def executor(self, snapshot, destination, image, probe, workload, timeout, *, execution_control):
        self.assertEqual(snapshot, self.snapshot)
        self.assertEqual(destination, self.output)
        self.assertEqual(image, IMAGE)
        self.assertEqual(workload, "ugc-export-v1")
        self.assertEqual(timeout, 30)
        execution_control.check()
        destination.mkdir()
        name = "repair-probe-" + "d" * 16
        execution_control.container(name, "reserved")
        result = {"status": "ok", "snapshot_id": self.config["snapshot_id"], "image_id": image,
                  "workload_id": workload, "probe": probe, "cleanup_ok": True,
                  "summary": {"semantic_ok": True}, "samples": {"small": [.1]}}
        execution_control.container(name, "cleaned")
        (destination / "result.json").write_text(json.dumps(result))
        return result

    def blocking_executor(self, snapshot, destination, image, probe, workload, timeout, *, execution_control):
        destination.mkdir()
        result = {"status": "execution_error", "snapshot_id": self.config["snapshot_id"],
                  "image_id": image, "workload_id": workload, "probe": probe,
                  "cleanup_ok": False, "summary": {}, "samples": {}}
        name = "repair-probe-" + "e" * 16
        execution_control.container(name, "reserved")
        self.started.set()
        try:
            while True:
                execution_control.check()
                time.sleep(.005)
        finally:
            result["cleanup_ok"] = True
            (destination / "result.json").write_text(json.dumps(result))
            execution_control.container(name, "cleaned")
            self.cleaned.set()

    def journal(self):
        return [json.loads(line) for line in Path(self.config["events_file"]).read_text().splitlines()]

    async def test_official_client_discovers_exact_typed_tools_and_bound_job(self):
        server = adapter.build_server(adapter.load_config(self.config_path))
        async with Client(server) as client:
            listing = await client.list_tools()
            self.assertEqual({tool.name for tool in listing.tools}, {"list_diagnostics", "run_probe", "read_report"})
            schemas = {tool.name: tool.input_schema for tool in listing.tools}
            self.assertEqual(schemas["run_probe"]["required"], ["probe"])
            self.assertEqual(set(schemas["run_probe"]["properties"]), {"probe"})
            self.assertEqual(set(schemas["run_probe"]["properties"]["probe"]["enum"]), {"cpu", "memory", "benchmark"})
            self.assertEqual(set(schemas["read_report"]["properties"]), {"report_id"})
            description = await client.call_tool("list_diagnostics")
            self.assertFalse(description.is_error)
            self.assertEqual(description.structured_content["snapshot_id"], self.config["snapshot_id"])
            self.assertTrue(description.structured_content["one_shot"])
        self.assertFalse(self.output.exists())

    async def test_real_client_roundtrip_report_matches_disk_and_second_run_is_refused(self):
        server = adapter.build_server(self.config)
        with patch.object(adapter, "execute_probe", side_effect=self.executor) as execute:
            async with Client(server) as client:
                result = await client.call_tool("run_probe", {"probe": "benchmark"})
                self.assertFalse(result.is_error, result)
                data = result.structured_content
                stored = json.loads((self.output / "result.json").read_text())
                self.assertEqual(data["report"], stored)
                self.assertEqual(data["status"], stored["status"])
                again = await client.call_tool("read_report", {"report_id": self.config["report_id"]})
                self.assertEqual(again.structured_content, data)
                repeated = await client.call_tool("run_probe", {"probe": "memory"})
                self.assertTrue(repeated.is_error)
                self.assertIn("already_used", str(repeated.content))
        execute.assert_called_once()
        containers = [row for row in self.journal() if "name" in row]
        self.assertEqual([row["state"] for row in containers], ["reserved", "cleaned"])
        self.assertEqual({row["name"] for row in containers}, {"repair-probe-" + "d" * 16})

    async def test_unknown_report_and_unready_report_never_resolve_arbitrary_paths(self):
        async with Client(adapter.build_server(self.config)) as client:
            for report_id in ("../../secret", "/etc/passwd", self.config["report_id"]):
                with self.subTest(report_id=report_id):
                    result = await client.call_tool("read_report", {"report_id": report_id})
                    self.assertTrue(result.is_error)
        self.assertFalse(self.output.exists())

    async def test_invalid_probe_schema_cannot_start_a_job(self):
        with patch.object(adapter, "execute_probe", side_effect=self.executor) as execute:
            async with Client(adapter.build_server(self.config)) as client:
                for arguments in ({"probe": "shell"}, {"probe": "cpu;whoami"}, {}, {"probe": 1}):
                    result = await client.call_tool("run_probe", arguments)
                    self.assertTrue(result.is_error)
                result = await client.call_tool("run_probe", {"probe": "cpu"})
                self.assertFalse(result.is_error)
        execute.assert_called_once()

    async def test_tool_requests_cannot_override_bound_paths_or_add_commands(self):
        with patch.object(adapter, "execute_probe", side_effect=self.executor) as execute:
            async with Client(adapter.build_server(self.config)) as client:
                for tool, arguments in (
                    ("run_probe", {"probe": "cpu", "snapshot": "/etc"}),
                    ("run_probe", {"probe": "cpu", "command": "whoami"}),
                    ("list_diagnostics", {"path": "/etc/passwd"}),
                    ("read_report", {"report_id": self.config["report_id"], "path": "/etc/passwd"}),
                ):
                    with self.subTest(tool=tool, arguments=arguments):
                        result = await client.call_tool(tool, arguments)
                        self.assertTrue(result.is_error)
                valid = await client.call_tool("run_probe", {"probe": "cpu"})
                self.assertFalse(valid.is_error)
        execute.assert_called_once()

    async def test_config_strict_fields_and_control_binding_reject_before_execution(self):
        invalid = (
            {"unexpected": "field"}, {"schema_version": True}, {"schema_version": 2},
            {"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": 121},
            {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
            {"report_id": "../../report"}, {"snapshot_id": "0" * 64},
            {"image": "$(touch bad)"}, {"image": "sha256:short"}, {"workload": "arbitrary:main"},
            {"cancel_file": str(self.root / "outside")},
            {"events_file": self.config["cancel_file"]},
            {"snapshot": str(self.snapshot / ".." / "snapshot")},
        )
        with patch.object(adapter, "execute_probe") as execute:
            for changes in invalid:
                with self.subTest(changes=changes):
                    self.config_path.write_text(json.dumps({**self.config, **changes}))
                    with self.assertRaises(ValueError):
                        adapter.load_config(self.config_path)
        execute.assert_not_called()

    async def test_config_and_control_file_symlinks_are_rejected(self):
        link = self.control / "linked.json"
        link.symlink_to(self.config_path)
        with self.assertRaises(ValueError):
            adapter.load_config(link)
        events = Path(self.config["events_file"])
        outside = self.root / "outside"
        outside.write_text("")
        events.symlink_to(outside)
        with self.assertRaises(ValueError):
            adapter.build_server(self.config)
        events.unlink()
        workspace = self.snapshot / "workspace"
        moved = self.root / "moved-workspace"
        workspace.rename(moved)
        workspace.symlink_to(moved)
        with self.assertRaises(ValueError):
            adapter.build_server(self.config)

    async def test_destination_inside_snapshot_repo_or_existing_directory_is_refused(self):
        for path in (self.snapshot / "out", self.source / "out", self.control):
            with self.subTest(path=path), self.assertRaises(ValueError):
                adapter.build_server({**self.config, "destination": str(path)})

    async def test_changed_snapshot_after_discovery_does_not_reach_executor(self):
        server = adapter.build_server(self.config)
        (self.snapshot / "workspace/map_exporter.py").write_text("changed\n")
        with patch.object(adapter, "execute_probe") as execute:
            async with Client(server) as client:
                result = await client.call_tool("run_probe", {"probe": "cpu"})
                self.assertTrue(result.is_error)
        execute.assert_not_called()
        self.assertEqual(self.journal()[-1]["event"], "probe_error")

    async def test_executor_exception_is_recorded_without_leaking_exception_text(self):
        with patch.object(adapter, "execute_probe", side_effect=RuntimeError("sensitive-value-must-not-leak")):
            async with Client(adapter.build_server(self.config)) as client:
                result = await client.call_tool("run_probe", {"probe": "cpu"})
                self.assertTrue(result.is_error)
                self.assertNotIn("sensitive-value", str(result))
        self.assertNotIn("sensitive-value", Path(self.config["events_file"]).read_text())
        self.assertEqual(self.journal()[-1]["error_type"], "RuntimeError")

    async def test_cancel_file_waits_for_cleanup_and_preserves_readable_record(self):
        with patch.object(adapter, "execute_probe", side_effect=self.blocking_executor):
            async with Client(adapter.build_server(self.config)) as client:
                pending = asyncio.create_task(client.call_tool("run_probe", {"probe": "cpu"}))
                self.assertTrue(await asyncio.to_thread(self.started.wait, 2))
                Path(self.config["cancel_file"]).touch()
                result = await asyncio.wait_for(pending, 3)
                self.assertTrue(result.is_error)
                self.assertTrue(self.cleaned.is_set())
                stored = await client.call_tool("read_report", {"report_id": self.config["report_id"]})
                self.assertFalse(stored.is_error)
                self.assertTrue(stored.structured_content["report"]["cleanup_ok"])
        self.assertEqual(self.journal()[-1]["event"], "probe_cancelled")

    async def test_sdk_cancellation_propagates_after_worker_cleanup(self):
        with patch.object(adapter, "execute_probe", side_effect=self.blocking_executor):
            async with Client(adapter.build_server(self.config)) as client:
                pending = asyncio.create_task(client.call_tool("run_probe", {"probe": "cpu"}))
                self.assertTrue(await asyncio.to_thread(self.started.wait, 2))
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                self.assertTrue(self.cleaned.is_set(), "SDK cancellation abandoned Docker-owning thread")
        self.assertEqual(self.journal()[-1]["event"], "probe_cancelled")

    async def test_concurrent_second_probe_is_rejected_before_first_finishes(self):
        with patch.object(adapter, "execute_probe", side_effect=self.blocking_executor) as execute:
            async with Client(adapter.build_server(self.config)) as client:
                first = asyncio.create_task(client.call_tool("run_probe", {"probe": "cpu"}))
                self.assertTrue(await asyncio.to_thread(self.started.wait, 2))
                second = await client.call_tool("run_probe", {"probe": "memory"})
                self.assertTrue(second.is_error)
                self.assertIn("already_used", str(second.content))
                Path(self.config["cancel_file"]).touch()
                await asyncio.wait_for(first, 3)
        execute.assert_called_once()
        self.assertTrue(self.cleaned.is_set())

    async def test_control_deadline_still_waits_for_executor_finally(self):
        self.config["timeout_seconds"] = .03
        with patch.object(adapter, "execute_probe", side_effect=self.blocking_executor):
            async with Client(adapter.build_server(self.config)) as client:
                result = await asyncio.wait_for(client.call_tool("run_probe", {"probe": "cpu"}), 3)
                self.assertTrue(result.is_error)
                self.assertTrue(self.cleaned.is_set())
        self.assertEqual(self.journal()[-1]["error_type"], "TimeoutExpired")

    async def test_cancel_marker_before_execution_never_reaches_runner(self):
        Path(self.config["cancel_file"]).touch()
        with patch.object(adapter, "execute_probe") as execute:
            async with Client(adapter.build_server(self.config)) as client:
                result = await client.call_tool("run_probe", {"probe": "cpu"})
                self.assertTrue(result.is_error)
        execute.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.journal()[-1]["event"], "probe_cancelled")

    async def test_large_report_is_not_returned_or_silently_truncated(self):
        def huge(*args, **kwargs):
            result = self.executor(*args, **kwargs)
            result["summary"]["oversized"] = "x" * adapter.MAX_RESPONSE_BYTES
            (self.output / "result.json").write_text(json.dumps(result))
            return result
        with patch.object(adapter, "execute_probe", side_effect=huge):
            async with Client(adapter.build_server(self.config)) as client:
                result = await client.call_tool("run_probe", {"probe": "cpu"})
                self.assertTrue(result.is_error)
                self.assertLess(len(str(result)), 2000)
                read = await client.call_tool("read_report", {"report_id": self.config["report_id"]})
                self.assertTrue(read.is_error)
        self.assertGreater((self.output / "result.json").stat().st_size, adapter.MAX_RESPONSE_BYTES)


if __name__ == "__main__":
    unittest.main()
