"""Charge framework tools to the same diagnostic budget as application tools."""
from langchain.agents.middleware import AgentMiddleware


class DiagnosticFileMiddleware(AgentMiddleware):
    def __init__(self, session):
        self.session = session

    def wrap_tool_call(self, request, handler):
        name = request.tool_call['name']
        if name in {'read_file', 'write_todos'}:
            self.session.reserve_tool(name)
            if name == 'read_file':
                path = request.tool_call.get('args', {}).get('file_path')
                self.session.record('diagnostic_file_read', virtual_path=path,
                                    scope='Session virtual state; no host filesystem.')
            else:
                self.session.record('diagnostic_framework_tool', tool=name)
            runtime = getattr(self.session, 'tool_runtime', None)
            if runtime is not None:
                return runtime.invoke('local', name, request.tool_call.get('args', {}),
                                      lambda: handler(request), check=self.session.check_budget)
        return handler(request)
