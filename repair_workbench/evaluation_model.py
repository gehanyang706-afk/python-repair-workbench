"""Meter an existing managed provider transport without changing its retry policy."""
from pathlib import Path

from langchain_core.messages import message_to_dict, messages_to_dict
from pydantic import Field

from .evaluation_budget import _atomic_write
from .model_transport import ManagedTongyi


class MeteredEvaluationTongyi(ManagedTongyi):
    provider_base_url: str | None = 'https://dashscope.aliyuncs.com/api/v1'
    ledger: object = Field(exclude=True, repr=False)
    run_id: str
    trace_dir: Path = Field(exclude=True, repr=False)
    validate_frozen: object = Field(exclude=True, repr=False)
    evaluation_role: str = 'main'

    def invoke_controlled(self, messages, session, stop=None, **kwargs):
        self.validate_frozen()
        if self.max_output_tokens > self.ledger.output_token_limit:
            raise ValueError('model_output_exceeds_reserved_bound')
        call = self.ledger.reserve(self.run_id, role=self.evaluation_role)
        trace = self.trace_dir / ('call-%03d' % call['index'])
        try:
            _atomic_write(trace.with_suffix('.request.json'), {
                'messages': messages_to_dict(messages), 'tools': self.tool_schemas,
                'model': self.model_name, 'max_output_tokens': self.max_output_tokens,
                'temperature': self.sampling_temperature, 'role': self.evaluation_role}, create=True)
            message = super().invoke_controlled(messages, session, stop, **kwargs)
            saved = message_to_dict(message)
            saved['data']['response_metadata'] = {
                k: v for k, v in saved['data'].get('response_metadata', {}).items()
                if k in {'token_usage', 'finish_reason', 'model_name', 'model'}}
            saved['data']['id'] = None
            _atomic_write(trace.with_suffix('.response.json'), saved, create=True)
        except BaseException as exc:
            self.ledger.unknown(call, type(exc).__name__)
            raise
        self.ledger.settle(call, message)
        return message
