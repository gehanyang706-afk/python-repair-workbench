"""Frozen, serial diagnostic development experiments with an offline policy.

This entry point never loads a provider model. Public development fixtures and
scripted decisions exercise the harness; they do not measure LLM performance.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import subprocess
import time

from .runner import run_tests
from .session import BudgetExceeded
from .snapshot import create_snapshot, snapshot_id, verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = 'ugc-diagnostics-dev-v1'
DATASET_SHA256 = '2302915b4d2fa0951b22fa9347eb0076c268c6191736ab84588b98249c1f7e14'
POLICY_ID = 'diagnostic_probe_policy_v1'
TASK_TEXT = ('诊断当前地图导出工具的资源使用，保持输出顺序、首个重复资源、缺失异常和输入不变。'
             '根据真实工具回执选择诊断；当前脚本仅验证执行链路，最后提出证据不足结论，不生成补丁。')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _check_git_environment():
    # -C does not override GIT_DIR/WORK_TREE/INDEX or injected Git configuration.
    # Reject before mutations, including later snapshot calls that inherit env.
    harmless = {'GIT_PAGER', 'GIT_TERMINAL_PROMPT'}
    _require(not any(key.startswith('GIT_') and key not in harmless for key in os.environ),
             'unsupported_git_environment')


def _json(path):
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 20 * 1024 * 1024,
             'missing_or_invalid_json_artifact')
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite_json_number')
    return json.loads(_regular(path).decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)


def _regular(path):
    _require(not any(item.is_symlink() for item in (path, *path.parents)), 'symlinked_evaluation_path')
    _require(path.is_file(), 'missing_evaluation_file')
    return path.read_bytes()


def load_dataset(path):
    """Accept only byte-identical copies of the reviewed public registry."""
    path = Path(path).absolute()
    raw = _regular(path / 'manifest.json')
    _require(hashlib.sha256(raw).hexdigest() == DATASET_SHA256, 'unregistered_diagnostic_dataset')
    manifest = _json(path / 'manifest.json')
    _require(manifest['dataset_id'] == DATASET_ID and manifest['split'] == 'dev'
             and manifest['synthetic'] is True and manifest['independent_evaluation'] is False,
             'unexpected_dataset_scope')
    for task in manifest['tasks']:
        directory = path / task['source_dir']
        actual = {}
        _require(directory.is_dir(), 'missing_task_source')
        for entry in directory.rglob('*'):
            _require(not entry.is_symlink(), 'symlinked_evaluation_path')
            if entry.is_file():
                actual[entry.relative_to(directory).as_posix()] = hashlib.sha256(_regular(entry)).hexdigest()
        _require(actual == task['files'], 'diagnostic_task_content_changed')
    return manifest, DATASET_SHA256


def source_fingerprint():
    """Include uncommitted implementation and fixed assets, never runtime secrets."""
    paths = [*ROOT.glob('repair_workbench/*.py'), *ROOT.glob('repair_workbench/skills/*/SKILL.md'),
             *ROOT.glob('requirements*.lock'), ROOT / 'pyproject.toml',
             ROOT / 'repair_workbench/swebench_harness_lock.json',
             ROOT / '.github/workflows/offline-validation.yml',
             ROOT / 'scripts/audit_diagnostic_run.py', ROOT / 'scripts/check_delivery.py',
             ROOT / 'scripts/qualify_diagnostic_probes.py',
             ROOT / 'benchmarks/ugc-diagnostics-dev-v1/qualification.py']
    code = {path.relative_to(ROOT).as_posix(): hashlib.sha256(_regular(path)).hexdigest()
            for path in sorted(set(paths))}
    dependencies = {}
    for distribution in importlib.metadata.distributions():
        name = re.sub(r'[-_.]+', '-', distribution.metadata['Name'].lower())
        versions = dependencies.setdefault(name, [])
        if distribution.version not in versions:
            versions.append(distribution.version)
    dependencies = {name: sorted(versions) for name, versions in sorted(dependencies.items())}
    return {'files': code, 'dependencies': dependencies, 'python': platform.python_version(),
            'platform': platform.platform()}


def _image_id(image):
    _require(isinstance(image, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', image),
             'invalid_local_image')
    result = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                            capture_output=True, text=True, check=True, timeout=15)
    identity = result.stdout.strip()
    _require(re.fullmatch(r'sha256:[a-f0-9]{64}', identity), 'invalid_image_identity')
    return identity


def _limits(repeats, seed, transport, common, batch_timeout):
    _require(type(repeats) is int and 1 <= repeats <= 3, 'invalid_repeats')
    _require(type(seed) is int and 0 <= seed <= 2**32 - 1, 'invalid_seed')
    _require(isinstance(transport, str) and transport in {'local', 'mcp'}, 'invalid_transport')
    for name, low, high in (('max_model_calls', 1, 30), ('max_attempts', 1, 5), ('max_probe_runs', 3, 12)):
        _require(type(common[name]) is int and low <= common[name] <= high, 'invalid_' + name)
    for name, high in (('deadline_seconds', 600), ('test_timeout', 120)):
        value = common[name]
        _require(type(value) in {int, float} and math.isfinite(value) and 0 < value <= high,
                 'invalid_' + name)
    _require(type(batch_timeout) in {int, float} and math.isfinite(batch_timeout)
             and 0 < batch_timeout <= 3600, 'invalid_batch_timeout')


def _plan(manifest, source, image, tasks, repeats, seed, transport, common, batch_timeout):
    _limits(repeats, seed, transport, common, batch_timeout)
    available = [task['id'] for task in manifest['tasks']]
    _require(isinstance(tasks, list) and tasks and all(isinstance(task, str) for task in tasks)
             and len(tasks) == len(set(tasks)) and set(tasks) <= set(available), 'invalid_task_selection')
    tasks = [task for task in available if task in tasks]
    _require(re.fullmatch(r'sha256:[a-f0-9]{64}', image), 'invalid_image_identity')
    rng = random.Random(seed)
    blocks = [(task, repeat) for task in tasks for repeat in range(1, repeats + 1)]
    rng.shuffle(blocks)
    schedule = []
    for task, repeat in blocks:
        order = ['adaptive', 'fixed']
        rng.shuffle(order)
        schedule.extend({'run_id': f'{task}-r{repeat:02d}-{condition}', 'task_id': task,
                         'repeat': repeat, 'condition': condition} for condition in order)
    plan = {'schema_version': 1, 'mode': 'scripted_offline', 'policy_id': POLICY_ID,
            'dataset': {'id': DATASET_ID, 'sha256': DATASET_SHA256, 'split': 'dev',
                        'synthetic': True, 'public': True, 'independent_evaluation': False},
            'source': source, 'image_id': image, 'tasks': tasks, 'repeats': repeats, 'seed': seed,
            'conditions': [{'id': name, 'strategy': name} for name in ('adaptive', 'fixed')],
            'common': {**common, 'workload': 'ugc-export-v1', 'diagnostic_transport': transport,
                       'use_skills': False, 'use_investigator': False,
                       'minimum_speedup': 0.15, 'max_memory_ratio': 1.25},
            'task_text': TASK_TEXT, 'schedule': schedule, 'batch_timeout_seconds': batch_timeout,
            'ceilings': {'scheduled_runs': len(schedule),
                         'model_wrapper_calls': len(schedule) * common['max_model_calls'],
                         'tools_per_run': 60, 'baseline_test_runs': len(tasks),
                         'exploratory_probe_runs': len(schedule) * common['max_probe_runs'],
                         'candidate_verification_probe_runs': len(schedule) * common['max_attempts'] * 8},
            'scoring_contract': 'latency_improvement_with_memory_guard_v1',
            'stop_policy': 'Cancel, drift, cleanup or audit failure stops the batch; local run budgets remain separate outcomes.',
            'claim_scope': 'Public synthetic development harness only; no LLM, cost or health-decision accuracy claims.'}
    plan['plan_sha256'] = _digest(plan)
    return plan


def create_plan(dataset, image, *, tasks=None, repeats=1, seed=0, transport='local',
                max_model_calls=12, max_attempts=3, max_probe_runs=6,
                deadline_seconds=180, test_timeout=30, batch_timeout_seconds=900):
    _check_git_environment()
    common = dict(max_model_calls=max_model_calls, max_attempts=max_attempts, max_probe_runs=max_probe_runs,
                  deadline_seconds=deadline_seconds, test_timeout=test_timeout)
    _limits(repeats, seed, transport, common, batch_timeout_seconds)
    manifest, _ = load_dataset(dataset)
    selected = [task['id'] for task in manifest['tasks']] if tasks is None else tasks
    _require(isinstance(selected, list) and selected and all(isinstance(item, str) for item in selected)
             and len(selected) == len(set(selected))
             and set(selected) <= {task['id'] for task in manifest['tasks']}, 'invalid_task_selection')
    return _plan(manifest, source_fingerprint(), _image_id(image), selected, repeats, seed,
                 transport, common, batch_timeout_seconds)


def validate_plan(plan, dataset):
    _check_git_environment()
    _require(isinstance(plan, dict), 'invalid_diagnostic_plan')
    _require(plan.get('plan_sha256') == _digest({key: value for key, value in plan.items() if key != 'plan_sha256'}),
             'diagnostic_plan_hash_mismatch')
    manifest, _ = load_dataset(dataset)
    _require(plan.get('source') == source_fingerprint(), 'diagnostic_source_drift')
    common = plan.get('common', {})
    try:
        settings = {name: common[name] for name in ('max_model_calls', 'max_attempts', 'max_probe_runs',
                                                   'deadline_seconds', 'test_timeout')}
        rebuilt = _plan(manifest, plan['source'], plan['image_id'], plan['tasks'], plan['repeats'],
                        plan['seed'], common['diagnostic_transport'], settings, plan['batch_timeout_seconds'])
    except (KeyError, TypeError):
        raise ValueError('invalid_diagnostic_plan') from None
    _require(plan == rebuilt, 'unsupported_diagnostic_plan_change')


def _output(path, dataset):
    path = Path(path).absolute()
    _require(not any(part.is_symlink() for part in (path, *path.parents)), 'symlinked_output_path')
    path = path.resolve()
    for protected in (Path(dataset).resolve(), ROOT / 'repair_workbench', ROOT / 'benchmarks', ROOT / 'scripts'):
        _require(path != protected and protected not in path.parents, 'output_inside_evaluation_source')
    _require(not path.exists(), 'evaluation_output_exists')
    return path


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def prepare_task(dataset, task, destination):
    _check_git_environment()
    manifest, _ = load_dataset(dataset)
    _require(task in manifest['tasks'], 'unregistered_task_contract')
    destination = _output(destination, dataset)
    repo = destination / 'source'
    repo.mkdir(parents=True)
    for relative in task['files']:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_regular(Path(dataset).absolute() / task['source_dir'] / relative))
    commands = [('init', '-q'), ('add', '--all'), ('commit', '-qm', task['id'])]
    env = {**os.environ, 'GIT_AUTHOR_DATE': '2000-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}
    for args in commands:
        subprocess.run(['git', '-C', str(repo), '-c', 'core.autocrlf=false', '-c', 'core.excludesFile=/dev/null',
                        '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=Diagnostic Evaluation', '-c', 'user.email=eval@example.invalid', *args],
                       env=env, capture_output=True, check=True, timeout=30)
    baseline = destination / 'baseline'
    create_snapshot(repo, baseline)
    _require({item['path']: item['sha256'] for item in verify_snapshot(baseline)['files']} == task['files'],
             'prepared_snapshot_file_mismatch')
    return baseline


class _BatchControl:
    def __init__(self, output, seconds):
        self.output, self.deadline = output, time.monotonic() + seconds
        self.resources, self.scope = {}, 'preflight'

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded('diagnostic_batch_deadline')

    def container(self, name, state):
        _require((state == 'reserved' and name not in self.resources)
                 or (state == 'cleaned' and self.resources.get(name) == 'reserved'), 'invalid_batch_container_lifecycle')
        self.resources[name] = state
        with (self.output / 'containers.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'scope': self.scope, 'name': name, 'state': state}) + '\n')


def _audit_module():
    path = ROOT / 'scripts/audit_diagnostic_run.py'
    spec = importlib.util.spec_from_file_location('diagnostic_batch_trusted_auditor', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_baseline(baseline, task, image):
    manifest = verify_snapshot(baseline)
    _require({item['path']: item['sha256'] for item in manifest['files']} == task['files'], 'batch_baseline_content_mismatch')
    tests = _json(baseline / 'reports/result.json')
    _require(tests.get('snapshot_id') == snapshot_id(manifest) and tests.get('image_id') == image
             and tests.get('status') == 'tests_passed' and tests.get('cleanup_ok') is True,
             'batch_baseline_not_qualified')
    for name in ('pytest.xml', 'pytest.log'):
        _regular(baseline / 'reports' / name)
    return snapshot_id(manifest)


def _check_probe(plan, directory, baseline_id):
    from .diagnostics import _validated_report, get_workload
    report = _json(directory / 'result.json')
    spec = get_workload(plan['common']['workload'])
    _require(report.get('image_id') == plan['image_id'] and report.get('snapshot_id') == baseline_id,
             'run_probe_identity_mismatch')
    _require(report.get('workload_id') == spec.id and report.get('workload_fingerprint') == spec.fingerprint
             and report.get('harness_sha256') == plan['source']['files']['repair_workbench/probe_worker.py']
             and report.get('probe') in {'cpu', 'memory', 'benchmark'}, 'run_probe_workload_or_harness_mismatch')
    _require(report.get('status') == 'ok' and report.get('cleanup_ok') is True
             and report.get('summary', {}).get('semantic_ok') is True, 'run_probe_execution_invalid')
    checked = _validated_report(directory / 'probe.json', report['probe'], spec)
    _require(all(report.get(key) == value for key, value in checked.items()), 'run_probe_measurement_mismatch')
    names = ['probe.log', 'probe.json', 'result.json']
    if report['probe'] == 'cpu':
        names += ['cpu-small.pstats', 'cpu-large.pstats']
    for name in names:
        _regular(directory / name)
    return names


def _verify_run(plan, row, directory, baseline, task):
    root = directory / 'agent'
    baseline_id = _check_baseline(baseline, task, plan['image_id'])
    provenance = _json(directory / 'provenance.json')
    _require(provenance.get('mode') == 'scripted_offline' and provenance.get('real_model_calls') == 0
             and provenance.get('plan_sha256') == plan['plan_sha256']
             and provenance.get('run_id') == row['run_id'], 'run_provenance_binding_mismatch')
    audit = _audit_module().audit_run(root, directory / 'provenance.json')
    result = _json(root / 'result.json')
    common = plan['common']
    options = result.get('diagnostic_options')
    # Historical receipts predate reproduction tools. Missing means disabled;
    # an enabled/new mechanism must never enter the fixed offline comparator.
    options_disabled = (type(options) is dict
        and set(options) in ({'skills', 'investigator'}, {'skills', 'investigator', 'reproduction'})
        and all(value is False for value in options.values()))
    _require(result.get('baseline_id') == baseline_id and result.get('diagnostic_strategy') == row['condition']
             and result.get('diagnostic_transport') == common['diagnostic_transport']
             and result.get('diagnostic_workload') == common['workload']
             and options_disabled
             and result.get('allowed_paths') == task['allowed_paths'], 'run_configuration_mismatch')
    _require(result.get('limits') == {'attempts': common['max_attempts'], 'model_calls': common['max_model_calls'],
                                     'deadline_seconds': common['deadline_seconds']}
             and result.get('diagnostic_limits') == {'exploratory_probe_runs': common['max_probe_runs'],
                                                     'verification_probe_runs_per_candidate': 8}, 'run_budget_mismatch')
    # The only permitted policy is read-only; no candidate is silently promoted to an LLM result.
    _require(result.get('attempts') == [] and result.get('delegation_history') == [], 'unexpected_scripted_candidate_or_delegation')
    _require(result.get('error_type') is None, 'offline_policy_execution_failed')
    artifacts = dict(audit['artifact_sha256'])
    for index in range(1, result['diagnostic_probe_runs'] + 1):
        probe = root / 'diagnostics' / f'probe-{index:02d}'
        names = _check_probe(plan, probe, baseline_id)
        for name in names:
            file = probe / name
            artifacts[file.relative_to(root).as_posix()] = hashlib.sha256(_regular(file)).hexdigest()
    progress = result.get('diagnostic_progress')
    _require(isinstance(progress, dict), 'missing_progress_summary')
    records = []
    if progress.get('report') is not None:
        _require(progress['report'] == 'progress/main.json', 'unexpected_progress_path')
        file = root / progress['report']
        full = _json(file)
        _require(progress == {key: value for key, value in full.items() if key not in {'records', 'processed_rounds'}},
                 'progress_summary_mismatch')
        records = full['records']
        events = [_json_line(line) for line in (root / 'events.jsonl').read_text().splitlines() if line.strip()]
        observed = [event for event in events if event['event'] == 'diagnostic_progress_observed'
                    and event.get('agent_role', 'main') == 'main']
        _require(len(records) == progress['rounds_observed'] == len(observed), 'progress_round_count_mismatch')
        for record, event in zip(records, observed):
            _require(all(record[key] == event[key] for key in ('model_round', 'new_observation', 'visible_repeat', 'streak'))
                     and [item['fingerprint'] for item in record['observations']] == event['fingerprints'],
                     'progress_event_mismatch')
        artifacts[progress['report']] = hashlib.sha256(_regular(file)).hexdigest()
    usage = audit['usage']
    metrics = {'model_wrapper_calls': usage['model_wrapper_calls'], 'reported_usage_calls': usage['reported_calls'],
               'missing_usage_calls': usage['missing_usage_calls'], 'all_calls_total_tokens': usage['all_calls_total_tokens'],
               'tools_admitted': result['tool_calls'], 'tools_proposed': result['tool_statistics']['calls'],
               'exploratory_probe_runs': result['diagnostic_probe_runs'],
               'reused_observations': audit['counts']['reused_observations'],
               'assessed_tool_rounds': len(records),
               'duplicate_only_rounds': sum(item['visible_repeat'] and not item['new_observation'] for item in records),
               'candidate_attempts': 0, 'first_candidate_seconds': None,
               'stop_reason': result.get('stop_reason'), 'diagnostic_stop_reason': result.get('diagnostic_stop_reason'),
               'decision_proposal': (result.get('final_decision') or {}).get('decision')}
    return {'audit': audit, 'artifact_sha256': artifacts, 'baseline_id': baseline_id,
            'result_state': result['state'], 'candidate_status': result['status'], 'metrics': metrics}


def _json_line(line):
    # The independent auditor has already validated this bounded event stream.
    return json.loads(line)


def _validate_rows(plan, rows):
    expected = {item['run_id']: item for item in plan['schedule']}
    _require(isinstance(rows, list) and len(rows) == len(expected), 'incomplete_planned_run_inventory')
    seen = set()
    for row in rows:
        identity = row.get('run_id')
        _require(identity in expected and identity not in seen
                 and all(row.get(key) == value for key, value in expected[identity].items()),
                 'duplicate_or_mismatched_run_identity')
        seen.add(identity)
        _require(row.get('state') in {'pending', 'running', 'completed', 'stopped', 'invalid', 'cancelled', 'not_run'}
                 and type(row.get('started')) is bool, 'invalid_run_state')
        if row.get('audit_status') == 'passed':
            _require(isinstance(row.get('baseline_id'), str)
                     and re.fullmatch('[a-f0-9]{64}', row['baseline_id']), 'missing_audited_baseline_identity')
            metrics = row.get('metrics', {})
            for key in ('model_wrapper_calls', 'reported_usage_calls', 'missing_usage_calls', 'tools_admitted',
                        'tools_proposed', 'exploratory_probe_runs', 'reused_observations',
                        'assessed_tool_rounds', 'duplicate_only_rounds', 'candidate_attempts'):
                _require(type(metrics.get(key)) is int and metrics[key] >= 0, 'invalid_audited_metric')
            _require(metrics['reported_usage_calls'] + metrics['missing_usage_calls'] == metrics['model_wrapper_calls'],
                     'invalid_audited_usage_accounting')
            total = metrics.get('all_calls_total_tokens')
            _require(total is None if metrics['missing_usage_calls'] else type(total) is int and total >= 0,
                     'unknown_usage_relabelled_as_zero')


def build_pairs(plan, rows):
    _validate_rows(plan, rows)
    by_key = {(row['task_id'], row['repeat'], row['condition']): row for row in rows}
    pairs = []
    for task in plan['tasks']:
        for repeat in range(1, plan['repeats'] + 1):
            sides = [by_key.get((task, repeat, condition)) for condition in ('adaptive', 'fixed')]
            comparable = all(side and side.get('audit_status') == 'passed' and side.get('state') == 'completed' for side in sides)
            comparable = comparable and sides[0].get('baseline_id') == sides[1].get('baseline_id')
            pair = {'task_id': task, 'repeat': repeat, 'comparable': bool(comparable),
                    'reason': 'complete_scripted_pair' if comparable else 'missing_invalid_or_stopped_side',
                    'delta_adaptive_minus_fixed': None, 'token_savings_claim_allowed': False}
            if comparable:
                pair['delta_adaptive_minus_fixed'] = {
                    key: sides[0]['metrics'][key] - sides[1]['metrics'][key]
                    for key in ('model_wrapper_calls', 'tools_admitted', 'exploratory_probe_runs')}
            pairs.append(pair)
    return pairs


def summarize(plan, rows):
    _validate_rows(plan, rows)
    verified = [row for row in rows if row.get('audit_status') == 'passed']
    total_calls = sum(row['metrics']['model_wrapper_calls'] for row in verified)
    missing = sum(row['metrics']['missing_usage_calls'] for row in verified)
    complete_usage = len(verified) == len(plan['schedule']) and missing == 0
    pairs = build_pairs(plan, rows)
    return {'planned_runs': len(plan['schedule']), 'distinct_tasks': len(plan['tasks']), 'repeats': plan['repeats'],
            'started_runs': sum(row.get('started') is True for row in rows),
            'states': dict(Counter(row['state'] for row in rows)), 'audited_runs': len(verified),
            'planned_pairs': len(pairs), 'comparable_pairs': sum(pair['comparable'] for pair in pairs),
            'model_wrapper_calls_for_audited_runs': total_calls,
            'missing_usage_calls_for_audited_runs': missing,
            'all_calls_total_tokens': sum(row['metrics']['all_calls_total_tokens'] for row in verified) if complete_usage else None,
            'usage_complete_all_planned_runs': complete_usage,
            'optimization_success_rate': None, 'health_decision_accuracy': None,
            'real_model_api_calls': 0, 'real_llm_performance_claim_allowed': False,
            'cost_savings_claim_allowed': False,
            'decision_proposals': dict(Counter(row['metrics']['decision_proposal'] for row in verified))}


def _reports(output, record):
    record['summary'] = summarize(record['plan'], record['rows'])
    record['pairs'] = build_pairs(record['plan'], record['rows'])
    _write(output / 'results.json', record)
    with (output / 'runs.csv').open('w', newline='', encoding='utf-8') as stream:
        fields = ['run_id', 'task_id', 'repeat', 'condition', 'state', 'reason', 'audit_status',
                  'model_wrapper_calls', 'tools_admitted', 'exploratory_probe_runs', 'missing_usage_calls']
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in record['rows']:
            writer.writerow({**row, **row.get('metrics', {})})
    summary = record['summary']
    lines = ['# 公开开发集诊断评测记录', '',
             f"计划 `{record['plan']['plan_sha256']}`；模式 scripted_offline；真实模型API调用0。", '',
             f"任务{summary['distinct_tasks']}项；计划运行{summary['planned_runs']}次；审计通过{summary['audited_runs']}次。",
             f"状态：{summary['states']}；完整可比较对 {summary['comparable_pairs']}/{summary['planned_pairs']}。", '',
             '该对照仅改变固定预诊断与可选诊断顺序；不是B0/Full或多Agent效果对照。',
             '全部原始结果保留。没有候选不算优化通过；no_change只记为提议，insufficient_evidence为弃答。',
             '当前验收契约是速度改善与内存不退化，未实现独立内存改善或健康决策评分。',
             '脚本决策、公开合成开发任务与重复运行不构成真实模型效果、费用收益或独立泛化证据。',
             '用量缺失时总Token保持null；缺失或未运行的行不按零调用处理。', '',
             '| Run | State | Reason |', '| --- | --- | --- |']
    lines.extend(f"| {row['run_id']} | {row['state']} | {row.get('reason', '')} |" for row in record['rows'])
    (output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_plan(plan, dataset, output):
    """Execute only the source-pinned offline policy, serially in new directories."""
    validate_plan(plan, dataset)
    output = _output(output, dataset)
    _require(_image_id(plan['image_id']) == plan['image_id'], 'local_image_drift')
    # Imports occur only after validation; no ModelSettings/provider factory is accepted.
    from .diagnosis import DiagnosticSession
    from .agent import run_agent
    from .diagnostic_eval_policy import create_scripted_model
    output.mkdir(parents=True)
    _write(output / 'plan.json', plan)
    rows = [{**item, 'state': 'pending', 'started': False, 'reason': None,
             'audit_status': None, 'metrics': {}} for item in plan['schedule']]
    record = {'schema_version': 1, 'mode': 'scripted_offline', 'plan': plan, 'rows': rows,
              'batch_state': 'running', 'stop_reason': None, 'real_model_api_calls': 0}
    control = _BatchControl(output, plan['batch_timeout_seconds'])
    manifest, _ = load_dataset(dataset)
    tasks = {task['id']: task for task in manifest['tasks']}
    baselines = {}
    _reports(output, record)
    try:
        for row in rows:
            try:
                control.check()
                validate_plan(plan, dataset)
                _require(all(state == 'cleaned' for state in control.resources.values()), 'batch_cleanup_unconfirmed')
                row.update(started=True, state='running')
                _reports(output, record)
                task = tasks[row['task_id']]
                if row['task_id'] not in baselines:
                    control.scope = 'baseline:' + row['task_id']
                    baseline = prepare_task(dataset, task, output / 'tasks' / row['task_id'])
                    run_tests(baseline, plan['image_id'], min(plan['common']['test_timeout'],
                              max(.001, control.deadline - time.monotonic())), execution_control=control)
                    _check_baseline(baseline, task, plan['image_id'])
                    baselines[row['task_id']] = baseline
                baseline = baselines[row['task_id']]
                _check_baseline(baseline, task, plan['image_id'])
                directory = output / 'runs' / row['run_id']
                directory.mkdir(parents=True)
                control.scope = row['run_id']
                started = time.monotonic()
                session = DiagnosticSession(baseline, directory / 'agent', set(task['allowed_paths']),
                                             strategy=row['condition'], execution_control=control, **plan['common'])
                result = run_agent(session, create_scripted_model(), plan['task_text'])
                row['stop_received'] = result.get('stop_reason')
                row['elapsed_seconds'] = round(time.monotonic() - started, 6)
                _write(directory / 'provenance.json', {'mode': 'scripted_offline', 'real_model_calls': 0,
                       'plan_sha256': plan['plan_sha256'], 'run_id': row['run_id'], 'result': result})
                validate_plan(plan, dataset)
                control.check()
                checked = _verify_run(plan, row, directory, baseline, task)
                _write(directory / 'audit.json', checked['audit'])
                row.update({key: value for key, value in checked.items() if key != 'audit'})
                row.update(audit_status='passed', state='completed' if result['state'] == 'completed' else 'stopped',
                           reason=result.get('stop_reason'))
                _require(all(state == 'cleaned' for state in control.resources.values()), 'batch_cleanup_unconfirmed')
                if result.get('stop_reason') == 'cancel_requested':
                    row['state'] = 'cancelled'
                    record.update(batch_state='stopped', stop_reason='cancel_requested')
                    break
            except (Exception, KeyboardInterrupt) as exc:
                reason = str(exc) if isinstance(exc, (ValueError, BudgetExceeded)) else type(exc).__name__
                cancelled = isinstance(exc, (KeyboardInterrupt, BudgetExceeded)) or row.get('stop_received') == 'cancel_requested'
                if isinstance(exc, KeyboardInterrupt) or row.get('stop_received') == 'cancel_requested':
                    reason = 'cancel_requested'
                row.update(state='cancelled' if cancelled else 'invalid',
                           reason=reason, audit_status='failed' if row['started'] else None)
                record.update(batch_state='stopped', stop_reason=reason)
                break
            finally:
                _reports(output, record)
        validate_plan(plan, dataset)
        if record['batch_state'] == 'running':
            record['batch_state'] = 'completed'
    except (Exception, KeyboardInterrupt) as exc:
        record.update(batch_state='stopped', stop_reason=str(exc) if isinstance(exc, (ValueError, BudgetExceeded)) else type(exc).__name__)
    finally:
        for row in rows:
            if row['state'] == 'pending':
                row.update(state='not_run', reason=record['stop_reason'])
        record['containers_confirmed_cleaned'] = all(state == 'cleaned' for state in control.resources.values())
        if not record['containers_confirmed_cleaned']:
            record.update(batch_state='stopped', stop_reason='batch_cleanup_unconfirmed')
        _reports(output, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('plan', help='冻结公开开发集脚本评测计划，不调用模型')
    make.add_argument('--dataset', type=Path, required=True)
    make.add_argument('--image', required=True)
    make.add_argument('--output', type=Path, required=True)
    make.add_argument('--task', action='append')
    make.add_argument('--repeats', type=int, default=1)
    make.add_argument('--seed', type=int, default=0)
    make.add_argument('--transport', choices=('local', 'mcp'), default='local')
    make.add_argument('--max-model-calls', type=int, default=12)
    make.add_argument('--max-attempts', type=int, default=3)
    make.add_argument('--max-probe-runs', type=int, default=6)
    make.add_argument('--deadline', type=float, default=180)
    make.add_argument('--timeout', type=float, default=30)
    make.add_argument('--batch-timeout', type=float, default=900)
    execute = commands.add_parser('run', help='串行执行冻结的离线脚本计划，不接受provider配置')
    execute.add_argument('--plan', type=Path, required=True)
    execute.add_argument('--dataset', type=Path, required=True)
    execute.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            result = create_plan(args.dataset, args.image, tasks=args.task, repeats=args.repeats,
                                 seed=args.seed, transport=args.transport, max_model_calls=args.max_model_calls,
                                 max_attempts=args.max_attempts, max_probe_runs=args.max_probe_runs,
                                 deadline_seconds=args.deadline, test_timeout=args.timeout,
                                 batch_timeout_seconds=args.batch_timeout)
            destination = _output(args.output, args.dataset)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'plan_sha256': result['plan_sha256'], 'runs': len(result['schedule']),
                              'real_model_api_calls': 0}))
            return 0
        result = run_plan(_json(args.plan), args.dataset, args.output)
        print(json.dumps({'batch_state': result['batch_state'], 'summary': result['summary']}, ensure_ascii=False))
        return 0 if result['batch_state'] == 'completed' else 2
    except (ValueError, OSError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'rejected', 'error_type': type(exc).__name__,
                          'reason': str(exc) if isinstance(exc, ValueError) else None}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
