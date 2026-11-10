"""Container entry point for an explicitly proposed reproduction program.

Its output is an untrusted observation, never a correctness/performance verdict.
The parent enforces execution resources and output size independently.
"""
import json
import runpy
import sys
import time


def main():
    sys.path.insert(0, '/workspace')
    started = time.perf_counter()
    program = runpy.run_path('/reproducer/program.py')
    run = program.get('run')
    if not callable(run):
        raise ValueError('reproducer must define run()')
    observation = run()
    print(json.dumps({'schema_version': 1, 'observation': observation,
                      'elapsed_seconds': time.perf_counter() - started},
                     ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
