"""Container-only probes for two reviewed real-history development workloads.

Importing this module does not import target code. execute() is called only by
the isolated container entry point, or with controlled substitutes in tests.
These probes are public development observations, not hidden final scoring.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload', choices=WORKLOADS, required=True)
    parser.add_argument('--probe', choices=PROBES, required=True)
    parser.add_argument('--workspace', type=Path, default=Path('/workspace'))
    parser.add_argument('--output', type=Path, default=Path('/reports'))
    args = parser.parse_args()
    execute(args.workload, args.probe, args.workspace, args.output)
