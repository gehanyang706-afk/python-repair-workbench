"""Qualify public development workloads on complete pinned repository snapshots.

No model/patch generation, package installation, source execution on the host,
image download or hidden-test claim. Raw ABBA runs are always retained.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repair_workbench import repository_probe_worker as worker
from repair_workbench.snapshot import verify_snapshot, snapshot_id


def validate_report(report, workload, probe):
    if (type(report.get('schema_version')) is not int or report['schema_version'] != 1
            or report.get('workload') != workload or report.get('probe') != probe):
        raise ValueError('report identity mismatch')
    if [r['size'] for r in report['rows']] != list(worker.WORKLOADS[workload]):
        raise ValueError('workload changed')
    for row in report['rows']:
        if type(row['size']) is not int:
            raise ValueError('invalid workload size')
        count = worker.REPEATS if probe == 'benchmark' else 1
        if any(len(row[key]) != count for key in ('samples', 'output_sha256', 'output_lengths')):
            raise ValueError('missing raw samples')
        if any(type(v) is not str or not re.fullmatch(r'[a-f0-9]{64}', v) for v in row['output_sha256']):
            raise ValueError('invalid output identity')
        if any(type(v) is not int or v < 0 for v in row['output_lengths']):
            raise ValueError('invalid output length')
        if probe != 'cpu' and any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                                 or (probe == 'memory' and type(v) is not int) for v in row['samples']):
            raise ValueError('invalid measurement')
    return report


def run_probe(snapshot, output, image, workload, probe):
    worker.validate_job(workload, probe)
    snapshot, output = Path(snapshot).resolve(strict=True), Path(output).resolve()
    manifest = verify_snapshot(snapshot)
    if any(root == output or root in output.parents for root in (snapshot, Path(manifest['source_repo']).resolve())):
        raise ValueError('probe output must be outside source/snapshot')
    source = Path(worker.__file__).resolve()
    if any(',' in str(p) for p in (snapshot, output, source)):
        raise ValueError('unsupported mount path')
    image_id = subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                       stderr=subprocess.PIPE, timeout=15).decode().strip()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
        raise ValueError('invalid immutable image identity')
    output.mkdir(parents=True, exist_ok=False)
    name = 'repair-repository-probe-' + uuid4().hex[:12]
    command = ['docker', 'run', '--name', name, '--pull', 'never', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--user', f'{os.getuid()}:{os.getgid()}', '--pids-limit', '64', '--memory', '256m',
               '--cpus', '1', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
               '--mount', f'type=bind,src={snapshot / "workspace"},dst=/workspace,readonly',
               '--mount', f'type=bind,src={source},dst=/trusted/worker.py,readonly',
               '--mount', f'type=bind,src={output},dst=/reports', '--workdir', '/workspace',
               '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/tmp', '--entrypoint',
               'python', image_id, '-I', '/trusted/worker.py', '--workload', workload, '--probe', probe]
    result = {'status': 'execution_error', 'cleanup_ok': False, 'image_id': image_id,
              'snapshot_id': snapshot_id(manifest), 'workload': workload, 'probe': probe,
              'worker_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    started = time.monotonic()
    try:
        with (output / 'probe.log').open('xb') as log:
            run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=60)
        path = output / 'probe.json'
        if run.returncode == 0 and path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024:
            report = validate_report(json.loads(path.read_text()), workload, probe)
            result.update(status='ok', report=report)
    except subprocess.TimeoutExpired:
        result['status'] = 'timeout'
    except (ValueError, KeyError, TypeError) as exc:
        result.update(status='report_error', error_type=type(exc).__name__)
    finally:
        try:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
            inspect = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
            result['cleanup_ok'] = inspect.returncode != 0 and 'no such' in inspect.stderr.lower()
        except (OSError, subprocess.SubprocessError) as exc:
            result['cleanup_error_type'] = type(exc).__name__
        result['elapsed_seconds'] = round(time.monotonic() - started, 6)
        (output / 'result.json').write_text(json.dumps(result, indent=2))
    return result


def qualify(snapshots, output, image):
    snapshots, output = Path(snapshots), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tasks = [('sqlparse-comments-dev', 'sql-format-dev-v1', 'latency'),
             ('more-itertools-memory-dev', 'iterator-slice-dev-v1', 'memory')]
    rows = []
    try:
        for task, workload, goal in tasks:
            observations = []
            for probe in ('benchmark', 'memory'):
                for index, role in enumerate(('base', 'reference', 'reference', 'base')):
                    receipt = run_probe(snapshots / (task + '-' + role),
                        output / task / f'{probe}-{index}-{role}', image, workload, probe)
                    observations.append({'probe': probe, 'role': role, **receipt})
                    if receipt['status'] != 'ok' or not receipt['cleanup_ok']:
                        raise RuntimeError('development observation/cleanup failed')
            # All measured outputs must preserve the baseline behavior, per input.
            if (len({r['image_id'] for r in observations}) != 1
                    or len({r['worker_sha256'] for r in observations}) != 1
                    or len({json.dumps(r['report']['environment'], sort_keys=True) for r in observations}) != 1):
                raise ValueError('development measurement environment drift')
            metrics = []
            for size in worker.WORKLOADS[workload]:
                selected = [(r, next(s for s in r['report']['rows'] if s['size'] == size)) for r in observations]
                fingerprints = {h for _, v in selected for h in v['output_sha256']}
                ratios = {}
                for probe in ('benchmark', 'memory'):
                    medians = {role: statistics.median([v for r, s in selected
                        if r['probe'] == probe and r['role'] == role for v in s['samples']])
                        for role in ('base', 'reference')}
                    ratios[probe + '_ratio'] = medians['reference'] / medians['base']
                metrics.append({'size': size, 'same_output': len(fingerprints) == 1, **ratios})
            rows.append({'task': task, 'workload': workload, 'goal': goal, 'metrics': metrics,
                         'physical_probes': len(observations),
                         'scope': 'development qualification; no hidden inputs or LLM candidate'})
            print(json.dumps(rows[-1]), flush=True)
    finally:
        (output / 'qualification.json').write_text(json.dumps({'real_model_calls': 0,
            'scope': 'reference-effect observations, not final scoring or Agent success', 'rows': rows}, indent=2))
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    qualify(args.snapshots, args.output, args.image)
