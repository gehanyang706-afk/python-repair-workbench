"""Collect one tool round by call ID; never persist rejected parameter values."""
import hashlib
import json
import re
from langchain_core.messages import AIMessage, ToolMessage
BUSINESS_TOOLS = {'submit_ref_edits','read_source','read_test_log','list_sources',
                  'read_symbol','source_outline','search_source','record_behavior_summary'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def collect_errors(session, messages):
    errors = []
    batch = session.active_tool_round
    if batch:
        indices = [i for i, message in enumerate(messages) if isinstance(message, AIMessage)
                   and {call['id'] for call in message.tool_calls} == set(batch['ids'])]
        if indices:
            results = [message for message in messages[indices[-1] + 1:]
                       if isinstance(message, ToolMessage) and message.tool_call_id in batch['ids']]
            for result in results:
                key = (batch['call'], result.tool_call_id)
                if key in session.processed_tool_results:
                    continue
                session.processed_tool_results.add(key)
                tool = batch['ids'][result.tool_call_id]
                if result.status != 'error' or tool not in BUSINESS_TOOLS:
                    continue
                match = re.search(r'^\s*([A-Za-z][A-Za-z0-9_.]*): (Input should be a valid '
                                  r'(string|integer|list|dictionary|boolean|number)|Field required|Extra inputs are not permitted)',
                                  str(result.content), flags=re.MULTILINE)
                field = match.group(1) if match else 'arguments'
                if not re.fullmatch(r'edits(?:\.\d+\.(?:source_ref|old_text|new_text))?|'
                                    r'diagnosis|path|start_line|line_count|attempt|offset|limit|'
                                    r'query|symbol|force|change|preserve|related_entries',field):
                    field='arguments'
                expected = match.group(3) if match and match.group(3) else 'tool_schema'
                error = {'kind': 'schema', 'tool':tool,'field': field, 'expected_type': expected}
                batch['had_errors'] = True
                errors.append(error)
                session.submission_counts['schema_rejections'] += 1
                session.record('schema_rejected', tool=tool, round=batch['call'],
                               tool_call_id_sha256=digest(result.tool_call_id),
                               error_fingerprint=digest(json.dumps(error, sort_keys=True)),
                               field=field, expected_type=expected)
            if (not errors and not batch.get('had_errors') and not session.pending_parse_errors
                    and len({r.tool_call_id for r in results}) == len(batch['ids'])):
                session.invalid_tool_fingerprint = None
    if session.pending_parse_errors:
        errors.extend(session.pending_parse_errors)
        session.pending_parse_errors = []
    return errors


def feedback(errors):
    examples={'submit_ref_edits':{'edits':[{'source_ref':'<已返回的引用>','old_text':'<原文>','new_text':'<新内容>'}],
                                 'diagnosis':'<原因>'},
              'read_source':{'path':'<快照相对路径>','start_line':1,'line_count':120},
              'read_symbol':{'path':'<快照相对路径>','symbol':'<outline中的符号>'}}
    selected={name:example for name,example in examples.items() if any(error.get('tool')==name for error in errors)}
    return ('工具调用参数不符合 Schema。安全错误摘要：' + json.dumps(errors, ensure_ascii=False)
            + '。最小结构：' + json.dumps(selected,ensure_ascii=False)
            + '。只修正错误字段，不要猜测引用、补造代码或扩大权限。')
