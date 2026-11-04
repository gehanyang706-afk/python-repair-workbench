"""Read-only evidence MCP service, reusable independently of the diagnostic Agent."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Literal

from pydantic import StrictInt

from .diagnostic_reports import ReportStore


def build_server(store):
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import CallToolResult, TextContent

    async def strict_arguments(context, call_next):
        if context.method == 'tools/call' and context.params is not None:
            name = context.params.get('name')
            arguments = context.params.get('arguments', {})
            if arguments is None:
                arguments = {}
            allowed = {'list_reports': set(), 'query_report': {'report_id', 'section', 'scale', 'offset', 'limit'}}
            if name in allowed and (not isinstance(arguments, dict) or set(arguments) - allowed[name]):
                return CallToolResult(content=[TextContent(type='text', text='unexpected_report_tool_arguments')],
                                      is_error=True)
        return await call_next(context)

    server = MCPServer('repair-workbench-evidence', version='1', log_level='WARNING',
                       instructions='Read-only measured evidence; never execute code or accept host paths.',
                       middleware=[strict_arguments])

    @server.tool(structured_output=True)
    def list_reports() -> dict[str, Any]:
        """Discover this frozen catalog; source paths and execution tools are not exposed."""
        return store.catalog()

    @server.tool(structured_output=True)
    def query_report(report_id: str, section: Literal['summary', 'hotspots', 'samples', 'environment'] = 'summary',
                     scale: Literal['small', 'large'] = 'large', offset: StrictInt = 0,
                     limit: StrictInt = 5) -> dict[str, Any]:
        """Read a bounded report page. CPU hotspots and measured samples retain snapshot identity."""
        try:
            return store.query(report_id, section, scale, offset, limit)
        except (ValueError, KeyError, TypeError):
            raise ToolError('invalid_or_unavailable_report_query') from None

    @server.resource('diagnostic://catalog', mime_type='application/json')
    def catalog() -> str:
        return json.dumps(store.catalog(), ensure_ascii=False)

    def register(identity):
        @server.resource(f'diagnostic://reports/{identity}', mime_type='application/json',
                         description='Large-workload summary; query_report retrieves bounded details.')
        def summary() -> str:
            return json.dumps(store.query(identity), ensure_ascii=False)

    for identity in store.reports:
        register(identity)
    return server


def server_parameters(bundle, sha256):
    from mcp import StdioServerParameters
    bundle = Path(bundle).absolute()
    # This service needs neither model credentials nor Docker connection settings.
    allowed = {'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'SYSTEMROOT'}
    environment = {k: v for k, v in os.environ.items() if k in allowed}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return StdioServerParameters(command=sys.executable,
        args=['-m', 'repair_workbench.diagnostic_report_mcp', '--bundle', str(bundle), '--sha256', sha256],
        cwd=str(Path(__file__).resolve().parent.parent), env=environment)


async def _query(parameters, arguments, expected_catalog):
    from mcp import Client
    from .diagnostic_mcp_client import _payload, MCPProbeError
    async with Client(parameters, cache=None) as client:
        tools = await client.list_tools()
        if {t.name for t in tools.tools} != {'list_reports', 'query_report'}:
            raise MCPProbeError('mcp_unexpected_tool_catalog')
        catalog = _payload(await client.call_tool('list_reports', {}))
        if catalog != expected_catalog:
            raise MCPProbeError('mcp_report_catalog_mismatch')
        return _payload(await client.call_tool('query_report', arguments))


def query_mcp_report(bundle, sha256, arguments, *, execution_control=None, timeout_seconds=10):
    """Official stdio client; compare the response against the frozen local record."""
    store = ReportStore(bundle, sha256)
    expected = store.query(**arguments)

    async def controlled():
        task = asyncio.create_task(_query(server_parameters(bundle, sha256), arguments, store.catalog()))
        deadline = time.monotonic() + timeout_seconds
        try:
            while not task.done():
                if execution_control:
                    execution_control.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('MCP evidence query timed out')
                await asyncio.sleep(.01)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        result = asyncio.run(controlled())
    except ExceptionGroup as exc:
        from .diagnostic_mcp_client import MCPProbeError
        from .tool_runtime import error_category
        error = MCPProbeError('mcp_report_exchange_failed')
        error.category = error_category(exc, 'mcp:reports')
        raise error from None
    if result != expected:
        from .diagnostic_mcp_client import MCPProbeError
        raise MCPProbeError('mcp_report_page_mismatch')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args(argv)
    build_server(ReportStore(args.bundle, args.sha256)).run(transport='stdio')


if __name__ == '__main__':
    main()
