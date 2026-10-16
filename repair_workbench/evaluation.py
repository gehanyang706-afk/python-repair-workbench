"""Pinned module-level bug evaluation with evaluator-only acceptance tests."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import re
import platform
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .candidate import create_candidate
from .runner import run_tests
from .snapshot import create_snapshot, git, verify_snapshot
from .verification import assess_repair, test_outcomes
from .settings import model_configuration


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def load_dataset(root):
    root = root.resolve(strict=True)
    raw = (root / 'manifest.json').read_bytes()
    manifest = json.loads(raw)
    if manifest.get('schema_version') != 1 or not manifest.get('tasks'):
        raise ValueError('unsupported or empty dataset')
    seen = set()
    for task in manifest['tasks']:
        name = task['id']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', name) or name in seen:
            raise ValueError('invalid or duplicate task id')
        seen.add(name)
        for commit in ('base_commit', 'fix_commit'):
            if not re.fullmatch('[0-9a-f]{40}', task[commit]):
                raise ValueError('upstream commit must be pinned')
        expected = task['files']
        folder = root / name
        if folder.is_symlink():
            raise ValueError('symlink in dataset')
        actual = {}
        for path in folder.rglob('*'):
            if path.is_symlink():
                raise ValueError('symlink in dataset')
            if path.is_file():
                actual[path.relative_to(folder).as_posix()] = digest(path.read_bytes())
        if actual != expected:
            raise ValueError('dataset content does not match manifest')
        for path in expected:
            relative = PurePosixPath(path)
            if relative.is_absolute() or '..' in relative.parts or '\\' in path or relative.as_posix() != path:
                raise ValueError('invalid dataset path')
            if relative.parts[0] not in {'source', 'oracle', 'visible', 'holdout'}:
                raise ValueError('invalid dataset file role')
        from .candidate import validate_source_path
        for path in task['allowed_paths']:
            validate_source_path(path, set(task['allowed_paths']))
            if 'source/' + path not in expected or 'oracle/' + path not in expected:
                raise ValueError('missing source or oracle')
        if not any(p.startswith('visible/') for p in expected) or not any(p.startswith('holdout/') for p in expected):
            raise ValueError('visible and holdout tests required')
    return manifest, digest(raw)


def selected_tasks(manifest, names):
    available = {t['id'] for t in manifest['tasks']}
    if names and (len(names) != len(set(names)) or not set(names) <= available):
        raise ValueError('unknown or duplicate task selection')
    return [t for t in manifest['tasks'] if not names or t['id'] in names]


def prepare(root, task, destination, *, holdout=False):
    """Materialize a synthetic input commit, without oracle/holdout leakage."""
    destination.mkdir(parents=True, exist_ok=False)
    repo = destination / 'source'
    repo.mkdir()
    roles = {'source': '', 'visible': 'tests/'}
    if holdout:
        roles['holdout'] = 'tests/'
    written = set()
    for role, prefix in roles.items():
        for relative in sorted(task['files']):
            if not relative.startswith(role + '/'):
                continue
            target_name = prefix + relative[len(role) + 1:]
            if target_name in written:
                raise ValueError('colliding dataset paths')
            written.add(target_name)
            target = repo / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / task['id'] / relative).read_bytes())
    git(repo, 'init', '-q')
    git(repo, '-c', 'core.autocrlf=false', 'add', '.')
    # Fixed identity and date make the normalized input commit repeatable.
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Repair Evaluation',
        '-c', 'user.email=eval@localhost', '-c', 'commit.gpgsign=false',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', task['id']],
        env={**os.environ, 'GIT_AUTHOR_DATE': '2000-01-01T00:00:00Z',
             'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}, check=True, capture_output=True, timeout=30)
    baseline = destination / 'baseline'
    create_snapshot(repo, baseline)
    return baseline


def changes_from(root, task):
    return {p: (root / task['id'] / 'oracle' / p).read_text() for p in task['allowed_paths']}


def compare(before_dir, after_dir, before, after):
    return assess_repair(before, after,
                         test_outcomes(before_dir / 'reports/pytest.xml'),
                         test_outcomes(after_dir / 'reports/pytest.xml'))


def qualify(root, task, output, image, timeout):
    checks = {}
    for scope in ('visible', 'holdout'):
        baseline = prepare(root, task, output / scope, holdout=scope == 'holdout')
        before = run_tests(baseline, image, timeout)
        fixed = output / scope / 'oracle'
        create_candidate(baseline, fixed, changes_from(root, task), set(task['allowed_paths']))
        after = run_tests(fixed, image, timeout)
        verdict = compare(baseline, fixed, before, after)
        if scope == 'holdout':
            visible_tests = test_outcomes(output / 'visible/baseline/reports/pytest.xml')
            hidden_tests = test_outcomes(baseline / 'reports/pytest.xml')
            if not any(v == 'failed' and k not in visible_tests for k, v in hidden_tests.items()):
                verdict = {'status': 'inconclusive', 'reason': 'holdout_does_not_reproduce_bug'}
        checks[scope] = {'baseline': before, 'oracle': after, 'verdict': verdict}
    return {'qualified': all(v['verdict']['status'] == 'passed' for v in checks.values()),
            'checks': checks}


def fingerprint():
    package = Path(__file__).parent
    versions = {}
    for name in ('deepagents', 'langchain', 'langgraph', 'langchain-community', 'dashscope'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {'code': {p.name: digest(p.read_bytes()) for p in sorted(package.glob('*.py'))},
            'dependencies': versions, 'python': platform.python_version(),
            'platform': platform.platform()}


def estimate(count, calls=12):
    # Planning scenarios, not billing caps. Per-request input assumed <=128K.
    low_in, high_in = count * min(4, calls) * 8000, count * calls * 32000
    low_out, high_out = count * min(4, calls) * 1000, count * calls * 4096
    return {'tasks': count, 'max_model_calls': count * calls,
            'planning_input_tokens': [low_in, high_in], 'planning_output_tokens': [low_out, high_out],
            'planning_cny': [round((low_in*.8+low_out*2)/1e6, 4), round((high_in*.8+high_out*2)/1e6, 4)],
            'assumption': 'Beijing qwen-plus non-thinking, <=128K input/request, no cache discount; not a hard cost cap',
            'pricing_url': 'https://help.aliyun.com/zh/model-studio/qwen-plus',
            'pricing_checked': '2026-10-11'}


def summarize(rows, mode):
    completed = [r for r in rows if r.get('state') == 'completed']
    live = [r for r in rows if r.get('agent')]
    usage = {'input_tokens': 0, 'output_tokens': 0, 'reports': 0}
    for row in live:
        for key in usage:
            usage[key] += row['agent'].get('token_usage', {}).get(key, 0)
    calls = sum(r['agent'].get('model_calls', 0) for r in live)
    successes = sum(r.get('outcome') == 'passed' for r in rows)
    context_fields=('reads','returned_characters','unique_lines','repeated_reads','suppressed_reads','invalid_requests')
    context_totals={key:sum(r.get('agent',{}).get('context_stats',{}).get(key,0) for r in rows) for key in context_fields}
    candidate_reasons=Counter(a.get('reason','unknown') for r in live for a in r['agent'].get('attempts',[]) if a.get('status') != 'passed')
    blocked=any(r.get('outcome') in {'provider_account_unavailable','not_run_provider_account_unavailable'} for r in rows)
    categorized={category:{field:sum(r['agent'].get('tool_statistics',{}).get('by_category',{}).get(category,{}).get(field,0)
                 for r in live) for field in ('calls','schema_rejections')}
                 for category in ('edit','read_search','analysis','framework','unknown')}
    return {'mode': mode, 'selected': len(rows), 'completed': len(completed),
            'qualified': sum(bool(r.get('qualification', {}).get('qualified')) for r in rows),
            'visible_passed': sum(r.get('agent', {}).get('status') == 'passed' for r in rows),
            'holdout_passed': successes,
            'success_rate_all_selected': successes / len(rows) if mode == 'run' and rows and not blocked else None,
            'first_attempt_holdout_passed': sum(r.get('outcome') == 'passed' and
                r['agent']['attempts'][0].get('status') == 'passed' for r in live),
            'outcomes': dict(Counter(r.get('outcome', r['state']) for r in rows)),
            'model_calls': calls,
            'tool_calls': sum(r['agent'].get('tool_calls', 0) for r in live),
            'candidate_attempts': sum(len(r['agent'].get('attempts', [])) for r in live),
            'context_totals':context_totals, 'candidate_failure_reasons':dict(candidate_reasons),
            'patch_lines': sum(r.get('patch_lines', 0) for r in rows),
            'token_usage': usage,
            **({'tool_categories':categorized,
                'tool_category_statistics_available':all('tool_statistics' in r['agent'] for r in live),
                'usage_missing_calls':calls-usage['reports']}
               if any('tool_statistics' in r['agent'] for r in live) else {}),
            'usage_complete': all(r.get('state') == 'completed' for r in rows) and usage['reports'] == calls,
            'elapsed_seconds': round(sum(r.get('elapsed_seconds', 0) for r in rows), 3)}


def reports(output, record):
    record['summary'] = summarize(record['rows'], record['mode'])
    write_json(output / 'results.json', record)
    with (output / 'results.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['task', 'state', 'outcome', 'visible_status', 'model_calls', 'elapsed_seconds'])
        for row in record['rows']:
            writer.writerow([row['id'], row['state'], row.get('outcome', ''),
                             row.get('agent', {}).get('status', ''),
                             row.get('agent', {}).get('model_calls', 0), row.get('elapsed_seconds', 0)])
    summary = record['summary']
    lines = ['# Agent evaluation', '', f"Mode: {record['mode']}; profile: {record.get('profile','v2')}; dataset: `{record['dataset_sha256']}`",
             '', f"Selected: {summary['selected']}; completed: {summary['completed']}; qualified: {summary['qualified']}.",
             f"Visible passes: {summary['visible_passed']}; holdout passes: {summary['holdout_passed']}.",
             f"Model calls: {summary['model_calls']}; reported usage: {summary['token_usage']}.",
             f"Context: {summary['context_totals']}; rejected candidates: {summary['candidate_failure_reasons']}.",
             f"Tool categories (proposal calls / Schema rejections): {summary.get('tool_categories')}; available: {summary.get('tool_category_statistics_available',False)}.",
             '', '| Task | State | Outcome |', '| --- | --- | --- |']
    lines += [f"| {r['id']} | {r['state']} | {r.get('outcome', '')} |" for r in record['rows']]
    lines += ['', 'Qualification uses official fixes and is not Agent performance. Success rates use all selected tasks.',
              'Task scope follows the pinned dataset manifest. Report module-level subsets and repository snapshots separately; small samples do not measure general repair ability.',
              'Holdout tests are evaluator-only during execution, but public upstream bugs can have training contamination.']
    (output / 'report.md').write_text('\n'.join(lines) + '\n')


from .source_context import PROFILES


def evaluate(root, output, image, *, mode='validate', names=None, timeout=30,
             model_factory=None, model_name=None, max_calls=12, max_attempts=3, deadline=240, profile="v2"):
    if profile not in PROFILES:
        raise ValueError("unknown repair profile")
    if mode not in {'validate', 'run'} or (mode == 'run' and model_factory is None):
        raise ValueError('live evaluation requires explicit model factory')
    if not 1 <= max_calls <= 30 or not 1 <= max_attempts <= 5:
        raise ValueError('invalid call or attempt limits')
    if any(not math.isfinite(x) or x <= 0 for x in (timeout, deadline)):
        raise ValueError('timeouts must be finite and positive')
    root = root.resolve(strict=True)
    manifest, dataset_hash = load_dataset(root)
    tasks = selected_tasks(manifest, names)
    output = output.resolve()
    if output == root or root in output.parents:
        raise ValueError('evaluation output must be outside dataset')
    image_id = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                              capture_output=True, check=True, text=True, timeout=30).stdout.strip()
    output.mkdir(parents=True, exist_ok=False)
    record = {'schema_version': 1, 'mode': mode, 'profile':profile, 'dataset_sha256': dataset_hash,
              'dataset': manifest, 'image_id': image_id, 'agent_version': fingerprint(),
              'created_at': datetime.now(timezone.utc).isoformat(), 'model': model_name,
              'model_configuration': model_configuration(),
              'limits': {'calls_per_task': max_calls, 'attempts_per_task': max_attempts,
                         'deadline_per_task': deadline, 'test_timeout': timeout},
              'rows': [{'id': t['id'], 'state': 'pending'} for t in tasks]}
    reports(output, record)
    for task, row in zip(tasks, record['rows']):
        provider_blocked = False
        started = time.monotonic()
        row['state'] = 'running'
        reports(output, record)
        case = output / task['id']
        try:
            # Recheck before every task. No paid call for invalid fixture or environment.
            if load_dataset(root)[1] != dataset_hash:
                raise ValueError('dataset changed during evaluation')
            qualification = qualify(root, task, case / 'qualification', image_id, timeout)
            row['qualification'] = qualification
            if not qualification['qualified']:
                row['outcome'] = 'invalid_task'
            elif mode == 'validate':
                row['outcome'] = 'qualified'
            else:
                from .agent import run_agent
                from .session import RepairSession
                baseline = case / 'qualification/visible/baseline'
                session = RepairSession(baseline, case / 'agent', set(task['allowed_paths']),
                    max_attempts=max_attempts, max_model_calls=max_calls,
                    deadline_seconds=deadline, test_timeout=timeout, profile=profile)
                result = run_agent(session, model_factory(), task['task'])
                row['agent'] = result
                row['outcome'] = result.get('stop_reason') or result.get('reason', 'no_candidate')
                provider_blocked = row['outcome'] == 'provider_account_unavailable'
                if result['status'] == 'passed':
                    attempt = next(a for a in result['attempts'] if a['status'] == 'passed')
                    candidate = case / 'agent' / f"attempt-{attempt['number']:02d}" / 'verification'
                    verify_snapshot(candidate)
                    changes = {p: (candidate / 'workspace' / p).read_text() for p in task['allowed_paths']}
                    hidden_base = case / 'qualification/holdout/baseline'
                    hidden = case / 'holdout-verification'
                    create_candidate(hidden_base, hidden, changes, set(task['allowed_paths']))
                    after = run_tests(hidden, image_id, timeout)
                    before = qualification['checks']['holdout']['baseline']
                    verdict = compare(hidden_base, hidden, before, after)
                    row['holdout'] = verdict
                    row['outcome'] = 'passed' if verdict['status'] == 'passed' else 'holdout_' + verdict['reason']
                    patch = (candidate / 'patch.diff').read_text()
                    row['patch_lines'] = len(patch.splitlines())
            row['state'] = 'completed'
        except Exception as exc:
            row.update(state='completed', outcome='evaluation_error', error_type=type(exc).__name__)
            # Retain usage evidence if a later verification step failed.
            saved = case / 'agent/result.json'
            if saved.exists():
                row['agent'] = json.loads(saved.read_text())
        finally:
            row['elapsed_seconds'] = round(time.monotonic() - started, 3)
            reports(output, record)
        if provider_blocked:
            record['interrupted_reason'] = 'provider_account_unavailable'
            for remaining in record['rows']:
                if remaining['state'] == 'pending':
                    remaining.update(state='not_run', outcome='not_run_provider_account_unavailable', elapsed_seconds=0)
            reports(output, record)
            break
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['estimate', 'validate', 'run'])
    parser.add_argument('--dataset', type=Path, default=Path('benchmarks/real-bugs-v1'))
    parser.add_argument('--case', action='append')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--image', default='study-agent-deepagents:0.1')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--profile', choices=PROFILES, default='observe')
    parser.add_argument('--max-model-calls', type=int, default=12)
    parser.add_argument('--max-attempts', type=int, default=3)
    parser.add_argument('--deadline', type=float, default=240)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.max_model_calls <= 30:
        parser.error('max-model-calls must be 1–30')
    if args.mode == 'estimate':
        manifest, _ = load_dataset(args.dataset)
        print(json.dumps(estimate(len(selected_tasks(manifest, args.case)), args.max_model_calls), ensure_ascii=False, indent=2))
        return 0
    if args.output is None:
        parser.error('--output is required; existing directories are never reused')
    factory, model = None, None
    if args.mode == 'run':
        from .settings import ModelSettings
        settings = ModelSettings.load(args.env_file, args.model)
        factory, model = settings.create_model, settings.model
    result = evaluate(args.dataset, args.output, args.image, mode=args.mode, names=args.case,
                      timeout=args.timeout, model_factory=factory, model_name=model,
                      max_calls=args.max_model_calls, max_attempts=args.max_attempts, deadline=args.deadline, profile=args.profile)
    print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
    return 0 if all(r.get('outcome') in {'qualified', 'passed'} for r in result['rows']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
