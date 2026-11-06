"""Allow native graph fan-out only for independent read-only tool batches."""
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


READ_BATCH_TOOLS = frozenset({'list_sources', 'read_source', 'source_outline', 'read_symbol',
                              'search_source', 'read_test_log', 'read_evidence',
                              'read_probe_report', 'read_file'})


class DiagnosticConcurrencyMiddleware(AgentMiddleware):
    def __init__(self, session):
        self.session = session

    def wrap_tool_call(self, request, handler):
        messages = request.state.get('messages', [])
        parent = next((m for m in reversed(messages) if getattr(m, 'type', None) == 'ai'), None)
        calls = getattr(parent, 'tool_calls', [])
        if len(calls) > 1 and any(c.get('name') not in READ_BATCH_TOOLS for c in calls):
            # Reject the entire mixed batch before any mutation or measurement;
            # execution order must not decide which operation takes effect.
            self.session.reserve_tool(request.tool_call['name'])
            self.session.record('parallel_batch_rejected', tool=request.tool_call['name'],
                                reason='stateful_tool_requires_single_call')
            return ToolMessage(content=json.dumps({'status': 'rejected',
                'reason': 'stateful_tool_requires_single_call',
                'next_action': 'Batch independent read tools only; issue stateful tools in separate turns.'}),
                tool_call_id=request.tool_call['id'], name=request.tool_call['name'], status='error')
        return handler(request)
