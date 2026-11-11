"""Audit archived F0/A1 runs without using a provider or re-running a task."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_workbench.evaluation_budget import _atomic_write
from repair_workbench.experiment_manifest import _validated_structure


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _integer(value):
    return type(value) is int and value >= 0


def audit(directory, expected_sha256):
    """Separate outcomes, provider receipts, physical probes and tool proposals.

    Verifies archived identity and denominators. It does not authenticate an
    arbitrary directory, recheck the live source, or prove provider calls were
    real just because a ledger entry says settled.
    """
    root = Path(directory).resolve(strict=True)
    artifacts = {}
    def load(relative, *, optional=False, lines=False):
        path = root / relative
        if optional and not path.exists():
            return None
        _require(path.is_file() and path.resolve() == path and path.stat().st_size <= 64 * 1024 * 1024,
                 'missing_or_unsafe_audit_artifact')
        raw = path.read_bytes()
        artifacts[str(relative)] = hashlib.sha256(raw).hexdigest()
        if lines:
            return [json.loads(line) for line in raw.splitlines() if line]
        return json.loads(raw)

    plan = _validated_structure(load('plan.json'))
    evaluation, ledger = load('evaluation.json'), load('token-ledger.json')
    _require(plan['manifest_sha256'] == evaluation['manifest_sha256'] == ledger['plan_hash'] == expected_sha256,
             'archive_plan_binding_mismatch')
    variants = {item['policy']: item['id'] for item in plan['variants']}
    _require(set(variants) == {'fixed', 'adaptive'} and len(plan['variants']) == 2,
             'repository_audit_requires_fixed_and_adaptive')
    rows = evaluation['rows']
    _require(len(rows) == len(plan['schedule']) and all(
        {k: actual.get(k) for k in planned} == planned for actual, planned in zip(rows, plan['schedule'])),
        'archived_rows_do_not_match_frozen_schedule')
    by_run = {row['run_id']: [] for row in rows}
    for index, call in enumerate(ledger['calls'], 1):
        _require(call.get('index') == index and call.get('run_id') in by_run
                 and call.get('state') in ('settled', 'usage_unknown', 'reserved') and _integer(call.get('charged_tokens')),
                 'foreign_or_invalid_ledger_entry')
        if call['state'] == 'settled':
            _require(_integer(call.get('input_tokens')) and _integer(call.get('output_tokens'))
                     and call['input_tokens'] + call['output_tokens'] == call['charged_tokens'], 'settled_usage_mismatch')
        else:
            _require(call.get('input_tokens') is None and call.get('output_tokens') is None,
                     'unknown_usage_cannot_be_reported_as_actual_tokens')
        by_run[call['run_id']].append(call)
    _require(evaluation['budget']['calls'] == len(ledger['calls'])
             and evaluation['budget']['charged_or_reserved_tokens'] == sum(c['charged_tokens'] for c in ledger['calls'])
             and evaluation['budget']['usage_complete'] == all(c['state'] == 'settled' for c in ledger['calls']),
             'batch_budget_summary_does_not_match_ledger')

    audited, pairs = [], {}
    for row in rows:
        run = row['run_id']
        _require(row['state'] in ('completed', 'not_run', 'stopped', 'error', 'running'), 'invalid_run_state')
        calls = by_run[run]
        _require(row['state'] != 'not_run' or not calls, 'unstarted_row_has_model_reservations')
        settled = [c for c in calls if c['state'] == 'settled']
        detail = {'task_id': row['task_id'], 'variant_id': row['variant_id'], 'repeat': row['repeat'],
                  'run_id': run, 'state': row['state'], 'outcome': row['outcome'], 'seconds': row.get('seconds'),
                  'ledger_entries': len(calls), 'settled_entries': len(settled),
                  'known_input_tokens': sum(c['input_tokens'] for c in settled),
                  'known_output_tokens': sum(c['output_tokens'] for c in settled),
                  'unknown_or_reserved_entries': len(calls) - len(settled),
                  'unknown_or_reserved_tokens': sum(c['charged_tokens'] for c in calls if c['state'] != 'settled'),
                  'agent_tools': None}
        prefix = Path(run) / 'repository'
        execution = load(prefix / 'execution.json', optional=True)
        agent = load(prefix / 'agent/result.json', optional=True)
        if row['state'] == 'completed' and row['outcome'] == 'passed':
            _require(execution is not None and execution.get('status') == 'passed'
                     and execution.get('cleanup_ok') is True
                     and execution.get('agent_finished_before_final_evaluation') is True
                     and execution.get('independent_evaluation_started') is True,
                     'passed_row_has_no_independent_acceptance')
            verdict = load(prefix / 'independent-evaluation/result.json')
            _require(verdict.get('status') == 'passed' and verdict.get('verdict', {}).get('status') == 'passed',
                     'passed_row_disagrees_with_raw_verdict')
        if agent is not None:
            events = load(prefix / 'agent/events.jsonl', lines=True)
            probes = [e for e in events if e.get('event') == 'repository_probe_finished']
            model = [e for e in events if e.get('event') == 'model_finished']
            detail['agent_tools'] = {'charged_root_calls': agent['tool_calls'],
                'model_proposals': sum(len(e.get('tools', [])) + e.get('invalid_tool_call_count', 0) for e in model),
                'schema_rejections': agent.get('tool_statistics', {}).get('schema_rejections'),
                'recorded_probe_dispatches': sum(e.get('dispatched') is True for e in probes),
                'recorded_probe_cache_hits': sum(e.get('cache_hit') is True for e in probes),
                'physical_probe_budget_used': agent.get('diagnostic_probe_runs')}
        audited.append(detail)
        pairs.setdefault((row['task_id'], row['repeat']), {})[row['variant_id']] = detail

    summary = {}
    for policy, variant in variants.items():
        group = [r for r in audited if r['variant_id'] == variant]
        complete = [r for r in group if r['state'] == 'completed']
        passed = sum(r['outcome'] == 'passed' for r in complete)
        full = len(complete) == len(group) and evaluation['stop_reason'] is None
        summary[policy] = {'planned': len(group), 'completed': len(complete),
            'not_run': sum(r['state'] == 'not_run' for r in group),
            'stopped_or_error': sum(r['state'] in ('stopped', 'error', 'running') for r in group),
            'passed': passed, 'failed': sum(r['outcome'] == 'failed' for r in complete),
            'inconclusive': sum(r['outcome'] == 'inconclusive' for r in complete),
            'joint_pass_rate': passed / len(group) if full and group else None,
            'known_input_tokens': sum(r['known_input_tokens'] for r in group),
            'known_output_tokens': sum(r['known_output_tokens'] for r in group),
            'unknown_or_reserved_tokens': sum(r['unknown_or_reserved_tokens'] for r in group),
            'tool_artifacts_present': sum(r['agent_tools'] is not None for r in group)}
    paired = []
    for (task, repeat), pair in sorted(pairs.items()):
        fixed, adaptive = pair[variants['fixed']], pair[variants['adaptive']]
        comparable_usage = all(r['state'] == 'completed' and r['settled_entries'] > 0
                               and not r['unknown_or_reserved_entries'] for r in (fixed, adaptive))
        known_total = lambda r: r['known_input_tokens'] + r['known_output_tokens']
        paired.append({'task_id': task, 'repeat': repeat, 'fixed_outcome': fixed['outcome'],
            'adaptive_outcome': adaptive['outcome'],
            'adaptive_minus_fixed_known_tokens': known_total(adaptive) - known_total(fixed) if comparable_usage else None})
    return {'manifest_sha256': expected_sha256, 'distinct_tasks': len(plan['tasks']),
        'planned_pairs': len(paired), 'stop_reason': evaluation['stop_reason'], 'variants': summary,
        'rows': audited, 'pairs': paired, 'artifact_sha256': artifacts,
        'scope': 'Archived receipts only; synthetic versus real provider provenance must be established separately.',
        'unnecessary_modification_rate': None, 'composite_invalid_exploration_rate': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    _atomic_write(args.output, audit(args.batch, args.manifest_sha256), create=True)


if __name__ == '__main__':
    main()
