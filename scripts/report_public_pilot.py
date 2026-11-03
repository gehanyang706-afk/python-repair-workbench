"""Paired analysis; incomplete global-budget pairs never imply Token savings."""
import argparse
import json
from pathlib import Path

VARIANTS = ('on_demand', 'failure_context')
GLOBAL_STOPS = {'batch_token_limit', 'batch_usage_unknown', 'batch_deadline',
                'cancel_requested', 'provider_account_unavailable'}


def usage(row, ledger):
    calls = [c for c in ledger['calls'] if c['run_id'] == row['id']]
    if row['state'] == 'not_run' or any(c['state'] != 'settled' for c in calls):
        return None
    return {'provider_calls': len(calls),
            'input_tokens': sum(c['input_tokens'] for c in calls),
            'output_tokens': sum(c['output_tokens'] for c in calls),
            'total_tokens': sum(c['charged_tokens'] for c in calls)}


def eligible(row):
    return (row['state'] == 'completed' and
            row.get('agent', {}).get('stop_reason') not in GLOBAL_STOPS and
            row['outcome'] != 'evaluation_error')


def group(rows, ledger):
    totals = [usage(row, ledger) for row in rows]
    known = all(u is not None for u in totals)
    passes = sum(r['outcome'] == 'passed' for r in rows)
    tools = sum(r.get('agent', {}).get('tool_calls', 0) for r in rows)
    reads = sum(r.get('agent', {}).get('context_stats', {}).get('reads', 0) for r in rows)
    repeated = sum(r.get('agent', {}).get('context_stats', {}).get('repeated_reads', 0) for r in rows)
    materialized = sum(r.get('agent', {}).get('submission_counts', {}).get('materialized', 0) for r in rows)
    attempts = [a for r in rows for a in r.get('agent', {}).get('attempts', [])]
    tokens = sum(u['total_tokens'] for u in totals) if known else None
    return {'runs': len(rows), 'distinct_bugs': len({r['task'] for r in rows}),
            'independent_passed': passes, 'visible_passed': sum(r.get('agent', {}).get('status') == 'passed' for r in rows),
            'first_candidate_independent_passed': sum(
                r['outcome'] == 'passed' and bool(r.get('agent', {}).get('attempts'))
                and r['agent']['attempts'][0]['status'] == 'passed' for r in rows),
            'usage_complete': known, 'total_tokens': tokens,
            'input_tokens': sum(u['input_tokens'] for u in totals) if known else None,
            'output_tokens': sum(u['output_tokens'] for u in totals) if known else None,
            'provider_calls': sum(u['provider_calls'] for u in totals) if known else None,
            'tokens_per_independent_success': round(tokens / passes, 2) if tokens is not None and passes else None,
            'agent_seconds': round(sum(r.get('agent_seconds', 0) for r in rows), 3),
            'tool_calls': tools, 'source_reads_including_prefetch': reads,
            'repeated_source_reads': repeated, 'repeated_source_read_fraction': repeated / reads if reads else None,
            'candidate_submissions': len(attempts), 'materialized_candidates': materialized,
            'preflight_rejections': sum(a.get('phase') == 'preflight' for a in attempts)}


def analyze(record, ledger):
    pairs = {}
    for row in record['rows']:
        pairs.setdefault((row['task'], row['repeat']), {})[row['variant']] = row
    comparable = []
    for key, pair in sorted(pairs.items()):
        ok = set(pair) == set(VARIANTS) and all(eligible(r) for r in pair.values())
        comparable.append({'task': key[0], 'repeat': key[1], 'comparable': ok,
                           'runs': {v: pair[v]['id'] for v in pair}})
    paired_rows = {v: [pairs[(p['task'], p['repeat'])][v] for p in comparable if p['comparable']]
                   for v in VARIANTS}
    paired = {v: group(paired_rows[v], ledger) for v in VARIANTS}
    common = [p for p in comparable if p['comparable'] and
              all(pairs[(p['task'], p['repeat'])][v]['outcome'] == 'passed' for v in VARIANTS)]
    common_stats = {v: group([pairs[(p['task'], p['repeat'])][v] for p in common], ledger) for v in VARIANTS}
    b, c = paired[VARIANTS[0]], paired[VARIANTS[1]]
    saving = (1 - c['total_tokens'] / b['total_tokens']
              if b['total_tokens'] and c['total_tokens'] is not None else None)
    cb, cc = common_stats[VARIANTS[0]], common_stats[VARIANTS[1]]
    common_saving = (1 - cc['total_tokens'] / cb['total_tokens']
                     if cb['total_tokens'] and cc['total_tokens'] is not None else None)
    return {'planned_runs': len(record['rows']), 'planned_pairs': len(pairs),
            'completed_runs': sum(r['state'] == 'completed' for r in record['rows']),
            'comparable_pairs': sum(p['comparable'] for p in comparable), 'pairs': comparable,
            'paired': paired, 'paired_token_reduction_fraction': saving,
            'common_passed_pairs': len(common), 'common_passed': common_stats,
            'common_passed_token_reduction_fraction': common_saving,
            'all_completed': {v: group([r for r in record['rows'] if r['variant'] == v
                                        and r['state'] == 'completed'], ledger) for v in VARIANTS},
            'row_usage': {r['id']: usage(r, ledger) for r in record['rows']},
            'stop_reason': record['stop_reason'],
            'billed_cny': None,
            'limits': 'Public development pilot with two independent bugs; repeats are not independent defects. Agent latency is not target-program speed. No diagnostic scheduling/MCP/Skills attribution.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    args = p.parse_args()
    report = analyze(json.loads((args.directory / 'evaluation.json').read_text()),
                     json.loads((args.directory / 'token-ledger.json').read_text()))
    output = args.directory / 'paired-analysis.json'
    if output.exists():
        raise FileExistsError('analysis evidence cannot be overwritten')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
