"""Audit and compare a frozen baseline with original/new-task evaluation groups."""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_workbench.evaluation import load_dataset, selected_tasks, summarize, compare as assess_reports
from repair_workbench.snapshot import verify_snapshot, snapshot_id


def read(run):
    return json.loads((run/'results.json').read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inspect(run, dataset, names=None, *, check_current_code=True):
    record = read(run)
    manifest, sha = load_dataset(dataset)
    require(record['dataset_sha256'] == sha, 'dataset mismatch')
    require([r['id'] for r in record['rows']] == [t['id'] for t in selected_tasks(manifest, names)], 'task selection mismatch')
    require(record['summary'] == summarize(record['rows'], 'run'), 'summary mismatch')
    if check_current_code:
        for name, sha in record['agent_version']['code'].items():
            require(hashlib.sha256((ROOT/'repair_workbench'/name).read_bytes()).hexdigest() == sha,
                    'runtime changed during evaluation')
    observed = Counter()
    tasks = []
    snapshots = 0
    for row in record['rows']:
        require(row['state'] == 'completed', 'unfinished row')
        require(row['qualification']['qualified'], 'unqualified task')
        agent = row['agent']
        case = run/row['id']
        require(agent == json.loads((case/'agent/result.json').read_text()), 'agent result mismatch')
        require(agent['model_calls'] <= record['limits']['calls_per_task'], 'model budget exceeded')
        require(len(agent['attempts']) <= record['limits']['attempts_per_task'], 'attempt budget exceeded')
        events = [json.loads(s) for s in (case/'agent/events.jsonl').read_text().splitlines()]
        starts = [e for e in events if e['event'] == 'model_started']
        finished = [e for e in events if e['event'] == 'model_finished']
        require([e['call'] for e in starts] == list(range(1,agent['model_calls']+1)), 'call sequence mismatch')
        require(sum(bool(e.get('usage_reported')) for e in finished) == agent['token_usage']['reports'], 'usage coverage mismatch')
        for key in ['input_tokens','output_tokens']:
            require(sum(e.get(key) or 0 for e in finished) == agent['token_usage'][key], 'token total mismatch')
        for event in finished:
            observed[str(event.get('finish_reason'))] += 1
        base = verify_snapshot(case/'qualification/visible/baseline')
        protected = {f['path']:f['sha256'] for f in base['files'] if f['path'] not in agent['allowed_paths']}
        for file in case.rglob('manifest.json'):
            snap = verify_snapshot(file.parent); snapshots += 1
            if 'agent' in file.relative_to(case).parts:
                actual = {f['path']:f['sha256'] for f in snap['files']}
                require(actual.keys() == {f['path'] for f in base['files']}, 'candidate inventory changed')
                require(all(actual.get(p)==h for p,h in protected.items()), 'protected file changed')
        for file in case.rglob('reports/result.json'):
            test = json.loads(file.read_text())
            require(test['image_id'] == record['image_id'] and test['cleanup_ok'], 'image/cleanup mismatch')
            require(test['snapshot_id'] == snapshot_id(verify_snapshot(file.parent.parent)), 'test/snapshot mismatch')
        require((row['outcome']=='passed') == (agent['status']=='passed' and row.get('holdout',{}).get('status')=='passed'), 'success scoring mismatch')
        if agent['status'] == 'passed':
            hidden_base=case/'qualification/holdout/baseline'
            hidden=case/'holdout-verification'
            before=json.loads((hidden_base/'reports/result.json').read_text())
            after=json.loads((hidden/'reports/result.json').read_text())
            require(assess_reports(hidden_base,hidden,before,after)==row['holdout'], 'holdout verdict mismatch')
        attempts=agent['attempts']
        tasks.append({'id':row['id'],'outcome':row['outcome'],'reason':agent['reason'],
            'completion_diagnosis':agent.get('completion_diagnosis'),
            'calls':agent['model_calls'],'tokens':agent['token_usage'],'elapsed_seconds':row['elapsed_seconds'],
            'attempts':[{'number':a['number'],'tool':a.get('submission_tool'),'phase':a.get('phase'),
                         'status':a['status'],'reason':a.get('reason')} for a in attempts],
            'last_model_observation':agent.get('last_model_observation'),
            'invalid_tool_calls':sum(e.get('invalid_tool_call_count',0) for e in finished),
            'no_candidate':not attempts,
            'recovery_to_public_pass':any(a['status']!='passed' for a in attempts) and agent['status']=='passed'})
    return {'summary':record['summary'],'tasks':tasks,'finish_reasons':dict(observed),
            'snapshots_verified':snapshots,'audit':'passed'}


def compare(baseline, original, new):
    before, after, fresh = read(baseline), read(original), read(new)
    for key in ['model','model_configuration','limits','image_id']:
        require(before[key] == after[key] == fresh[key], 'comparison configuration mismatch: '+key)
    require(before['dataset_sha256']==after['dataset_sha256'], 'original dataset changed')
    require(after['agent_version']==fresh['agent_version'], 'group runtime mismatch')
    require(before['agent_version']['dependencies']==after['agent_version']['dependencies'], 'dependency version changed')
    require(after['summary']['model_calls']+fresh['summary']['model_calls']<=72, 'batch budget exceeded')
    old_group=inspect(original, ROOT/'benchmarks/real-bugs-v1')
    new_group=inspect(new, ROOT/'benchmarks/generalization-v1')
    changes={}
    for key in ['model_calls','tool_calls','candidate_attempts','elapsed_seconds']:
        b,a=before['summary'][key],after['summary'][key]
        changes[key]={'before':b,'after':a,'change_percent':round((a-b)/b*100,2) if b else None}
    for key in ['input_tokens','output_tokens']:
        b,a=before['summary']['token_usage'][key],after['summary']['token_usage'][key]
        changes[key]={'before':b,'after':a,'change_percent':round((a-b)/b*100,2) if b else None}
    return {'baseline_summary':before['summary'],'original_group':old_group,'new_group':new_group,
            'original_resource_changes':changes,'batch_model_calls':after['summary']['model_calls']+fresh['summary']['model_calls'],
            'comparison_scope':'Original tasks compared with baseline; new tasks reported separately, no pooled success rate.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline',type=Path);parser.add_argument('original',type=Path);parser.add_argument('new',type=Path)
    args=parser.parse_args()
    print(json.dumps(compare(args.baseline,args.original,args.new),ensure_ascii=False,indent=2))
