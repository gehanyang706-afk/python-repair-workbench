"""Read-only cross-checks of the pilot's first traces, usage and independent tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repair_workbench.public_live_evaluation import validate_plan, make_plan, hash_json, CONTEXT_RESERVATION
from repair_workbench.runner import classify_result
from repair_workbench.snapshot import verify_snapshot, snapshot_id
from repair_workbench.verification import assess_repair, test_outcomes


def read(path):
    return json.loads(path.read_text())


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def check_plan(plan, root):
    try:
        validate_plan(plan)
        return 'current_source'
    except ValueError:
        # A subsequent offline fix must not rewrite the source used by an old
        # live run. Check that exact archived source and every other plan field.
        archive = root / 'frozen-implementation'
        expected = plan['source']['files']
        actual = {p.relative_to(archive).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in archive.rglob('*') if p.is_file()}
        require(actual == expected, 'historical source archive mismatch')
        current = make_plan(plan['qualification'])
        require({k: v for k, v in current['source'].items() if k != 'files'} ==
                {k: v for k, v in plan['source'].items() if k != 'files'}, 'dependency drift')
        current['source'] = plan['source']
        current.pop('plan_hash')
        current['plan_hash'] = hash_json(current)
        require(current == plan, 'historical plan or non-source evidence changed')
        return 'archived_source'


def audit(root, check_cleanup=True):
    root = Path(root).resolve(strict=True)
    record = read(root / 'evaluation.json')
    ledger = read(root / 'token-ledger.json')
    plan = read(root / 'plan.json')
    source_validation = check_plan(plan, root)
    require(record['plan'] == plan, 'record plan mismatch')
    require(ledger['plan_hash'] == plan['plan_hash'], 'ledger plan mismatch')
    rows = record['rows']
    require([{k: r[k] for k in ('task', 'repeat', 'variant', 'id')} for r in rows] == plan['schedule'],
            'schedule or denominators changed')
    require(ledger['limits'] == plan['limits'], 'ledger limits changed')
    require([c['index'] for c in ledger['calls']] == list(range(1, len(ledger['calls']) + 1)),
            'missing or duplicate call index')
    known = {r['id']: r for r in rows}
    require(all(c['run_id'] in known for c in ledger['calls']), 'unknown run in ledger')
    require(sum(c['charged_tokens'] for c in ledger['calls']) <= plan['limits']['batch_tokens'],
            'batch cap exceeded')
    schemas = set()
    reports = 0
    accepted = 0
    expected_traces = set()
    all_calls = []
    for row in rows:
        calls = [c for c in ledger['calls'] if c['run_id'] == row['id']]
        require(sum(c['charged_tokens'] for c in calls) <= plan['limits']['run_tokens'], 'run cap exceeded')
        require(len(calls) <= plan['limits']['requests_per_run'], 'request cap exceeded')
        if row['state'] == 'not_run':
            require(not calls, 'not_run row has provider calls')
            continue
        case = root / row['id']
        result = read(case / 'agent/result.json')
        require(result == row['agent'], 'agent row mismatch')
        events = [json.loads(s) for s in (case / 'agent/events.jsonl').read_text().splitlines()]
        started = [e for e in events if e['event'] == 'model_worker_started']
        finished = [e for e in events if e['event'] == 'model_worker_finished']
        require(len(started) == len(finished) == len(calls), 'worker/call count mismatch')
        require(all(e['cleanup_ok'] for e in finished), 'worker cleanup unconfirmed')
        for start, end in zip(started, finished):
            require(start['pid'] == end['pid'], 'worker cleanup identity mismatch')
            if check_cleanup:
                process = subprocess.run(['ps', '-p', str(start['pid']), '-o', 'command='],
                                         capture_output=True, text=True, timeout=10)
                require(start['request_process_id'] not in process.stdout, 'request worker still alive')
        for call in calls:
            prefix = case / 'provider-traces' / ('call-%03d' % call['index'])
            req = prefix.with_suffix('.request.json')
            resp = prefix.with_suffix('.response.json')
            require(req.is_file(), 'missing first request trace')
            expected_traces.add(req)
            request = read(req)
            require(request['model'] == 'qwen-max' and request['temperature'] == 0
                    and request['max_output_tokens'] == 4096, 'provider settings mismatch')
            require('api_key' not in request, 'credential field in trace')
            schemas.add(hashlib.sha256(json.dumps(request['tools'], sort_keys=True).encode()).hexdigest())
            if call['state'] == 'settled':
                require(resp.is_file(), 'settled call lacks response')
                expected_traces.add(resp)
                message = read(resp)['data']
                usage = message.get('usage_metadata') or message['response_metadata'].get('token_usage')
                require(isinstance(usage, dict), 'usage missing')
                for k in ('input_tokens', 'output_tokens'):
                    require(type(usage.get(k)) is int and usage[k] >= 0 and usage[k] == call[k],
                            'raw usage/ledger mismatch')
                require(call['charged_tokens'] == usage['input_tokens'] + usage['output_tokens'],
                        'usage settlement mismatch')
            else:
                if resp.exists():
                    expected_traces.add(resp)
                require(call['charged_tokens'] == CONTEXT_RESERVATION and
                        call['input_tokens'] is None and call['output_tokens'] is None,
                        'unknown usage released or fabricated')
        if all(c['state'] == 'settled' for c in calls):
            require(result['token_usage']['reports'] == len(calls)
                    and result['usage_complete'] == (result['model_calls'] == len(calls)),
                    'agent usage completeness mismatch')
            for k in ('input_tokens', 'output_tokens'):
                require(result['token_usage'][k] == sum(c[k] for c in calls), 'agent usage total mismatch')
        q = Path(plan['qualification']) / row['task'] / 'qualification'
        base = verify_snapshot(q / 'visible/baseline')
        allowed = set(result['allowed_paths'])
        baseline_files = {f['path']: f['sha256'] for f in base['files']}
        for manifest_path in (case / 'agent').glob('attempt-*/verification/manifest.json'):
            candidate = verify_snapshot(manifest_path.parent)
            actual = {f['path']: f['sha256'] for f in candidate['files']}
            require(actual.keys() == baseline_files.keys(), 'candidate file set changed')
            changed = {p for p in actual if actual[p] != baseline_files[p]}
            require(changed and changed <= allowed, 'candidate modified unauthorized paths')
        for p in case.rglob('reports/result.json'):
            report = read(p)
            status, counts = classify_result(report['returncode'], p.parent / 'pytest.xml')
            require(status == report['status'] and counts == report['counts'], 'raw XML/report mismatch')
            require(report['cleanup_ok'], 'container cleanup unconfirmed')
            require(report['snapshot_id'] == snapshot_id(verify_snapshot(p.parent.parent)),
                    'test report snapshot mismatch')
            if check_cleanup:
                name = report['command'][report['command'].index('--name') + 1]
                inspected = subprocess.run(['docker', 'inspect', name],
                                           capture_output=True, timeout=15)
                require(inspected.returncode != 0, 'container still exists')
            reports += 1
        if row['outcome'] == 'passed':
            hidden = case / 'independent-verification'
            baseline = q / 'holdout/baseline'
            before = read(baseline / 'reports/result.json')
            after = read(hidden / 'reports/result.json')
            old = {f['path']: f['sha256'] for f in verify_snapshot(baseline)['files']}
            new = {f['path']: f['sha256'] for f in verify_snapshot(hidden)['files']}
            require(old.keys() == new.keys() and
                    {p for p in old if old[p] != new[p]} <= allowed,
                    'independent tests or unauthorized files changed')
            verdict = assess_repair(before, after, test_outcomes(baseline / 'reports/pytest.xml'),
                                    test_outcomes(hidden / 'reports/pytest.xml'))
            require(verdict['status'] == 'passed' and row['holdout']['status'] == 'passed',
                    'independent success unsupported')
            accepted += 1
        all_calls.extend(calls)
    require(set(root.glob('*/provider-traces/*.json')) == expected_traces, 'extra/missing trace files')
    require(len(schemas) <= 1, 'tool catalog differs between variants')
    return {'ok': True, 'source_validation': source_validation, 'planned_runs': len(rows),
            'completed_runs': sum(r['state'] == 'completed' for r in rows),
            'provider_calls': len(all_calls), 'independent_passed': accepted,
            'checked_test_reports': reports,
            'charged_or_reserved_tokens': sum(c['charged_tokens'] for c in all_calls),
            'tool_catalogs': len(schemas)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.directory)
    except Exception as exc:
        result = {'ok': False, 'reason': str(exc), 'error_type': type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
