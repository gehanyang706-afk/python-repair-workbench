"""Audit a frozen four-task baseline without executing models or candidate code."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_workbench.evaluation import load_dataset, summarize
from repair_workbench.snapshot import verify_snapshot


def audit(run, dataset):
    result = json.loads((run / 'results.json').read_text())
    manifest, dataset_hash = load_dataset(dataset)
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    require(result['dataset_sha256'] == dataset_hash, 'dataset changed')
    require(result['model'] == 'qwen-plus', 'unexpected model')
    require(result['limits'] == {'calls_per_task': 12, 'attempts_per_task': 3,
            'deadline_per_task': 240.0, 'test_timeout': 30.0}, 'baseline budget changed')
    for name, expected in result['agent_version']['code'].items():
        require(hashlib.sha256((ROOT/'repair_workbench'/name).read_bytes()).hexdigest() == expected,
                'runtime code changed: ' + name)
    require({r['id'] for r in result['rows']} == {t['id'] for t in manifest['tasks']}
            and len(result['rows']) == 4, 'task selection changed')
    require(result['summary'] == summarize(result['rows'], 'run'), 'summary mismatch')
    require(result['summary']['model_calls'] <= 48, 'total call limit exceeded')
    snapshots = 0
    for row in result['rows']:
        require(row['state'] == 'completed', 'incomplete task')
        case = run / row['id']
        agent = json.loads((case/'agent/result.json').read_text())
        require(agent == row['agent'], 'agent result mismatch')
        events = [json.loads(line) for line in (case/'agent/events.jsonl').read_text().splitlines()]
        require(sum(e['event'] == 'model_started' for e in events) == agent['model_calls'] <= 12,
                'model event/count mismatch')
        require(sum(e['event'] == 'model_finished' for e in events) == agent['token_usage']['reports'],
                'model completion/usage report mismatch')
        require(len(agent['attempts']) <= 3, 'attempt limit exceeded')
        base = verify_snapshot(case/'qualification/visible/baseline')
        expected = {x['path']: x['sha256'] for x in base['files']}
        for file in case.rglob('manifest.json'):
            snap = verify_snapshot(file.parent)
            snapshots += 1
            if 'agent' in file.relative_to(case).parts:
                actual = {x['path']: x['sha256'] for x in snap['files']}
                require(actual.keys() == expected.keys(), 'candidate changed file set')
                require(all(actual[p] == h for p,h in expected.items() if p not in agent['allowed_paths']),
                        'candidate changed protected files')
        for file in case.rglob('reports/result.json'):
            test = json.loads(file.read_text())
            require(test['image_id'] == result['image_id'] and test['cleanup_ok'],
                    'test environment or cleanup mismatch')
        require(not (case/'holdout-verification').exists() if agent['status'] != 'passed' else True,
                'unexpected holdout execution')
    return {'audit': 'passed', 'dataset_sha256': dataset_hash, 'snapshots_verified': snapshots,
            'model_calls': result['summary']['model_calls'],
            'checks': ['frozen runtime code', 'dataset bytes', 'exact four tasks', 'budgets',
                       'summary reconciliation', 'model event counts', 'snapshot hashes',
                       'protected files unchanged', 'same image and successful container cleanup']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--dataset', type=Path, default=ROOT/'benchmarks/real-bugs-v1')
    args = parser.parse_args()
    print(json.dumps(audit(args.run.resolve(), args.dataset.resolve()), ensure_ascii=False, indent=2))
