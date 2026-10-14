"""Validate frozen paired evaluation evidence and derive comparable metrics."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from compare_evaluations import ROOT, inspect, read, require
from repair_workbench.evaluation import fingerprint, load_dataset, compare as assess_reports
from repair_workbench.runner import classify_result
from repair_workbench.snapshot import verify_snapshot
from repair_workbench.source_context import SIMPLE_PROFILES, PROFILES


def validate_artifacts(run, record, task):
    case = run / task['id']
    for scope in ('visible', 'holdout'):
        expected = {}
        roles = {'source': '', 'visible': 'tests/'}
        if scope == 'holdout':
            roles['holdout'] = 'tests/'
        for role, prefix in roles.items():
            expected.update({prefix + path[len(role) + 1:]: sha for path, sha in task['files'].items()
                             if path.startswith(role + '/')})
        base = case / 'qualification' / scope / 'baseline'
        actual = {item['path']: item['sha256'] for item in verify_snapshot(base)['files']}
        require(actual == expected, 'pinned baseline input mismatch')
        fixed = case / 'qualification' / scope / 'oracle'
        expected_fixed = {**expected, **{path: task['files']['oracle/' + path] for path in task['allowed_paths']}}
        require({item['path']: item['sha256'] for item in verify_snapshot(fixed)['files']} == expected_fixed,
                'pinned oracle input mismatch')
        before = json.loads((base / 'reports/result.json').read_text())
        after = json.loads((fixed / 'reports/result.json').read_text())
        qualification = record['rows'][0]['qualification']['checks'][scope]
        require(before == qualification['baseline'] and after == qualification['oracle'],
                'qualification report mismatch')
        require(assess_reports(base, fixed, before, after) == qualification['verdict'],
                'qualification verdict mismatch')
    for report in case.rglob('reports/result.json'):
        result = json.loads(report.read_text())
        if isinstance(result.get('returncode'), int):
            status, counts = classify_result(result['returncode'], report.parent / 'pytest.xml')
            require(status == result['status'] and counts == result['counts'], 'test XML classification mismatch')
    base = case / 'qualification/visible/baseline'
    before = json.loads((base / 'reports/result.json').read_text())
    for attempt in record['rows'][0]['agent']['attempts']:
        verified = case / 'agent' / f"attempt-{attempt['number']:02d}" / 'verification'
        if (verified / 'reports/result.json').exists():
            after = json.loads((verified / 'reports/result.json').read_text())
            verdict = assess_reports(base, verified, before, after)
            require(attempt['status'] == verdict['status'] and attempt['reason'] == verdict['reason'],
                    'public verdict mismatch')
    if record['rows'][0]['agent']['status'] == 'passed':
        row = record['rows'][0]
        number = next(attempt['number'] for attempt in row['agent']['attempts'] if attempt['status'] == 'passed')
        public = verify_snapshot(case / 'agent' / f'attempt-{number:02d}' / 'verification')
        hidden_base = verify_snapshot(case / 'qualification/holdout/baseline')
        public_files = {item['path']: item['sha256'] for item in public['files']}
        expected = {item['path']: item['sha256'] for item in hidden_base['files']}
        expected.update({path: public_files[path] for path in task['allowed_paths']})
        require({item['path']: item['sha256'] for item in verify_snapshot(case / 'holdout-verification')['files']} == expected,
                'independent verification did not use the exact public candidate')


def metrics(run, row):
    case = run / row['id']
    agent = row['agent']
    events = [json.loads(line) for line in (case / 'agent/events.jsonl').read_text().splitlines()]
    models = [event for event in events if event['event'] == 'model_finished']
    reads = [event for event in events if event['event'] == 'source_read']
    first_valid = next((attempt['number'] for attempt in agent['attempts']
                        if attempt.get('phase') == 'verification'), None)
    first = next((event for event in events if event['event'] == 'attempt_started'
                  and event['number'] == first_valid), None)
    first_call = max((event['call'] for event in events if event['event'] == 'model_started'
                      and first and event['elapsed_seconds'] <= first['elapsed_seconds']), default=None)
    counters = agent['submission_counts']
    require(counters['requests'] == len(agent['attempts']), 'submission count mismatch')
    require(counters['materialized'] == len(list((case / 'agent').glob('attempt-*/candidate/manifest.json'))),
            'materialized count mismatch')
    if agent['profile'] in SIMPLE_PROFILES:
        require(counters['schema_rejections'] == sum(event['event'] in {'schema_rejected', 'invalid_tool_call'}
                                                   for event in events), 'schema rejection count mismatch')
    statistics=agent.get('tool_statistics')
    if statistics is not None:
        require(statistics['calls']==sum(len(event['tools'])+event['invalid_tool_call_count'] for event in models),
                'tool proposal count mismatch')
        for field in ('calls','schema_rejections'):
            require(statistics[field]==sum(value[field] for value in statistics['by_tool'].values())
                    ==sum(value[field] for value in statistics['by_category'].values()),
                    'tool category total mismatch')
        require(statistics['schema_rejections']==counters['schema_rejections'],'legacy schema total mismatch')
    latencies = []
    starts = {event['call']: event['elapsed_seconds'] for event in events if event['event'] == 'model_started'}
    for event in models:
        latencies.append(event['elapsed_seconds'] - starts[event['call']])
    telemetry = agent['profile'] != 'v2'
    delivered = sum(event['delivered_lines'] for event in reads)
    overlap = delivered - sum(event['new_unique_lines'] for event in reads)
    return {
        'id': row['id'], 'profile': agent['profile'], 'outcome': row['outcome'],
        'public_passed': agent['status'] == 'passed', 'independent_passed': row['outcome'] == 'passed',
        'stop_reason': agent.get('stop_reason'), 'submission_counts': counters,
        'tool_statistics':statistics,
        'usage_complete':agent['token_usage']['reports']==agent['model_calls'],
        'usage_missing_calls':agent['model_calls']-agent['token_usage']['reports'],
        'syntax_passed_candidates': sum(attempt.get('phase') == 'verification' for attempt in agent['attempts']),
        'first_candidate_submission_call': first_call,
        'calls_before_first_candidate': first_call - 1 if first_call is not None else None,
        'model_calls': agent['model_calls'], 'token_usage': agent['token_usage'],
        'finish_reasons': dict(Counter(str(event.get('finish_reason')) for event in models)),
        'invalid_tool_calls': sum(event.get('invalid_tool_call_count', 0) for event in models),
        'source_read_requests': sum(event.get('tool') == 'read_source' for event in events if event['event'] == 'tool_started'),
        'source_read_telemetry_available': telemetry,
        'successful_source_reads': len(reads) if telemetry else None,
        'returned_source_characters': sum(event['returned_characters'] for event in reads) if telemetry else None,
        'source_characters_by_purpose':dict(Counter({purpose:sum(event['returned_characters'] for event in reads
                if event.get('purpose','tool')==purpose) for purpose in
                {event.get('purpose','tool') for event in reads}})) if telemetry else None,
        'read_failure_reasons':dict(Counter(event.get('reason') for event in events
            if event['event']=='operation_rejected' and event.get('operation') in
            {'read_source','read_symbol','source_outline','search_source','read_test_log'})) if telemetry else None,
        'fully_repeated_read_ranges': sum(event['repeated'] for event in reads) if telemetry else None,
        'overlap_line_ratio': overlap / delivered if delivered else None,
        'source_read_rejections': sum(event.get('operation') == 'read_source' for event in events
                                      if event['event'] == 'operation_rejected') if telemetry else None,
        'framework_read_file_requests': sum(event.get('tools', []).count('read_file') for event in models),
        'invalid_tool_recoveries': agent.get('invalid_tool_recoveries', 0),
        'max_model_response_seconds': round(max(latencies, default=0), 3),
        'max_input_tokens_per_call': max((event.get('input_tokens') or 0 for event in models), default=0),
        'elapsed_seconds': row['elapsed_seconds'], 'patch_lines': row.get('patch_lines', 0),
    }


def audit(root, *, check_current_code=True):
    root = root.resolve(strict=True)
    experiment = json.loads((root / 'experiment.json').read_text())
    require(experiment['state'] == 'completed', 'experiment incomplete')
    require(experiment['code_unchanged'], 'code changed during experiment')
    current_fingerprint = fingerprint()
    if check_current_code:
        require(current_fingerprint == experiment['code_fingerprint'], 'current runtime differs from frozen experiment')
    rows, configuration, pairs = [], None, {}
    expected_profiles=experiment.get('profiles',['v2','simple_edit'])
    require(len(expected_profiles)>=2 and len(set(expected_profiles))==len(expected_profiles)
            and set(expected_profiles)<=set(PROFILES),'invalid experiment profile matrix')
    names = set()
    snapshots = 0
    for selection in experiment['run_order']:
        name = selection['run']
        require(isinstance(name, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', name) and name not in names,
                'invalid or duplicate run path')
        names.add(name)
        run = root / name
        relative_dataset = Path(selection['dataset'])
        require(not relative_dataset.is_absolute() and '..' not in relative_dataset.parts
                and relative_dataset.parts[0] == 'benchmarks', 'dataset path outside approved corpora')
        dataset = (ROOT / relative_dataset).resolve(strict=True)
        require((ROOT / 'benchmarks').resolve() in dataset.parents, 'dataset path outside approved corpora')
        record = read(run)
        report = inspect(run, dataset, names=[selection['task']], check_current_code=check_current_code)
        require(record['dataset'] == load_dataset(dataset)[0], 'recorded dataset manifest mismatch')
        task = next(task for task in record['dataset']['tasks'] if task['id'] == selection['task'])
        validate_artifacts(run, record, task)
        snapshots += report['snapshots_verified']
        require(record['agent_version'] == experiment['code_fingerprint'], 'frozen agent fingerprint mismatch')
        require(record['dataset_sha256'] == experiment['datasets'][selection['dataset']], 'frozen dataset mismatch')
        require(record['profile'] == selection['profile'], 'profile mismatch')
        require(record['model'] == experiment['model'] and record['image_id'] == experiment['image_id'],
                'frozen model or image mismatch')
        expected_limits = {'calls_per_task': experiment['limits']['calls_per_run'],
                           'attempts_per_task': experiment['limits']['attempts_per_run'],
                           'deadline_per_task': experiment['limits']['deadline_per_run'],
                           'test_timeout': experiment['limits']['test_timeout']}
        require(record['limits'] == expected_limits, 'frozen limits mismatch')
        current = {key: record[key] for key in ('model', 'model_configuration', 'image_id', 'limits', 'agent_version')}
        require(configuration is None or current == configuration, 'paired configuration mismatch')
        configuration = current
        row = metrics(run, record['rows'][0])
        row['run'] = name
        row['dataset'] = selection['dataset']
        rows.append(row)
        pair = pairs.setdefault((selection['dataset'], selection['task']), [])
        pair.append(selection['profile'])
    require(all(len(pair) == len(expected_profiles) and set(pair) == set(expected_profiles) for pair in pairs.values()),
            'unpaired task selection')
    totals = {key: sum(row['token_usage'][key] for row in rows) for key in ('input_tokens', 'output_tokens', 'reports')}
    calls = sum(row['model_calls'] for row in rows)
    require(calls == experiment['total_calls'] and calls <= experiment['approved_max_calls'], 'batch call limit mismatch')
    for key in ('input_tokens', 'output_tokens'):
        require(totals[key] == experiment['total_' + key], 'batch token mismatch')
    require(totals['reports'] == calls, 'incomplete token usage coverage')
    return {
        'audit': 'passed', 'git_revision': experiment['git_revision'],
        'current_code_matches': current_fingerprint == experiment['code_fingerprint'],
        'snapshots_verified': snapshots, 'model_calls': calls, 'approved_max_calls': experiment['approved_max_calls'],
        'token_usage': totals, 'task_elapsed_seconds': round(sum(row['elapsed_seconds'] for row in rows), 3),
        'batch_elapsed_seconds': experiment['elapsed_seconds'],
        'usage_cost_cny_non_thinking': (round((totals['input_tokens'] * .8 + totals['output_tokens'] * 2) / 1e6, 6)
                                      if experiment['model'] == 'qwen-plus' else None),
        'usage_cost_cny_thinking_output': (round((totals['input_tokens'] * .8 + totals['output_tokens'] * 8) / 1e6, 6)
                                         if experiment['model'] == 'qwen-plus' else None),
        'cost_scope': 'Usage estimate at Beijing <=128k/request rates; not a provider invoice.',
        'rows': rows,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    parser.add_argument('--allow-code-change', action='store_true',
                        help='Audit historical artifacts while reporting current code drift separately.')
    args = parser.parse_args()
    print(json.dumps(audit(args.experiment, check_current_code=not args.allow_code_change), ensure_ascii=False, indent=2))
