"""Build the pinned, explicitly adapted two-bug pilot; performs no model calls."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def main():
    dataset = ROOT / 'benchmarks/bugsinpy-derived-v1'
    existing = json.loads((dataset / 'manifest.json').read_text())
    provenance = {task['id']: task['upstream_git_blobs'] for task in existing['tasks']}
    tasks = []
    for name, item in provenance.items():
        # apply_patch may add one trailing newline. Recover only the exact Git blob.
        for relative, expected in {**{'source/' + p: s for p, s in item['files'].items()},
                                   'oracle/' + item['allow']: item['oracle_sha']}.items():
            path = dataset / name / relative
            data = path.read_bytes()
            while blob_sha(data) != expected and data.endswith(b'\n'):
                data = data[:-1]
            if blob_sha(data) != expected:
                raise ValueError('upstream Git blob mismatch: ' + relative)
            path.write_bytes(data)
        files = {p.relative_to(dataset / name).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((dataset / name).rglob('*')) if p.is_file()}
        tasks.append({'id': name, 'upstream': 'https://github.com/tqdm/tqdm',
            'benchmark': 'BugsInPy-derived module-level adaptation; not official BugsInPy score',
            'benchmark_commit': '11c5f1eea954a42132cfd06bf257766a7963e0fd',
            'benchmark_bug': int(name.split('-')[1]),
            'metadata_url': 'https://github.com/soarsmu/BugsInPy/blob/11c5f1eea954a42132cfd06bf257766a7963e0fd/projects/tqdm/bugs/' + name.split('-')[1] + '/bug.info',
            'base_commit': item['base'], 'fix_commit': item['fix'],
            'allowed_paths': [item['allow']],
            'task': ('修复 tenumerate 在非默认 start 下与内置 enumerate 编号不一致的问题，保持空序列、迭代器及其他包装器的既有行为。'
                     if name == 'tqdm-1' else
                     '修复 tqdm 在未知总长度的迭代器上使用自定义 unit_scale 时崩溃的问题，保持已知总长度和默认缩放的既有行为。'),
            'adaptation': 'Complete top-level package sources retained byte-for-byte; optional package modules not installed. Original trigger assertions ported from nose to pytest with monitor disabled; evaluator-only boundary tests added before model calls. Python/image differ from official Python 3.6.9.',
            'upstream_git_blobs': item, 'files': files})
    (dataset / 'manifest.json').write_text(json.dumps({'schema_version': 1,
        'name': 'bugsinpy-derived-v1', 'split': 'public development pilot; no contamination guarantee',
        'tasks': tasks}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'tasks': len(tasks), 'files': sum(len(t['files']) for t in tasks)}))


if __name__ == '__main__':
    main()
