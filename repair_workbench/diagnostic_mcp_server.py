"""One-job stdio MCP adapter for application-bound, registered diagnostic probes."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from .diagnostics import get_workload, run_probe as execute_probe
from .snapshot import snapshot_id, verify_snapshot


CONFIG_FIELDS = frozenset({"schema_version", "snapshot", "snapshot_id", "destination", "image",
                           "workload", "timeout_seconds", "cancel_file", "events_file", "report_id"})
MAX_RESPONSE_BYTES = 256 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_REPORT_ID = re.compile(r"(?:probe-)?[a-f0-9]{32}\Z|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")


def _canonical(value, *, exists=False, directory=False):
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError("invalid_job_path")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("job_path_must_be_canonical_absolute")
    if any(part.is_symlink() for part in (path, *path.parents)) or path.resolve() != path:
        raise ValueError("job_path_symlinks_forbidden")
    if path.name.startswith(".env"):
        raise ValueError("environment_files_are_not_job_paths")
    if exists and not (path.is_dir() if directory else path.is_file()):
        raise ValueError("job_path_not_found_or_wrong_type")
    if path.exists() and not directory and not path.is_file():
        raise ValueError("job_control_path_must_be_regular")
    return path


def _validate_config(config, *, control_directory=None, config_path=None):
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ValueError("unexpected_job_config_fields")
    config = dict(config)
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported_job_config_version")
    if not isinstance(config["snapshot_id"], str) or not _DIGEST.fullmatch(config["snapshot_id"]):
        raise ValueError("invalid_snapshot_identity")
    if not isinstance(config["report_id"], str) or not _REPORT_ID.fullmatch(config["report_id"]):
        raise ValueError("invalid_opaque_report_id")
    if (type(config["timeout_seconds"]) not in {int, float}
            or not math.isfinite(config["timeout_seconds"]) or not 0 < config["timeout_seconds"] <= 120):
        raise ValueError("job_timeout_must_be_in_0_120")
    image = config["image"]
    if (not isinstance(image, str) or len(image) > 256
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image)
            or image.startswith("sha256:") and not re.fullmatch(r"sha256:[a-f0-9]{64}", image)):
        raise ValueError("invalid_local_image")
    get_workload(config["workload"])
    snapshot = _canonical(config["snapshot"], exists=True, directory=True)
    _canonical(str(snapshot / "manifest.json"), exists=True)
    _canonical(str(snapshot / "workspace"), exists=True, directory=True)
    manifest = verify_snapshot(snapshot)
    if snapshot_id(manifest) != config["snapshot_id"]:
        raise ValueError("snapshot_identity_mismatch")
    destination = _canonical(config["destination"], directory=True)
    if destination.exists():
        raise ValueError("diagnostic_destination_already_exists")
    source_repo = _canonical(manifest["source_repo"], directory=True)
    if any(destination == root or root in destination.parents for root in (snapshot, source_repo)):
        raise ValueError("diagnostic_destination_inside_source")
    cancel = _canonical(config["cancel_file"])
    events = _canonical(config["events_file"])
    control = cancel.parent if control_directory is None else control_directory
    _canonical(str(control), exists=True, directory=True)
    if cancel.parent != control or events.parent != control or cancel == events:
        raise ValueError("control_files_must_be_distinct_config_siblings")
    if config_path is not None and config_path in {cancel, events}:
        raise ValueError("config_cannot_be_a_control_file")
    if any(control == root or root in control.parents for root in (snapshot, source_repo)):
        raise ValueError("control_directory_inside_source")
    return config


def load_config(path: Path) -> dict:
    """Load only the trusted launcher JSON; tool requests cannot provide paths."""
    path = _canonical(str(path), exists=True)
    if path.suffix != ".json" or path.stat().st_size > 32 * 1024:
        raise ValueError("invalid_job_config_file")
    config = json.loads(path.read_text(encoding="utf-8"))
    return _validate_config(config, control_directory=path.parent, config_path=path)


class _JobCancelled(RuntimeError):
    pass


class _Control:
    def __init__(self, config):
        self.config = config
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + config["timeout_seconds"]
        self._journal_lock = threading.Lock()

    def check(self):
        cancel = _canonical(self.config["cancel_file"])
        if self.cancelled.is_set() or cancel.exists():
            raise _JobCancelled("diagnostic_job_cancelled")
        if time.monotonic() >= self.deadline:
            raise subprocess.TimeoutExpired("MCP registered diagnostic", self.config["timeout_seconds"])

    def journal(self, value):
        path = _canonical(self.config["events_file"])
        data = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
        with self._journal_lock:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

    def container(self, name, state):
        if not isinstance(name, str) or not re.fullmatch(r"repair-probe-[a-f0-9]{16}", name):
            raise ValueError("unrecognized_diagnostic_container")
        if state not in {"reserved", "cleaned"}:
            raise ValueError("invalid_container_state")
        self.journal({"name": name, "state": state})

    def event(self, event, status=None, error_type=None):
        value = {"event": event, "report_id": self.config["report_id"]}
        if status is not None:
            value["status"] = status
        if error_type is not None:
            value["error_type"] = error_type
        self.journal(value)


def build_server(config: dict) -> MCPServer:
    """Bind one process to one immutable snapshot, workload and output directory."""
    config = _validate_config(config)
    async def strict_arguments(context, call_next):
        # The SDK's default function-argument model ignores extra keys. Reject
        # them at its public context middleware boundary rather than accepting
        # path/command-looking fields that have no meaning for this service.
        if context.method == "tools/call" and context.params is not None:
            allowed = {"list_diagnostics": set(), "run_probe": {"probe"}, "read_report": {"report_id"}}
            name = context.params.get("name")
            arguments = context.params.get("arguments", {})
            if arguments is None:
                arguments = {}
            if name in allowed and (not isinstance(arguments, dict) or set(arguments) != allowed[name]):
                return CallToolResult(content=[TextContent(type="text", text="unexpected_diagnostic_tool_arguments")],
                                      is_error=True)
        return await call_next(context)

    server = MCPServer("repair-workbench-diagnostics", version="1", log_level="WARNING",
                       instructions="Only registered probes for this application-bound job are available.",
                       middleware=[strict_arguments])
    lock = threading.Lock()
    state = {"used": False, "probe": None, "finished": False}

    def envelope():
        with lock:
            if not state["finished"]:
                raise ToolError("diagnostic_report_not_ready")
            probe = state["probe"]
        path = _canonical(str(Path(config["destination"]) / "result.json"), exists=True)
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            raise ToolError("diagnostic_report_too_large")
        report = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(report, dict) or report.get("snapshot_id") != config["snapshot_id"]
                or report.get("workload_id") != config["workload"] or report.get("probe") != probe
                or report.get("status") not in {"ok", "timeout", "execution_error", "report_error"}
                or not isinstance(report.get("summary"), dict)
                or type(report.get("cleanup_ok")) is not bool
                or report.get("status") == "ok" and not re.fullmatch(r"sha256:[a-f0-9]{64}", str(report.get("image_id")))
                or config["image"].startswith("sha256:") and report.get("image_id") not in {None, config["image"]}):
            raise ToolError("diagnostic_report_identity_mismatch")
        value = {"report_id": config["report_id"], "status": report["status"],
                 "summary": report["summary"], "report": report}
        try:
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError):
            raise ToolError("invalid_diagnostic_report") from None
        if size > MAX_RESPONSE_BYTES:
            raise ToolError("diagnostic_report_too_large")
        return value

    @server.tool(structured_output=True)
    async def list_diagnostics() -> dict[str, Any]:
        """Describe the fixed diagnostic workload and this one-shot job; no code executes."""
        workload = get_workload(config["workload"])
        return {"workload_id": workload.id, "snapshot_id": config["snapshot_id"],
                "report_id": config["report_id"], "diagnostics": ["cpu", "memory", "benchmark"],
                "entrypoint": workload.entrypoint, "one_shot": True,
                "timeout_seconds": config["timeout_seconds"]}

    @server.tool(structured_output=True)
    async def run_probe(probe: Literal["cpu", "memory", "benchmark"]) -> dict[str, Any]:
        """Execute one registered probe for this bound snapshot; no paths or commands are accepted."""
        with lock:
            if state["used"]:
                raise ToolError("diagnostic_job_already_used")
            state.update(used=True, probe=probe)
        control = _Control(config)

        def execute():
            control.check()
            manifest = verify_snapshot(Path(config["snapshot"]))
            if snapshot_id(manifest) != config["snapshot_id"]:
                raise ValueError("snapshot_identity_mismatch")
            return execute_probe(Path(config["snapshot"]), Path(config["destination"]),
                                 config["image"], probe, config["workload"], config["timeout_seconds"],
                                 execution_control=control)

        control.event("probe_started")
        worker = asyncio.create_task(asyncio.to_thread(execute))
        try:
            result = await asyncio.shield(worker)
            with lock:
                state["finished"] = True
            value = envelope()
            if value["report"] != result:
                raise ToolError("diagnostic_result_differs_from_record")
            control.event("probe_finished", status=value["status"])
            return value
        except anyio.get_cancelled_exc_class():
            control.cancelled.set()
            # MCP cancellation must not abandon the thread that owns Docker cleanup.
            with anyio.CancelScope(shield=True):
                try:
                    await worker
                except Exception:
                    pass
                with lock:
                    state["finished"] = True
                control.event("probe_cancelled", status="cancelled")
            raise
        except _JobCancelled:
            with lock:
                state["finished"] = True
            control.event("probe_cancelled", status="cancelled")
            raise ToolError("diagnostic_job_cancelled") from None
        except Exception as exc:
            with lock:
                state["finished"] = True
            control.event("probe_error", status="execution_error", error_type=type(exc).__name__)
            if isinstance(exc, ToolError):
                raise
            raise ToolError("diagnostic_job_execution_error") from None

    @server.tool(structured_output=True)
    async def read_report(report_id: str) -> dict[str, Any]:
        """Read this job's bounded recorded result by its opaque ID; never accepts a path."""
        if report_id != config["report_id"]:
            raise ToolError("unknown_diagnostic_report")
        try:
            return envelope()
        except (OSError, ValueError, TypeError, KeyError):
            raise ToolError("unusable_diagnostic_report") from None

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        server = build_server(load_config(args.config))
        server.run(transport="stdio")
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "mcp_server_error", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
