"""Exercise the pinned official evaluator on a synthetic two-test local image."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_workbench.candidate import make_patch
from repair_workbench.snapshot import create_snapshot
from repair_workbench.swebench_adapter import import_dataset, prepare_workspace, qualify


def prepare_fixture(output, image='repair-swebench-fixture:20261103'):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = output / 'repo'
    repo.mkdir()
    fixture = ROOT / 'benchmarks/swebench-harness-fixture-v1'
    shutil.copyfile(fixture / 'calculator.py', repo / 'calculator.py')
    shutil.copytree(fixture / 'tests', repo / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    for args in [('init', '-q'), ('add', '.'),
                 ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                  '-c', 'commit.gpgsign=false', 'commit', '-qm', 'baseline')]:
        subprocess.run(['git', '-C', str(repo), *args], env=env, check=True, capture_output=True)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
    base = output / 'patch-baseline'
    create_snapshot(repo, base)
    patch = make_patch(base / 'workspace', {'calculator.py': 'def add(a, b):\n    return a + b\n'}, output).decode()
    row = {'instance_id': 'workbench__fixture-1', 'repo': 'workbench/fixture',
           'base_commit': commit, 'problem_statement': 'add(2, 3) returns -1; it should add both arguments.',
           'patch': patch, 'test_patch': '', 'version': 'fixture-v1', 'image': image,
           'eval_script': '#!/bin/bash\ncd /testbed\necho ">>>>> Start Test Output"\n'
                          'python -m pytest -rA tests/test_calculator.py\n'
                          'echo ">>>>> End Test Output"\n',
           'FAIL_TO_PASS': ['tests/test_calculator.py::test_add'],
           'PASS_TO_PASS': ['tests/test_calculator.py::test_zero'],
           'eval_type': 'pass_and_fail', 'log_parser': 'parse_log_pytest'}
    raw = output / 'fixture.json'
    raw.write_text(json.dumps([row]))
    dataset = output / 'evaluator-dataset'
    import_dataset(raw, dataset, dataset_name='synthetic-harness-fixture', revision='0' * 40)
    prepare_workspace(dataset, row['instance_id'], repo, output / 'agent-workspace')
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--harness-python', type=Path, required=True)
    p.add_argument('--image', default='repair-swebench-fixture:20261103')
    args = p.parse_args()
    row = prepare_fixture(args.output, args.image)
    result = qualify(row, args.output / 'official-qualification', args.harness_python,
                     workspace=args.output / 'agent-workspace', timeout=60)
    print(json.dumps({'synthetic': True, 'real_model_calls': 0, 'qualified': result['qualified'],
                      'negative': result['negative']['status'], 'gold': result['gold']['status']}, indent=2))
    return 0 if result['qualified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
