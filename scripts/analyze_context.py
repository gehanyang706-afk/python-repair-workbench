"""Aggregate source-read evidence without exposing source or tool arguments."""
import argparse
import json
from collections import Counter
from pathlib import Path


def one(case: Path):
    result=json.loads((case/'result.json').read_text())
    events=[json.loads(line) for line in (case/'events.jsonl').read_text().splitlines()]
    calls=Counter(e['tool'] for e in events if e.get('event')=='tool_started')
    reads=[e for e in events if e.get('event')=='source_read']
    models=[e for e in events if e.get('event')=='model_finished']
    requested_framework_reads=sum(e.get('tools',[]).count('read_file') for e in models)
    return {'case':case.parent.name, 'model_calls':result.get('model_calls'),
        'read_source_calls':calls['read_source'],'read_symbol_calls':calls['read_symbol'],
        'framework_read_file_requests':requested_framework_reads, 'source_outline_calls':calls['source_outline'],
        'source_search_calls':calls['search_source'],
        'business_tool_calls':sum(calls.values()), 'candidate_attempts':len(result.get('attempts',[])),
        'input_tokens':result.get('token_usage',{}).get('input_tokens'),
        'output_tokens':result.get('token_usage',{}).get('output_tokens'),
        'logged_model_observations':len(models),
        'read_ranges_observed':len(reads),
        'read_ranges_complete':len(reads)==calls['read_source']+calls['read_symbol'] and requested_framework_reads==0,
        'unique_lines':sum(e.get('new_unique_lines',0) for e in reads) if reads else None,
        'repeated_read_calls':sum(bool(e.get('repeated')) for e in reads) if reads else None,
        'suppressed_read_calls':sum(bool(e.get('suppressed')) for e in reads) if reads else None,
        'returned_characters':sum(e.get('returned_characters',0) for e in reads) if reads else None,
        'note':'Historical events do not record source ranges or returned text; repetition and utility cannot be reconstructed.' if not reads else None}


def analyze(root:Path):
    cases=sorted(p for p in root.glob('*/agent') if (p/'result.json').exists() and (p/'events.jsonl').exists())
    if not cases:raise ValueError('no agent result/events found')
    rows=[one(p) for p in cases]
    total={key:sum(r[key] for r in rows) for key in ['model_calls','read_source_calls','read_symbol_calls','framework_read_file_requests','source_outline_calls','source_search_calls','business_tool_calls','candidate_attempts','input_tokens','output_tokens','logged_model_observations','read_ranges_observed']}
    return {'source':str(root),'cases':rows,'totals':total,
            'limitations':'Business-tool calls and model-requested read_file names are known; framework tool execution and exact historical read ranges/returned text/utility are not fully observed.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evaluation_dir',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    result=analyze(args.evaluation_dir)
    encoded=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded)
    else:print(encoded,end='')
    return 0

if __name__=='__main__':raise SystemExit(main())
