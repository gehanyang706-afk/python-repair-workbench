"""Local stdio MCP diagnostics with a trusted job and parent-owned cancellation.

The protocol exposes a fixed diagnostic service, not arbitrary server commands.
The parent validates archived measurements and owns cleanup after disconnection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from .diagnostics import _validated_report, get_workload
from .snapshot import snapshot_id, verify_snapshot


class MCPProbeError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _read_json(path, limit=256 * 1024):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        raise MCPProbeError('mcp_report_unavailable')
    return json.loads(path.read_text(encoding='utf-8'))


def _payload(result):
    if result.is_error or not isinstance(result.structured_content, dict):
        raise MCPProbeError('mcp_tool_error')
    if len(json.dumps(result.structured_content, ensure_ascii=False)) > 256 * 1024:
        raise MCPProbeError('mcp_result_too_large')
    return result.structured_content


class _Journal:
    def __init__(self, path, control):
        self.path, self.control = path, control
        self.offset = 0
        self.tail = b''
        self.containers = {}
        self.events = []
        self.failure = None

    def drain(self):
        if not self.path.exists():
            return
        if self.path.is_symlink() or self.path.stat().st_size > 128 * 1024:
            raise MCPProbeError('mcp_invalid_journal')
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            chunk = stream.read(128 * 1024 + 1)
            self.offset += len(chunk)
        lines = (self.tail + chunk).split(b'\n')
        self.tail = lines.pop()
        for line in lines:
            if not line:
                continue
            self._consume(line)

    def _consume(self, line):
        item = json.loads(line)
        if not isinstance(item, dict):
            raise MCPProbeError('mcp_invalid_journal')
        if 'name' in item or 'state' in item:
            if (set(item) != {'name', 'state'}
                    or not isinstance(item['name'], str)
                    or not re.fullmatch(r'repair-probe-[a-f0-9]{16}', item['name'])
                    or item['state'] not in {'reserved', 'cleaned'}):
                raise MCPProbeError('mcp_invalid_container_receipt')
            name, state = item['name'], item['state']
            if (state == 'reserved' and name in self.containers
                    or state == 'cleaned' and self.containers.get(name) != 'reserved'):
                raise MCPProbeError('mcp_invalid_container_lifecycle')
            self.containers[name] = state
            if self.control:
                self.control.container(name, state)
        elif (item.get('event') not in {'probe_started', 'probe_finished', 'probe_cancelled', 'probe_error'}
              or set(item) - {'event', 'report_id', 'status', 'error_type'}):
            raise MCPProbeError('mcp_invalid_server_event')
        self.events.append(item)

    def finish(self):
        """Only after the server is closed, classify the final unterminated line."""
        self.drain()
        if self.tail:
            tail, self.tail = self.tail, b''
            try:
                self._consume(tail)
            except Exception:
                self.failure = 'incomplete_or_invalid_journal_tail'
                raise MCPProbeError('mcp_incomplete_journal') from None

    def cleanup_outstanding(self):
        cleaned = True
        for name, state in list(self.containers.items()):
            if state != 'reserved':
                continue
            try:
                result = subprocess.run(['docker', 'rm', '-f', name],
                                        capture_output=True, text=True, timeout=15)
                success = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                success = False
            if success:
                self.containers[name] = 'cleaned'
                if self.control:
                    self.control.container(name, 'cleaned')
            cleaned = cleaned and success
        return cleaned


def _server_parameters(config_path):
    from mcp import StdioServerParameters
    # No model credentials, tracing endpoints or provider configuration cross the boundary.
    allowed = {'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL',
               'SYSTEMROOT', 'DOCKER_HOST', 'DOCKER_CONTEXT'}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return StdioServerParameters(
        command=sys.executable,
        args=['-m', 'repair_workbench.diagnostic_mcp_server', '--config', str(config_path)],
        cwd=str(Path(__file__).resolve().parent.parent), env=environment)


async def _exchange(parameters, probe, report_id, discovery):
    from mcp import Client
    async with Client(parameters, cache=None) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        if names != {'list_diagnostics', 'run_probe', 'read_report'}:
            raise MCPProbeError('mcp_unexpected_tool_catalog')
        discovery.extend(tool.model_dump(mode='json', by_alias=True) for tool in tools.tools)
        info = _payload(await client.call_tool('list_diagnostics', {}))
        if info.get('snapshot_id') is None or info.get('workload_id') != 'ugc-export-v1':
            raise MCPProbeError('mcp_invalid_capabilities')
        response = _payload(await client.call_tool('run_probe', {'probe': probe}))
        archived = _payload(await client.call_tool('read_report', {'report_id': report_id}))
        if response != archived or response.get('report_id') != report_id:
            raise MCPProbeError('mcp_report_identity_mismatch')
        return response


async def _controlled_exchange(parameters, probe, config, journal, discovery, control):
    deadline = time.monotonic() + config['timeout_seconds']
    task = asyncio.create_task(_exchange(parameters, probe, config['report_id'], discovery))
    stop = None
    primary = None
    try:
        while not task.done():
            journal.drain()
            try:
                if control:
                    control.check()
                if time.monotonic() >= deadline:
                    raise MCPProbeError('mcp_probe_timeout')
            except BaseException as exc:
                stop = exc
                Path(config['cancel_file']).touch(exist_ok=True)
                break
            await asyncio.sleep(.05)
        if stop is not None:
            try:
                # Cleanup has its own grace period after the task's execution deadline.
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except BaseException:
                pass
            raise stop
        if control:
            control.check()
        response = await task
        journal.drain()
        if control:
            control.check()
        if time.monotonic() >= deadline:
            raise MCPProbeError('mcp_probe_timeout')
        return response
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if not task.done():
            Path(config['cancel_file']).touch(exist_ok=True)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except BaseException:
                pass
        try:
            journal.drain()
        except Exception as exc:
            journal.failure = type(exc).__name__
            if primary is None:
                raise


def _pin_image(image, timeout_seconds):
    if image.startswith('sha256:'):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('invalid immutable image identity')
        return image
    identity = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                              capture_output=True, text=True, check=True,
                              timeout=min(15, timeout_seconds)).stdout.strip()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', identity):
        raise MCPProbeError('mcp_invalid_local_image_identity')
    return identity


def run_mcp_probe(snapshot: Path, destination: Path, image: str, probe: str,
                  workload='ugc-export-v1', timeout_seconds=30, *, execution_control=None):
    """Use the official MCP SDK, then independently validate its on-disk result."""
    started = time.monotonic()
    get_workload(workload)
    if probe not in {'cpu', 'memory', 'benchmark'}:
        raise ValueError('unknown diagnostic probe')
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120):
        raise ValueError('invalid MCP diagnostic timeout')
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', image):
        raise ValueError('invalid diagnostic image')
    snapshot, destination = Path(snapshot).resolve(strict=True), Path(destination)
    manifest = verify_snapshot(snapshot)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('cannot overwrite MCP diagnostic results')
    destination = destination.resolve()
    for root in (snapshot, Path(manifest['source_repo']).resolve()):
        if destination == root or root in destination.parents:
            raise ValueError('MCP results must be outside the original repository and snapshot')
    if execution_control:
        execution_control.check()
    image = _pin_image(image, timeout_seconds)
    remaining = timeout_seconds - (time.monotonic() - started)
    if execution_control:
        execution_control.check()
    if remaining <= 0:
        raise MCPProbeError('mcp_probe_timeout')
    directory = destination.with_name(destination.name + '-mcp')
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    config = {'schema_version': 1, 'snapshot': str(snapshot),
              'snapshot_id': snapshot_id(manifest), 'destination': str(destination),
              'image': image, 'workload': workload, 'timeout_seconds': remaining,
              'cancel_file': str(directory / 'cancel'),
              'events_file': str(directory / 'containers.jsonl'),
              'report_id': 'probe-' + uuid.uuid4().hex}
    config_path = directory / 'config.json'
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    config_path.chmod(0o600)
    journal = _Journal(Path(config['events_file']), execution_control)
    discovery = []
    transport = {'protocol': 'mcp', 'sdk_version': '2.3.0', 'transport': 'stdio',
                 'report_id': config['report_id'], 'status': 'running', 'real_model_calls': 0}
    raised = None
    try:
        response = asyncio.run(_controlled_exchange(_server_parameters(config_path), probe,
                               config, journal, discovery, execution_control))
        journal.finish()
        report = response.get('report')
        if not isinstance(report, dict) or _read_json(destination / 'result.json') != report:
            raise MCPProbeError('mcp_archived_result_mismatch')
        if (report.get('snapshot_id') != config['snapshot_id'] or report.get('probe') != probe
                or report.get('workload_id') != workload
                or report.get('workload_fingerprint') != get_workload(workload).fingerprint):
            raise MCPProbeError('mcp_result_binding_mismatch')
        if (report.get('image_id') != image
                and not (report.get('status') != 'ok' and report.get('image_id') is None)):
            raise MCPProbeError('mcp_image_identity_mismatch')
        worker = Path(__file__).with_name('probe_worker.py')
        if report.get('harness_sha256') != hashlib.sha256(worker.read_bytes()).hexdigest():
            raise MCPProbeError('mcp_harness_identity_mismatch')
        if report.get('status') == 'ok':
            checked = _validated_report(destination / 'probe.json', probe, get_workload(workload))
            if any(report.get(key) != value for key, value in checked.items()):
                raise MCPProbeError('mcp_measurement_validation_failed')
        if (report.get('cleanup_ok') is not True
                or any(state != 'cleaned' for state in journal.containers.values())):
            raise MCPProbeError('mcp_cleanup_unconfirmed')
        transport['status'] = 'completed'
        return report
    except BaseException as exc:
        raised = exc
        transport.update(status='stopped', error_type=type(exc).__name__)
        if isinstance(exc, MCPProbeError):
            raise
        # Application BudgetExceeded/cancellation must retain its original stop reason.
        from .session import BudgetExceeded
        if isinstance(exc, (BudgetExceeded, KeyboardInterrupt)):
            raise
        raise MCPProbeError('mcp_transport_error') from None
    finally:
        try:
            journal.finish()
        except Exception as exc:
            journal.failure = type(exc).__name__
        transport['journal_valid'] = journal.failure is None
        cleanup_ok = journal.cleanup_outstanding() and journal.failure is None
        transport.update(cleanup_ok=cleanup_ok,
                         elapsed_seconds=round(time.monotonic() - started, 6),
                         tools=discovery, containers=journal.containers,
                         server_events=journal.events)
        (directory / 'transport.json').write_text(json.dumps(transport, ensure_ascii=False, indent=2) + '\n')
        if raised is None and not cleanup_ok:
            raise MCPProbeError('mcp_cleanup_unconfirmed')
