"""Private stdin/stdout provider protocol. Credentials and exceptions never go to logs."""
import contextlib
import io
import json
import re
import sys


def request(payload):
    endpoint = payload.get('provider_base_url')
    if endpoint not in (None, 'https://dashscope.aliyuncs.com/api/v1'):
        raise ValueError('unsupported explicit provider endpoint')
    import dashscope
    from langchain_community.chat_models import ChatTongyi
    from langchain_core.messages import message_to_dict, messages_from_dict
    from langsmith import tracing_context
    import dashscope.api_entities.http_request as transport
    # The pinned SDK otherwise retransmits ConnectionError once even with the
    # LangChain retry count set to zero. One admitted model call sends once.
    original_send = transport._send_with_retry
    original_endpoint = dashscope.base_http_api_url
    if endpoint is not None:
        dashscope.base_http_api_url = endpoint
    transport._send_with_retry = lambda send: send()
    try:
        options = payload.get('provider_options', {'max_tokens': 4096, 'temperature': None})
        if (set(options) != {'max_tokens', 'temperature'}
                or type(options['max_tokens']) is not int or not 1 <= options['max_tokens'] <= 8192
                or (options['temperature'] is not None and
                    (type(options['temperature']) not in (int, float)
                     or not 0 <= options['temperature'] <= 2))):
            raise ValueError('invalid provider options')
        model_kwargs = {'request_timeout': (payload['connect_timeout'], payload['read_timeout']),
                        'max_tokens': options['max_tokens']}
        if options['temperature'] is not None:
            model_kwargs['temperature'] = options['temperature']
        model = ChatTongyi(model=payload['model'], api_key=payload['api_key'], max_retries=0,
            model_kwargs=model_kwargs, cache=False)
        bound = model.bind_tools(payload['tools']) if payload['tools'] else model
        with tracing_context(enabled=False):
            message = bound.invoke(messages_from_dict(payload['messages']), stop=payload['stop'])
        return {'message': message_to_dict(message)}
    finally:
        dashscope.base_http_api_url = original_endpoint
        transport._send_with_retry = original_send
        transport.close_shared_sync_session()



def classify_error(exc):
    text = str(exc)
    if 'AllocationQuota.FreeTierOnly' in text:
        return 'free_quota_exhausted'
    if re.search(r'code:\s*Arrearage\b', text):
        return 'Arrearage'
    if re.search(r'(?i)status_code:\s*401\b|invalid.?api.?key|authentication.failed', text):
        return 'AuthenticationFailed'
    if type(exc).__name__ == 'ConnectTimeout':
        return 'provider_connect_timeout'
    if type(exc).__name__ == 'ReadTimeout':
        return 'provider_read_timeout'
    return 'provider_error'


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
        with contextlib.redirect_stdout(io.StringIO()):
            result = request(payload)
    except Exception as exc:
        result = {'error': classify_error(exc)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
