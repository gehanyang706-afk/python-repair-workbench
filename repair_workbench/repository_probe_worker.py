"""Container-only probes for two reviewed real-history development workloads.

Importing this module does not import target code. execute() is called only by
the isolated container entry point, or with controlled substitutes in tests.
These probes are public development observations, not hidden final scoring.
"""
from __future__ import annotations

import argparse
import cProfile
from contextlib import redirect_stdout
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import pstats
import sys
import time
import tracemalloc


WORKLOADS = {'sql-format-dev-v1': (1000, 2000), 'iterator-slice-dev-v1': (5000, 50000)}
PROBES = ('benchmark', 'memory', 'cpu')
REPEATS = 5


def validate_job(workload, probe):
    if type(workload) is not str or workload not in WORKLOADS:
        raise ValueError('unknown_repository_workload')
    if type(probe) is not str or probe not in PROBES:
        raise ValueError('unknown_repository_probe')


def _digest(output):
    return hashlib.sha256(json.dumps(output, ensure_ascii=False, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _call(workload, size):
    if workload == 'sql-format-dev-v1':
        import sqlparse
        value = '-- development comment\n' * size
        return lambda: sqlparse.format(value, strip_comments=True)
    import more_itertools
    # Construct the iterator on each call; never reuse an exhausted iterator.
    return lambda: list(more_itertools.islice_extended(iter(range(size)), -size, -1, 1))


def execute(workload, probe, workspace, output):
    validate_job(workload, probe)
    workspace, output = Path(workspace), Path(output)
    sys.path.insert(0, str(workspace))
    rows = []
    for size in WORKLOADS[workload]:
        invoke = _call(workload, size)
        invoke()  # Public development warmup, not counted as a measured sample.
        samples, fingerprints, lengths = [], [], []
        for _ in range(REPEATS if probe == 'benchmark' else 1):
            if probe == 'benchmark':
                start = time.perf_counter_ns()
                value = invoke()
                metric = (time.perf_counter_ns() - start) / 1e9
            elif probe == 'memory':
                tracemalloc.start()
                try:
                    value = invoke()
                    metric = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            else:
                profile = cProfile.Profile()
                value = profile.runcall(invoke)
                profile.dump_stats(str(output / f'cpu-{size}.pstats'))
                stats = pstats.Stats(profile).stats
                functions = []
                for (filename, line, function), (_, calls, own, cumulative, _) in stats.items():
                    try: filename = str(Path(filename).relative_to(workspace))
                    except ValueError: filename = Path(filename).name
                    functions.append({'file': filename[:180], 'line': line,
                                      'function': function[:180], 'calls': calls,
                                      'own_seconds': own, 'cumulative_seconds': cumulative})
                functions.sort(key=lambda row: row['cumulative_seconds'], reverse=True)
                metric = {'top_functions': functions[:15]}
            samples.append(metric)
            fingerprints.append(_digest(value))
            lengths.append(len(value))
        rows.append({'size': size, 'samples': samples, 'output_sha256': fingerprints,
                     'output_lengths': lengths})
    report = {'schema_version': 1, 'workload': workload, 'probe': probe, 'rows': rows,
              'environment': {'python': platform.python_version(), 'machine': platform.machine(),
                              'clock': 'perf_counter_ns'},
              'scope': 'registered public development inputs; Python-traced allocations, not RSS'}
    (output / 'probe.json').write_text(json.dumps(report, indent=2))
    return report


def load_registered_registry():
    """Load only the evaluator-mounted registry, never a job-selected module."""
    definition = importlib.util.spec_from_file_location(
        '_trusted_repository_measure_registry', '/trusted/registry.py')
    if definition is None or definition.loader is None:
        raise ValueError('trusted_repository_registry_unavailable')
    registry = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(registry)
    return registry


def execute_registered_cpu(job, workspace):
    """Profile a registered public job in the container, outside final timing.

    Each reported duration is the profiled wall time per invocation for one
    repeat. Profiler overhead is included, so these are not benchmark samples.
    Output encoding, hashing, GC and summary formatting are never profiled.
    """
    fields = {'workload_id', 'input', 'input_sha256', 'probe', 'warmups', 'repeats',
              'batch_iterations', 'gc_policy', 'warmup_seconds'}
    if type(job) is not dict or set(job) != fields or job['probe'] != 'cpu':
        raise ValueError('invalid_registered_cpu_job')
    registry = load_registered_registry()
    measurement_job = dict(job, probe='benchmark')
    registry.validate_job(measurement_job)
    workspace = Path(workspace).resolve(strict=True)
    sys.path.insert(0, str(workspace))
    invoke = registry._operation(measurement_job, workspace)
    warmup_started = time.perf_counter()
    for _ in range(job['warmups']):
        invoke()
    while time.perf_counter() - warmup_started < job['warmup_seconds']:
        invoke()

    profile = cProfile.Profile()
    durations, fingerprints, lengths = [], [], []
    for _ in range(job['repeats']):
        value = None
        if job['gc_policy'] == 'collect_before_sample':
            gc.collect()
        started = time.perf_counter_ns()
        for _iteration in range(job['batch_iterations']):
            value = profile.runcall(invoke)
        durations.append((time.perf_counter_ns() - started) / 1e9 / job['batch_iterations'])
        fingerprints.append(hashlib.sha256(registry.canonical(value).encode()).hexdigest())
        lengths.append(len(value))

    functions = []
    for (filename, line, function), (_, calls, own, cumulative, _) in pstats.Stats(profile).stats.items():
        path = Path(filename)
        try:
            filename = path.resolve().relative_to(workspace).as_posix()
        except (OSError, ValueError):
            filename = path.name
        functions.append({'file': filename[:180], 'line': line,
                          'function': function[:180], 'calls': calls,
                          'own_seconds': own, 'cumulative_seconds': cumulative})
    functions.sort(key=lambda row: row['cumulative_seconds'], reverse=True)
    return {'schema_version': 1, 'workload_id': job['workload_id'], 'probe': 'cpu',
            'input_sha256': job['input_sha256'], 'profiled_seconds': durations,
            'output_sha256': fingerprints, 'output_lengths': lengths,
            'top_functions': functions[:15],
            'environment': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                            'system': platform.system(), 'machine': platform.machine(), 'clock': 'perf_counter_ns'}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload', choices=WORKLOADS)
    parser.add_argument('--probe', choices=PROBES)
    parser.add_argument('--job', type=Path)
    parser.add_argument('--workspace', type=Path, default=Path('/workspace'))
    parser.add_argument('--output', type=Path, default=Path('/reports'))
    args = parser.parse_args()
    if args.job is not None:
        if args.workload is not None or args.probe is not None:
            parser.error('--job cannot be combined with --workload or --probe')
        with redirect_stdout(sys.stderr):
            report = execute_registered_cpu(json.loads(args.job.read_text()), args.workspace)
        print(json.dumps(report, allow_nan=False), flush=True)
    else:
        if args.workload is None or args.probe is None:
            parser.error('--workload and --probe are required without --job')
        execute(args.workload, args.probe, args.workspace, args.output)
