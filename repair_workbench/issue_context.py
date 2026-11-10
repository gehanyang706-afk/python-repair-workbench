"""Bounded Issue-only retrieval using the same charged source tools as the Agent."""
from __future__ import annotations

import json
import keyword
import re

MAX_QUERIES = 4
MAX_EXCERPTS = 3
MAX_CHARACTERS = 6000
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')
_STOP_WORDS = frozenset(keyword.kwlist) | frozenset({
    'a', 'an', 'the', 'this', 'that', 'these', 'those', 'it', 'its', 'when', 'with',
    'without', 'after', 'before', 'using', 'use', 'used', 'does', 'not', 'have', 'has',
    'will', 'would', 'should', 'could', 'can', 'cannot', 'instead', 'please', 'both',
    'python', 'issue', 'bug', 'function', 'method', 'class', 'argument', 'arguments',
    'return', 'returns', 'returned', 'expected', 'actual', 'result', 'value', 'values',
    'works', 'fails', 'failed', 'file', 'line', 'code', 'test', 'tests', 'error',
})


def issue_queries(issue: str) -> list[str]:
    """Prefer literal code anchors, then distinctive words; never infer a path."""
    if not isinstance(issue, str) or not 1 <= len(issue) <= 12000:
        raise ValueError('Issue must contain 1..12000 characters')
    candidates = []
    for quoted in re.finditer(r'`([^`\n]{1,160})`', issue):
        candidates.extend(_IDENTIFIER.findall(quoted.group(1)))
    candidates.extend(match.group(1) for match in re.finditer(
        r'([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(', issue))
    words = _IDENTIFIER.findall(issue)
    candidates.extend(word for word in words if '_' in word or '.' in word
                      or any(c.isupper() for c in word[1:]))
    candidates.extend(word for word in words if len(word) >= 4)
    queries = []
    for candidate in candidates:
        # A qualified call's leaf is also a literal identifier in the Issue.
        query = candidate.rsplit('.', 1)[-1]
        if (2 <= len(query) <= 120 and query.casefold() not in _STOP_WORDS
                and query not in queries):
            queries.append(query)
        if len(queries) == MAX_QUERIES:
            break
    return queries


def issue_evidence(session) -> dict:
    """Return source receipts, not hypotheses or acceptance data.

    search_source already uses static AST owners and issues version-bound raw
    source_ref receipts. Its budget and all source reads remain charged even
    when the initial model packet omits excess matches.
    """
    session.check_budget()
    queries = issue_queries(session.public_task['problem_statement'])
    packet = {'status': 'no_match', 'source_excerpts': [],
              'scope': 'Literal Issue anchors in approved baseline sources only; matches are not a diagnosis.',
              'navigation': 'Use the unchanged source tools to investigate missing or unrelated evidence.'}
    stats = {'search_attempts': 0, 'tool_calls': 0, 'retrieved_matches': 0,
             'source_reads': 0, 'retrieved_characters': 0, 'excerpt_count': 0,
             'excerpt_characters': 0, 'packet_characters': 0, 'complete': False}
    before_tools = session.tool_calls
    before_reads = session.context_stats['reads']
    before_characters = session.context_stats['returned_characters']
    seen = set()
    try:
        for query in queries:
            session.check_budget()
            stats['search_attempts'] += 1
            session.record_tool_statistic('search_source')
            result = session.search_source(query)
            stats['retrieved_matches'] += len(result['matches'])
            for hit in result['matches']:
                identity = (hit['path'], hit['start_line'])
                # Avoid treating substring matches such as 'add' in 'padding'
                # as evidence for an Issue's symbol. No file is guessed.
                if (hit['path'] not in session.allowed_paths or identity in seen
                        or not re.search(r'(?<!\w)' + re.escape(query) + r'(?!\w)', hit['text'])):
                    continue
                excerpt = {key: hit[key] for key in ('path', 'start_line', 'content', 'source_ref')}
                excerpt.update(query=query, matched_line=hit['line'], owner=hit.get('owner'))
                projected = {**packet, 'status': 'matched',
                             'source_excerpts': packet['source_excerpts'] + [excerpt]}
                if len(json.dumps(projected, ensure_ascii=False)) > MAX_CHARACTERS:
                    continue
                packet = projected
                seen.add(identity)
                if len(packet['source_excerpts']) == MAX_EXCERPTS:
                    break
            if len(packet['source_excerpts']) == MAX_EXCERPTS:
                break
        session.check_budget()
        stats['complete'] = True
        return packet
    finally:
        stats.update(tool_calls=session.tool_calls - before_tools,
                     source_reads=session.context_stats['reads'] - before_reads,
                     retrieved_characters=session.context_stats['returned_characters'] - before_characters,
                     excerpt_count=len(packet['source_excerpts']),
                     excerpt_characters=sum(len(item['content']) for item in packet['source_excerpts']),
                     packet_characters=len(json.dumps(packet, ensure_ascii=False)))
        session.issue_context_stats = stats
        session.record('issue_initial_context', **stats)
