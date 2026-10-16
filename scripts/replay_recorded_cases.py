"""Reverify two recorded model patches; no Agent, inference or test modifications."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from repair_workbench.evaluation import load_dataset,prepare,write_json
from repair_workbench.candidate import create_candidate
from repair_workbench.runner import run_tests
from repair_workbench.verification import verify_candidate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    reference=ROOT/'docs/recorded-cases'
    specification=json.loads((reference/'manifest.json').read_text())
    record={'scope':'Recorded patch replay only; not a new model benchmark.',
            'real_model_calls':0,'rows':[]}
    for item in specification['cases']:
        dataset=ROOT/'benchmarks'/item['dataset'];manifest,sha=load_dataset(dataset)
        task=next(task for task in manifest['tasks'] if task['id']==item['id'])
        patch=reference/item['patch']
        if hashlib.sha256(patch.read_bytes()).hexdigest()!=item['patch_sha256']:
            raise ValueError('recorded patch hash mismatch')
        row={'id':item['id'],'dataset_sha256':sha,'patch_sha256':item['patch_sha256'],
             'checks':{}}
        for scope in ('public','independent'):
            folder=output/item['id']/scope
            baseline=prepare(dataset,task,folder/'input',holdout=scope=='independent')
            before=run_tests(baseline,args.image,30)
            if before['status']!='tests_failed' or not before['cleanup_ok']:
                raise ValueError('recorded failure did not reproduce')
            with tempfile.TemporaryDirectory(dir=folder) as temporary:
                staging=Path(temporary)/'workspace'
                shutil.copytree(baseline/'workspace',staging)
                # An independent temporary Git root prevents apply from using
                # the surrounding workbench checkout. The baseline stays intact.
                subprocess.run(['git','-C',str(staging),'init','-q'],check=True,capture_output=True)
                for options in (['--check'],[]):
                    subprocess.run(['git','-C',str(staging),'apply',*options,str(patch)],
                                   check=True,capture_output=True,timeout=15)
                files=json.loads((baseline/'manifest.json').read_text())['files']
                changes={entry['path']:(staging/entry['path']).read_text() for entry in files
                    if (staging/entry['path']).read_bytes()!=(baseline/'workspace'/entry['path']).read_bytes()}
                if not changes or not set(changes)<=set(task['allowed_paths']):
                    raise ValueError('recorded patch changed files outside approval')
            candidate=folder/'candidate';create_candidate(baseline,candidate,changes,set(task['allowed_paths']))
            verdict=verify_candidate(baseline,candidate,folder/'verification',set(task['allowed_paths']),30)
            row['checks'][scope]=verdict
            if verdict['status']!=item['expected_'+scope]:
                raise ValueError('replay differs from historical outcome')
        record['rows'].append(row)
        write_json(output/'results.json',record)
        print(json.dumps({'id':item['id'],'public':row['checks']['public']['status'],
                          'independent':row['checks']['independent']['status']}),flush=True)
    record['replay_matches_record']=True;write_json(output/'results.json',record)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
