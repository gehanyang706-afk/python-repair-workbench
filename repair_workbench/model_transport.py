"""Bound and reap the local process that owns a provider request and its sockets."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import messages_from_dict, messages_to_dict
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field
from .session import BudgetExceeded


class ProviderRequestError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def run_request_worker(payload, session, call_timeout=30, *, command=None):
    session.check_budget()
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > 8 * 1024 * 1024:
        raise ProviderRequestError('model_input_too_large')
    started = time.monotonic()
    process = subprocess.Popen(command or [sys.executable, '-m', 'repair_workbench.provider_worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, 'LANGSMITH_TRACING': 'false',
                                     'LANGCHAIN_TRACING_V2': 'false'})
    session.record('model_worker_started', call=session.model_calls, pid=process.pid,
                   call_timeout_seconds=call_timeout)
    reason = 'completed'
    try:
        first = True
        while True:
            session.check_budget()
            remaining = call_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise BudgetExceeded('model_call_timeout')
            try:
                output, _ = process.communicate(input=encoded if first else None, timeout=min(.05, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        if process.returncode != 0 or len(output) > 2 * 1024 * 1024:
            raise ProviderRequestError('model_worker_failed')
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise ProviderRequestError('model_worker_protocol_error') from None
        if 'error' in result:
            code = result['error']
            if code not in {'Arrearage', 'AuthenticationFailed', 'provider_connect_timeout',
                            'provider_read_timeout', 'provider_error'}:
                code = 'provider_error'
            raise ProviderRequestError(code)
        return result
    except (BudgetExceeded, ProviderRequestError) as exc:
        reason = str(exc)
        raise
    except KeyboardInterrupt:
        reason = 'cancel_requested'
        raise
    finally:
        cleanup_ok = False
        try:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1)
            cleanup_ok = process.poll() is not None
        except (OSError,subprocess.TimeoutExpired):
            cleanup_ok=False
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            session.record('model_worker_finished', call=session.model_calls, pid=process.pid,
                           cleanup_ok=cleanup_ok, reason=reason if cleanup_ok else 'model_worker_cleanup_failed',
                           elapsed_seconds_worker=round(time.monotonic() - started, 3))
        if not cleanup_ok:
            raise ProviderRequestError('model_worker_cleanup_failed')


class ManagedTongyi(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    model_name: str = 'qwen-plus'
    api_key: str = Field(exclude=True, repr=False)
    tool_schemas: list = Field(default_factory=list)
    call_timeout: float = 30
    connect_timeout: float = 5
    read_timeout: float = 30

    @property
    def _llm_type(self):
        return 'repair-workbench:managed-tongyi'

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={'tool_schemas': [convert_to_openai_tool(tool) for tool in tools]})

    def invoke_controlled(self, messages, session, stop=None, **kwargs):
        result = run_request_worker({'model': self.model_name, 'api_key': self.api_key,
            'messages': messages_to_dict(messages), 'tools': self.tool_schemas, 'stop': stop,
            'connect_timeout': self.connect_timeout, 'read_timeout': self.read_timeout},
            session, self.call_timeout)
        return messages_from_dict([result['message']])[0]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError('managed_provider_requires_repair_session')
