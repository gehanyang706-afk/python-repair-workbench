"""Container-only registered inputs; the caller separates public and final jobs.

The worker never chooses a module, callable, command, file or acceptance limit
from a model proposal. It measures one reviewed library operation per job.
Final acceptance and provenance checks belong to the independent host runner.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time
import tracemalloc


WORKLOADS = ('sql-format-v2', 'sql-comments-v3', 'iterator-slice-v2')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def validate_input(workload, value):
    if type(value) is not dict:
        raise ValueError('registered input must be a JSON object')
    if workload in ('sql-format-v2', 'sql-comments-v3'):
        if (set(value) != {'size', 'kind'} or type(value['size']) is not int
                or not 0 <= value['size'] <= 4000 or value['kind'] not in ('line_comments', 'quoted_markers')):
            raise ValueError('invalid SQL workload input')
    elif workload == 'iterator-slice-v2':
        if (set(value) != {'size', 'start', 'stop', 'step'} or type(value['size']) is not int
                or not 0 <= value['size'] <= 100000 or type(value['step']) is not int
                or not -16 <= value['step'] <= 16 or value['step'] == 0
                or any(v is not None and (type(v) is not int or not -200000 <= v <= 200000)
                       for v in (value['start'], value['stop']))):
            raise ValueError('invalid iterator workload input')
    else:
        raise ValueError('unregistered repository workload')


def validate_job(job):
    required = {'workload_id', 'input', 'input_sha256', 'probe', 'warmups', 'repeats'}
    if (type(job) is not dict or not required <= set(job) <= required | {'batch_iterations', 'gc_policy', 'warmup_seconds'}
            or job['probe'] not in ('benchmark', 'memory')
            or type(job['warmups']) is not int or not 1 <= job['warmups'] <= 3
            or type(job['repeats']) is not int or not 3 <= job['repeats'] <= 9):
        raise ValueError('invalid registered measurement job')
    if (type(job.get('batch_iterations', 1)) is not int or not 1 <= job.get('batch_iterations', 1) <= 10000
            or job.get('gc_policy', 'unmodified') not in ('unmodified', 'collect_before_sample')):
        raise ValueError('invalid registered sampling protocol')
    seconds = job.get('warmup_seconds', 0)
    if type(seconds) not in (int, float) or not 0 <= seconds <= 5 or not math.isfinite(seconds):
        raise ValueError('invalid minimum warmup duration')
    validate_input(job['workload_id'], job['input'])
    if job['input_sha256'] != hashlib.sha256(canonical(job['input']).encode()).hexdigest():
        raise ValueError('registered input identity mismatch')


def _operation(job, workspace):
    value = job['input']
    if job['workload_id'] in ('sql-format-v2', 'sql-comments-v3'):
        import sqlparse as library
        row = '-- measured comment\n' if value['kind'] == 'line_comments' else "SELECT '-- kept', '/* kept */'; -- removed\n"
        prepared = row * value['size']
        if job['workload_id'] == 'sql-format-v2':
            prepared += 'SELECT 1;\n'
        invoke = lambda: library.format(prepared, strip_comments=True)
    else:
        import more_itertools as library
        invoke = lambda: list(library.islice_extended(iter(range(value['size'])),
            value['start'], value['stop'], value['step']))
    origin = Path(library.__file__).resolve(strict=True)
    if workspace not in origin.parents:
        raise ValueError('target library was imported outside the selected repository')
    return invoke


def execute(job, workspace):
    validate_job(job)
    workspace = Path(workspace).resolve(strict=True)
    sys.path.insert(0, str(workspace))
    invoke = _operation(job, workspace)
    warmup_started = time.perf_counter()
    for _ in range(job['warmups']):
        invoke()
    while time.perf_counter() - warmup_started < job.get('warmup_seconds', 0):
        invoke()
    samples, digests, lengths = [], [], []
    for _ in range(job['repeats']):
        output = None  # Release previous output before standardizing GC state.
        if job.get('gc_policy', 'unmodified') == 'collect_before_sample':
            gc.collect()
        if job['probe'] == 'benchmark':
            iterations = job.get('batch_iterations', 1)
            started = time.perf_counter_ns()
            for _iteration in range(iterations):
                output = invoke()
            sample = (time.perf_counter_ns() - started) / 1e9 / iterations
        else:
            tracemalloc.start()
            try:
                output = invoke()
                sample = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        # Encoding, hashing and result serialization are outside measurement.
        digests.append(hashlib.sha256(canonical(output).encode()).hexdigest())
        lengths.append(len(output))
        samples.append(sample)
    return {'schema_version': 1, 'workload_id': job['workload_id'], 'probe': job['probe'],
            'input_sha256': job['input_sha256'], 'samples': samples,
            'output_sha256': digests, 'output_lengths': lengths,
            'environment': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                            'system': platform.system(), 'machine': platform.machine(), 'clock': 'perf_counter_ns'}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, default=Path('/workspace'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    job = json.loads(args.job.read_text())
    encoded = json.dumps(execute(job, args.workspace), allow_nan=False) + '\n'
    if args.output is None:
        print(encoded, end='', flush=True)
    else:
        args.output.write_text(encoded)
