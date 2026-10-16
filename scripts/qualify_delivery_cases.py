"""Qualify pinned delivery cases with original tests; never constructs a model."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from repair_workbench.evaluation import load_dataset,qualify,fingerprint,write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    dataset=ROOT/'benchmarks/delivery-heldout-v1'
    manifest,sha=load_dataset(dataset)
    image=subprocess.check_output(['docker','image','inspect',args.image,'--format','{{.Id}}'],text=True,timeout=15).strip()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    record={'dataset_sha256':sha,'image_id':image,'agent_version':fingerprint(),
            'real_model_calls':0,'rows':[],'scope':'qualification only; not Agent repair performance'}
    for task in manifest['tasks']:
        result=qualify(dataset,task,output/task['id'],image,30)
        record['rows'].append({'id':task['id'],'task_type':task['task_type'],
            'upstream':task['upstream'],'base_commit':task['base_commit'],
            'fix_commit':task['fix_commit'],'source_file_count':task['source_file_count'],
            'qualification':result})
        write_json(output/'results.json',record)
        print(json.dumps({'task':task['id'],'type':task['task_type'],'qualified':result['qualified']}),flush=True)
    return 0 if all(row['qualification']['qualified'] for row in record['rows']) else 1


if __name__=='__main__':
    raise SystemExit(main())
