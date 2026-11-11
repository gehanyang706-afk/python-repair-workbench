"""Conservative visible-observation repetition checks for diagnostic tool loops."""
from __future__ import annotations

import hashlib
import json

from .session import BudgetExceeded
from .snapshot import snapshot_id


POLICY_VERSION = 2
_VOLATILE = {'source_ref', 'source_refs', 'evidence_ref', 'evidence_refs', 'hypothesis_id',
             'revision', 'remaining_model_calls', 'remaining_calls', 'elapsed_seconds',
             'delegation_elapsed_seconds', 'reused', 'scope', 'next_action', 'diagnosis'}
_OBSERVATIONS = {'read_source', 'read_symbol', 'source_outline', 'search_source',
                 'list_sources', 'read_evidence', 'read_probe_report', 'read_test_log', 'run_probe',
                 'run_preview_probe', 'read_file'}
_JUDGEMENTS = {'record_hypothesis', 'write_todos'}


def initial_progress_state():
    return {'policy_version': POLICY_VERSION, 'rounds_observed': 0, 'no_progress_streak': 0,
            'warning_count': 0, 'stopped': False, 'processed_rounds': [], 'records': [],
            'report': None, 'stop_reason': None}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _VOLATILE}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def progress_summary(state):
    return {key: value for key, value in state.items() if key not in {'records', 'processed_rounds'}}


def _save(session, state):
    role = getattr(session, 'progress_agent_role', 'main')
    name = f'investigator-{session.delegation_count:02d}' if role == 'investigator' else 'main'
    state['report'] = f'progress/{name}.json'
    path = session.output / state['report']
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    if role == 'investigator':
        session._budget()['progress'] = progress_summary(state)
    session.save('running')


def _stop(session, state):
    # A simultaneous root cancellation/deadline must win over local repetition.
    session.check_budget()
    child = getattr(session, 'progress_agent_role', 'main') == 'investigator'
    reason = 'investigator_no_progress' if child else 'diagnostic_no_progress'
    state.update(stopped=True, stop_reason=reason)
    if child:
        session.progress_stop_reason = reason
    else:
        session.diagnostic_stop_reason = reason
    session.record('diagnostic_progress_stopped', streak=state['no_progress_streak'], reason=reason)
    _save(session, state)
    if child:
        from .diagnostic_delegation import ChildQuotaExceeded
        raise ChildQuotaExceeded(reason)
    raise BudgetExceeded(reason)


def _fingerprint(session, call, message, model_round=None):
    name, arguments = call.get('name'), call.get('args', {})
    if message.status == 'error' or not isinstance(arguments, dict):
        return None
    text = message.content if isinstance(message.content, str) else message.text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        if name == 'read_file':
            data = {'content': text}
        elif name in _JUDGEMENTS:
            data = {}
        else:
            return None
    if name == 'task' and isinstance(data, dict) and model_round is not None:
        refs = data.get('evidence_refs', [])
        matching = next((record for record in session.delegation_history
                         if record.get('parent_model_round') == model_round
                         and record.get('tool_call_id_sha256') == hashlib.sha256(call['id'].encode()).hexdigest()
                         and record.get('status') == 'investigator_proposal'
                         and record.get('model_calls_used', 0) > 0
                         and record.get('returned_sha256') == hashlib.sha256(text.encode()).hexdigest()), None)
        if (matching is None or data.get('status') != 'investigator_proposal'
                or not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or ref not in session.evidence for ref in refs)):
            return None
        # One completed, admitted investigation is a recovery opportunity for main.
        material = {'delegation': matching['number'], 'receipt': matching['returned_sha256']}
        args, observation = {}, True
    elif name in _JUDGEMENTS:
        # New IDs, revisions and rewritten explanations are still model judgements.
        material, args, observation = {'model_judgement': True}, {}, False
    elif name in _OBSERVATIONS and isinstance(data, dict):
        rejected = data.get('status') in {'rejected', 'inconclusive', 'error'}
        observation = not rejected
        material, args = dict(data), dict(arguments)
        if name in {'run_probe', 'run_preview_probe', 'read_evidence'}:
            material.pop('sha256', None)  # Evidence JSON hash includes issued random refs.
            args.pop('evidence_ref', None)
        if name == 'read_source':
            args = {'path': arguments.get('path'), 'start_line': arguments.get('start_line', 1),
                    'line_count': arguments.get('line_count', 120)}
        elif name == 'search_source':
            args = {'query': arguments.get('query'), 'path': arguments.get('path')}
        elif name == 'list_sources':
            args = {'offset': arguments.get('offset', 0), 'limit': arguments.get('limit', 100)}
        elif name in {'run_probe', 'run_preview_probe'}:
            args = {'probe': arguments.get('probe'), 'attempt': arguments.get('attempt', 0)}
            if getattr(session, 'repository_diagnostics_enabled', False):
                observation_data = data.get('observation')
                args['case_id'] = (observation_data.get('case_id', arguments.get('case_id'))
                                   if isinstance(observation_data, dict) else arguments.get('case_id'))
                if name == 'run_preview_probe':
                    args['preview_ref'] = arguments.get('preview_ref')
        if rejected:
            material = {'status': data.get('status'), 'reason': data.get('reason')}
    elif name in {'submit_ref_edits', 'preview_ref_edits'} and isinstance(data, dict):
        attempt = next((item for item in session.attempts if item['number'] == data.get('number')), {})
        observation = bool(attempt.get('content_id') and (data.get('evidence_ref') or
                           (name == 'preview_ref_edits' and attempt.get('status') == 'preview_ready')))
        material = {'content_id': attempt.get('content_id'), 'status': data.get('status'),
                    'reason': data.get('reason')}
        args = {}
    else:
        return None
    binding = {'baseline': snapshot_id(session.manifest), 'workload': session.workload,
               'tool': name, 'policy_version': POLICY_VERSION}
    stable_args, stable_material = _clean(args), _clean(material)
    return {'tool': name, 'action_sha256': _digest([binding, stable_args]),
            'observation_sha256': _digest(stable_material),
            'fingerprint': _digest([binding, stable_args, stable_material]),
            'is_observation': observation}


