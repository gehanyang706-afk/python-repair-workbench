"""Frozen public-bug pilot with persistent, pessimistic provider reservations."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import time


from .agent import run_agent
from .candidate import create_candidate
from .diagnostic_evaluation import source_fingerprint, _image_id
from .evaluation import load_dataset, write_json, digest, compare
from .evaluation_budget import EvaluationLedger
from .evaluation_model import MeteredEvaluationTongyi
from .runner import run_tests
from .session import RepairSession, BudgetExceeded
from .snapshot import verify_snapshot

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'benchmarks/bugsinpy-derived-v1'
CONTEXT_RESERVATION = 30720 + 4096
LIMITS = {'batch_tokens': 300000, 'run_tokens': 100000, 'requests_per_run': 12,
          'attempts_per_run': 3, 'run_seconds': 240, 'batch_seconds': 3600,
          'test_seconds': 30, 'request_seconds': 45, 'output_tokens': 4096}


class TokenLedger(EvaluationLedger):
    """Historical pilot defaults; new experiments pass explicit provider bounds."""
    def __init__(self, path, plan_hash, limits=None):
        super().__init__(path, plan_hash, dict(limits or LIMITS),
                         input_token_limit=30720, output_token_limit=4096)


class MeteredTongyi(MeteredEvaluationTongyi):
    """Compatibility name for the archived public-pilot protocol."""


class PilotSession(RepairSession):
    """Identical profile/prompt/catalog; intervention is the initial packet only."""
    def __init__(self, *args, variant, **kwargs):
        self.variant = variant
        super().__init__(*args, profile='simple_context', **kwargs)

    def initial_context(self):
        if self.variant == 'failure_context':
            return super().initial_context()
        if self.variant != 'on_demand':
            raise ValueError('unknown pilot variant')
        self.record('initial_context', characters=0, excerpt_count=0,
                    policy='on_demand; same tools and common prompt')
        return {'scope': 'Use the same source navigation and test-log tools on demand.'}


def hash_json(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def make_plan(qualification):
    manifest, dataset_hash = load_dataset(DATASET)
    qualification = Path(qualification).resolve(strict=True)
    report = json.loads((qualification / 'results.json').read_text())
    if (report.get('dataset_sha256') != dataset_hash
            or {r['id'] for r in report['rows']} != {t['id'] for t in manifest['tasks']}
            or len(report['rows']) != 2 or
            any(not r.get('qualification', {}).get('qualified') for r in report['rows'])):
        raise ValueError('both tasks must qualify before inference')
    for task in manifest['tasks']:
        for scope in ('visible', 'holdout'):
            base = qualification / task['id'] / 'qualification' / scope / 'baseline'
            verify_snapshot(base)
    schedule = []
    for repeat in range(3):
        for index, task in enumerate(manifest['tasks']):
            order = ['on_demand', 'failure_context']
            if (repeat + index) % 2:
                order.reverse()
            for variant in order:
                schedule.append({'task': task['id'], 'repeat': repeat + 1, 'variant': variant,
                                 'id': '%s-r%d-%s' % (task['id'], repeat + 1, variant)})
    plan = {'schema_version': 1, 'model': 'qwen-max', 'model_alias_is_rolling': True,
            'provider': 'DashScope Beijing native Generation API',
            'extra_paid_cny_cap': 0, 'free_only_confirmed_by_user': True,
            'sampling_temperature': 0, 'retries': 0, 'limits': LIMITS,
            'qualification': str(qualification),
            'qualification_evidence': {str(p.relative_to(qualification)): digest(p.read_bytes())
                for p in sorted(qualification.rglob('*')) if p.is_file()},
            'dataset_hash': dataset_hash, 'source': source_fingerprint(),
            'schedule': schedule,
            'comparison': 'Same simple_context profile, prompt, tools, exact-text edits and verification; initial failure-focused context vs on-demand context only.',
            'scope': '2 public BugsInPy-derived development bugs; 3 repeated runs each. Not 6 independent bugs, official score, generalization or MCP/diagnostic-scheduling ablation.',
            'stop': ['free quota/account error', 'any unknown usage', 'batch token limit',
                     'cancel', 'source/dataset/qualification drift', 'unconfirmed cleanup']}
    return {**plan, 'plan_hash': hash_json(plan)}


def validate_plan(plan):
    if plan != make_plan(plan['qualification']):
        raise ValueError('frozen plan or evidence changed')


def summarize(rows, ledger):
    variants = {}
    for variant in ('on_demand', 'failure_context'):
        group = [r for r in rows if r['variant'] == variant]
        calls = [c for c in ledger.state['calls']
                 if c['run_id'] in {r['id'] for r in group}]
        complete = all(c['state'] == 'settled' for c in calls)
        variants[variant] = {
            'planned_runs': len(group), 'completed_runs': sum(r['state'] == 'completed' for r in group),
            'independent_passed': sum(r.get('outcome') == 'passed' for r in group),
            'outcomes': dict(Counter(r['outcome'] for r in group)),
            'provider_calls': len(calls), 'usage_complete': complete,
            'input_tokens': sum(c['input_tokens'] for c in calls) if complete else None,
            'output_tokens': sum(c['output_tokens'] for c in calls) if complete else None,
            'total_tokens': sum(c['charged_tokens'] for c in calls) if complete else None,
            'agent_seconds': round(sum(r.get('agent_seconds', 0) for r in group), 3),
            'tool_calls': sum(r.get('agent', {}).get('tool_calls', 0) for r in group),
            'repeated_source_reads': sum(r.get('agent', {}).get('context_stats', {}).get('repeated_reads', 0) for r in group)}
    return {'variants': variants, 'charged_or_reserved_tokens': ledger.charged(),
            'reserved_unknown_calls': sum(c['state'] != 'settled' for c in ledger.state['calls']),
            'provider_calls': len(ledger.state['calls'])}


def run(plan, output, env_file):
    validate_plan(plan)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    lock = (output / 'owner.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    from dotenv import dotenv_values
    config = dotenv_values(env_file)
    if config.get('DASHSCOPE_BASE_URL') != 'https://dashscope.aliyuncs.com/compatible-mode/v1':
        raise ValueError('only the approved Beijing account configuration is allowed')
    key = config.get('DASHSCOPE_API_KEY')
    if not key:
        raise ValueError('approved provider configuration has no key')
    ledger = TokenLedger(output / 'token-ledger.json', plan['plan_hash'])
    manifest, _ = load_dataset(DATASET)
    tasks = {t['id']: t for t in manifest['tasks']}
    rows = [{**s, 'state': 'not_run', 'outcome': 'not_run'} for s in plan['schedule']]
    record = {'plan': plan, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'rows': rows, 'stop_reason': None}
    write_json(output / 'plan.json', plan)
    started = time.monotonic()

    def persist():
        record['summary'] = summarize(rows, ledger)
        write_json(output / 'evaluation.json', record)

    def check():
        if time.monotonic() - started >= LIMITS['batch_seconds']:
            raise BudgetExceeded('batch_deadline')
        validate_plan(plan)

    persist()
    try:
        for row in rows:
            check()
            task = tasks[row['task']]
            case = output / row['id']
            case.mkdir()
            traces = case / 'provider-traces'
            traces.mkdir()
            q = Path(plan['qualification']) / task['id'] / 'qualification'
            baseline = q / 'visible/baseline'
            image = json.loads((baseline / 'reports/result.json').read_text())['image_id']
            if _image_id(image) != image:
                raise ValueError('image drift')
            session = PilotSession(baseline, case / 'agent', set(task['allowed_paths']),
                variant=row['variant'], max_attempts=LIMITS['attempts_per_run'],
                max_model_calls=LIMITS['requests_per_run'], deadline_seconds=LIMITS['run_seconds'],
                test_timeout=LIMITS['test_seconds'])
            model = MeteredTongyi(model_name=plan['model'], api_key=key, ledger=ledger,
                run_id=row['id'], trace_dir=traces, validate_frozen=check, cache=False,
                sampling_temperature=0, max_output_tokens=4096,
                call_timeout=45, read_timeout=40, connect_timeout=5)
            row.update(state='running', outcome='running')
            persist()
            tick = time.monotonic()
            result = run_agent(session, model, task['task'])
            row.update(agent=result, agent_seconds=round(time.monotonic() - tick, 3),
                       outcome=result['stop_reason'], state='completed')
            if result['status'] == 'passed':
                attempt = next(a for a in result['attempts'] if a['status'] == 'passed')
                candidate = session.output / ('attempt-%02d' % attempt['number']) / 'verification'
                verify_snapshot(candidate)
                changes = {p: (candidate / 'workspace' / p).read_text() for p in task['allowed_paths']}
                hidden = case / 'independent-verification'
                hidden_base = q / 'holdout/baseline'
                create_candidate(hidden_base, hidden, changes, set(task['allowed_paths']))
                after = run_tests(hidden, image, LIMITS['test_seconds'])
                before = json.loads((hidden_base / 'reports/result.json').read_text())
                row['holdout'] = compare(hidden_base, hidden, before, after)
                row['outcome'] = 'passed' if row['holdout']['status'] == 'passed' else 'independent_failed'
            persist()
            # All original and reconstructed runs must confirm their own cleanup.
            if any(not json.loads(p.read_text()).get('cleanup_ok')
                   for p in case.rglob('reports/result.json')):
                raise BudgetExceeded('container_cleanup_unconfirmed')
            if ledger.state['blocked']:
                raise BudgetExceeded(ledger.state['blocked'])
            if result['stop_reason'] in {'cancel_requested', 'batch_token_limit', 'batch_deadline'}:
                raise BudgetExceeded(result['stop_reason'])
    except BaseException as exc:
        record['stop_reason'] = str(exc) if isinstance(exc, BudgetExceeded) else type(exc).__name__
        for row in rows:
            if row['state'] == 'running':
                row.update(state='error', outcome='evaluation_error')
        persist()
        if not isinstance(exc, (Exception, KeyboardInterrupt)):
            raise
    finally:
        record['completed_utc'] = datetime.now(timezone.utc).isoformat()
        persist()
        lock.close()
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['plan', 'run'])
    p.add_argument('--qualification', type=Path)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--env-file', type=Path)
    args = p.parse_args()
    if args.mode == 'plan':
        if args.plan.exists() or not args.qualification:
            p.error('new plan path and qualification are required')
        write_json(args.plan, make_plan(args.qualification))
        print(json.dumps({'plan_hash': json.loads(args.plan.read_text())['plan_hash'],
                          'planned_runs': 12, 'batch_tokens': 300000}))
    else:
        if not args.output or not args.env_file:
            p.error('output and approved configuration path are required')
        result = run(json.loads(args.plan.read_text()), args.output, args.env_file)
        print(json.dumps({'stop_reason': result['stop_reason'], **result['summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
