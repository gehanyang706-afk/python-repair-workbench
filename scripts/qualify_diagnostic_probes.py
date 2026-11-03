"""Qualify trusted public workloads using real containers, without model calls.

This is environment/tool qualification, not an Agent score or held-out benchmark.
Every first result is retained, including inconclusive and failed observations.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from repair_workbench.diagnostics import run_probe
from repair_workbench.runner import run_tests
from repair_workbench.snapshot import create_snapshot


def qualify(output, image):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    corpus = ROOT / 'benchmarks/ugc-diagnostics-dev-v1'
    spec = importlib.util.spec_from_file_location('trusted_ugc_qualification', corpus / 'qualification.py')
    qualification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qualification)
    manifest = qualification.fixture_manifest()
    report = {'schema_version': 1, 'type': 'public_synthetic_workload_tool_qualification',
              'real_model_calls': 0, 'agent_success_rate': None,
              'independent_evaluation': False,
              'manifest_sha256': hashlib.sha256((corpus / 'manifest.json').read_bytes()).hexdigest(),
              'cases': [], 'status': 'running'}

    def save():
        (output / 'qualification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    save()
    try:
        for task in manifest['tasks']:
            case = output / task['id']
            repo = case / 'source'
            repo.mkdir(parents=True)
            for relative in task['files']:
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(qualification._trusted_bytes(task['id'], relative))
            for args in (('init', '-q'), ('add', '--all'), ('commit', '-qm', 'Trusted public workload')):
                subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                                '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                '-C', str(repo), *args], capture_output=True, check=True, timeout=30)
            baseline = case / 'baseline'
            create_snapshot(repo, baseline)
            tests = run_tests(baseline, image)
            item = {'case_id': task['id'], 'original_tests': tests, 'probes': {}}
            report['cases'].append(item)
            save()
            if tests['status'] != 'tests_passed' or not tests['cleanup_ok']:
                raise RuntimeError('workload_test_or_cleanup_failed')
            for kind in ('cpu', 'memory', 'benchmark'):
                probe = run_probe(baseline, case / 'probes' / kind, tests['image_id'], kind)
                item['probes'][kind] = probe
                save()
                if (probe['status'] != 'ok' or not probe['cleanup_ok']
                        or not probe['summary'].get('semantic_ok')):
                    raise RuntimeError('registered_probe_qualification_failed')
        report['status'] = 'qualified'
    except Exception as exc:
        report['status'], report['error_type'] = 'failed', type(exc).__name__
        raise
    finally:
        save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args(argv)
    try:
        result = qualify(args.output, args.image)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps({'status': result['status'], 'cases': len(result['cases']),
                      'real_model_calls': 0, 'report': str(args.output / 'qualification.json')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