def _rounds(session, messages):
    """Only pair results with an actual visible AI tool call; never a history cache."""
    rounds = []
    for index, message in enumerate(messages):
        if getattr(message, 'type', None) != 'ai' or not message.tool_calls:
            continue
        following = []
        for result in messages[index + 1:]:
            if getattr(result, 'type', None) == 'ai':
                break
            if getattr(result, 'type', None) == 'tool':
                following.append(result)
        pairs = []
        for call in message.tool_calls:
            results = [result for result in following if result.tool_call_id == call['id']]
            if len(results) != 1:
                break
            pairs.append((call, results[0]))
        else:
            rounds.append((index, message, pairs))
    return rounds


def observe_tool_round(session, messages):
    """Return one warning or stop before the next provider reservation."""
    session.check_budget()
    state = session.diagnostic_progress
    if state['stopped']:
        _stop(session, state)
    batch = session.active_tool_round
    if not batch or batch['call'] in state['processed_rounds'] or session.final_decision:
        return None
    rounds = _rounds(session, messages)
    matches = [item for item in rounds if {call['id'] for call in item[1].tool_calls} == set(batch['ids'])]
    if not matches:
        return None
    current_index, _, pairs = matches[-1]
    state['processed_rounds'].append(batch['call'])
    if any(message.status == 'error' for _, message in pairs):
        return None  # Schema/parse recovery keeps its existing independent limits.
    previous = {item['fingerprint'] for index, _, prior in rounds if index < current_index
                for call, message in prior if (item := _fingerprint(session, call, message))}
    current = [item for call, message in pairs
               if (item := _fingerprint(session, call, message, batch['call']))]
    if not current:
        return None
    new = any(item['is_observation'] and item['fingerprint'] not in previous for item in current)
    repeated = any(item['fingerprint'] in previous for item in current)
    state['rounds_observed'] += 1
    if new:
        state['no_progress_streak'] = 0
    elif repeated:
        state['no_progress_streak'] += 1
    record = {'model_round': batch['call'], 'new_observation': new, 'visible_repeat': repeated,
              'streak': state['no_progress_streak'], 'observations': current}
    state['records'].append(record)
    session.record('diagnostic_progress_observed', model_round=batch['call'],
                   new_observation=new, visible_repeat=repeated, streak=record['streak'],
                   fingerprints=[item['fingerprint'] for item in current])
    if state['no_progress_streak'] >= 4:
        _stop(session, state)
    warning = None
    if state['no_progress_streak'] == 3 and repeated and not new:
        state['warning_count'] += 1
        session.record('diagnostic_progress_warning', model_round=batch['call'], streak=3)
        warning = ('当前版本连续三轮重复仍可见的工具观察，没有新增可区分证据。'
                   '请在原预算内换范围或实验、提交已有候选，或提出证据不足结论；'
                   '仅改写假设不算新观察。下一轮仍重复将停止。')
    _save(session, state)
    return warning
