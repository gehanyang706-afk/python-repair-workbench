"""Read-only regrade and cross-artifact audit using the pinned official parser."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_workbench.swebench_worker import verify_harness


def load(path):
    return json.loads(path.read_text())


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def audit(qualification):
    lock = verify_harness()
    from swebench.harness.utils import make_test_spec
    from swebench.harness.grading import get_eval_report
    qualification = Path(qualification).resolve(strict=True)
    summary = load(qualification / 'qualification.json')
    rows = []
    for role in ('negative', 'gold'):
        root = qualification / role
        payload = load(root / 'evaluator-input.json')
        saved = load(root / 'result.json')
        require(saved == summary[role], 'qualification/result mismatch')
        require(saved['harness_commit'] == lock['commit'], 'harness identity mismatch')
        row, pred = payload['record'], payload['prediction']
        directory = (root / 'logs/evaluation' / payload['run_id'] /
                     pred['model_name_or_path'] / row['instance_id'])
        record = dict(row);record['image'] = payload['image_id']
        report = get_eval_report(make_test_spec(record), pred,
                                 str(directory / 'test_output.txt'), True)
        require(report == load(directory / 'report.json') == saved['official_report'],
                'official regrade mismatch')
        item = report[row['instance_id']]
        require(item['patch_successfully_applied'], 'unusable official output')
        require(saved['status'] == ('resolved' if item['resolved'] else 'unresolved'),
                'official status mismatch')
        binding = load(root / 'source-binding.json')
        require(binding['content_match'] and binding['dirty_returncode'] == 0
                and binding['upstream_commit'] == row['base_commit']
                and binding['checked_files'] == len(payload['base_git_blobs']),
                'source binding mismatch')
        policy = load(root / 'container-policy.json')
        require(policy['image_id'] == payload['image_id']
                and policy['network'] == payload['network_policy']
                and policy['memory_bytes'] == 2 * 1024**3
                and policy['nano_cpus'] == 2_000_000_000 and policy['pids_limit'] == 256
                and 'ALL' in policy['cap_drop'] and not policy['cap_add'],
                'container policy mismatch')
        if role == 'gold':
            require((directory / 'patch.diff').read_text() == pred['model_patch'],
                    'control patch changed')
        name = 'sweb.eval.%s.%s' % (row['instance_id'].lower(), payload['run_id'])
        inspected = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
        require(saved['cleanup_ok'] and inspected.returncode != 0
                and 'no such' in inspected.stderr.lower(), 'container cleanup unconfirmed')
        tests = item['tests_status']
        rows.append({'role': role, 'status': saved['status'],
            'F2P': {k: len(v) for k, v in tests['FAIL_TO_PASS'].items()},
            'P2P': {k: len(v) for k, v in tests['PASS_TO_PASS'].items()},
            'source_files_checked': binding['checked_files'], 'network': policy['network'],
            'cleanup_ok': True})
    expected = rows[0]['status'] == 'unresolved' and rows[1]['status'] == 'resolved'
    require(summary['qualified'] == expected, 'qualification misclassified')
    return {'ok': True, 'qualified': expected, 'real_model_calls': 0,
            'harness_commit': lock['commit'], 'controls': rows,
            'meaning': 'evidence integrity and official regrade; not Agent effectiveness'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qualification', type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.qualification)
    except Exception as exc:
        result = {'ok': False, 'reason': str(exc), 'error_type': type(exc).__name__}
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
