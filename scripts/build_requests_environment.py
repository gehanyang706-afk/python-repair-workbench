"""Build a local derived environment from the verified historical artifact bundle.

No network is available during installation. Target sources, tests and official
grading remain unchanged; the resulting image is explicitly a derived image.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4


SOURCE_IMAGE = 'sha256:d1c8c15eeb5e12b7eb3815307d1131d68c72d53419fa2f8b89bc46bc0009a61b'


def build(bundle, output, tag):
    bundle, output = Path(bundle).resolve(strict=True), Path(output).resolve()
    if not re.fullmatch(r'repair-workbench-requests-env:[a-zA-Z0-9_.-]+', tag):
        raise ValueError('a new project-specific image tag is required')
    records = json.loads((bundle / 'artifacts.json').read_text())['artifacts']
    expected = {('pytest-httpbin', '0.2.0'), ('httpbin', '0.4.1'), ('Flask', '0.10.1'),
                ('Werkzeug', '0.11.4'), ('Jinja2', '2.8'), ('MarkupSafe', '0.23'),
                ('itsdangerous', '0.24'), ('decorator', '4.0.9'), ('six', '1.10.0')}
    if len(records) != 9 or {(r['package'], r['version']) for r in records} != expected:
        raise ValueError('expected the nine reviewed historical dependencies')
    names = []
    for record in records:
        name = record['file']
        if Path(name).name != name:
            raise ValueError('invalid bundle path')
        data = (bundle / name).read_bytes()
        if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('bundle drift')
        names.append('/bundle/' + name)
    if subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True, timeout=15).returncode == 0:
        raise FileExistsError('refusing to replace an existing image tag')
    output.mkdir(parents=True, exist_ok=False)
    name = 'repair-requests-deps-' + uuid4().hex[:12]
    receipt = {'source_image': SOURCE_IMAGE, 'bundle_sha256': hashlib.sha256(
        (bundle / 'artifacts.json').read_bytes()).hexdigest(), 'artifacts': records,
        'container': name, 'derived_environment': True}
    code = ('import subprocess,sys,json,importlib.metadata as m; '
            'subprocess.run([sys.executable,"-m","pip","install","--no-index",'
            '"--no-deps","--no-build-isolation",*' + repr(names) + '],check=True); '
            'import flask,httpbin,pytest_httpbin; '
            'print(json.dumps({p:m.version(p) for p in ' + repr([r['package'] for r in records]) + '}))')
    started = time.monotonic()
    try:
        created = subprocess.run(['docker', 'create', '--pull', 'never', '--name', name,
            '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '1g', '--cpus', '1', '--pids-limit', '128', '--mount',
            f'type=bind,src={bundle},dst=/bundle,readonly', SOURCE_IMAGE,
            '/opt/miniconda3/envs/testbed/bin/python', '-c', code],
            capture_output=True, text=True, timeout=20)
        if created.returncode:
            raise RuntimeError('isolated dependency container creation failed')
        with (output / 'build.log').open('wb') as log:
            run = subprocess.run(['docker', 'start', '-a', name], stdout=log,
                                 stderr=subprocess.STDOUT, timeout=180)
        receipt['build_returncode'] = run.returncode
        if run.returncode:
            raise RuntimeError('dependency installation/import failed; inspect build.log')
        committed = subprocess.run(['docker', 'commit', name, tag],
                                   capture_output=True, text=True, timeout=30)
        if committed.returncode or not re.fullmatch(r'sha256:[a-f0-9]{64}', committed.stdout.strip()):
            raise RuntimeError('derived image commit failed')
        receipt.update(derived_image_id=committed.stdout.strip(), image_tag=tag)
    except BaseException as exc:
        receipt['error_type'] = type(exc).__name__
        raise
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
        check = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
        receipt.update(cleanup_ok=check.returncode != 0 and 'no such' in check.stderr.lower(),
                       elapsed_seconds=round(time.monotonic() - started, 3))
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    if not receipt['cleanup_ok']:
        raise RuntimeError('dependency container cleanup unconfirmed')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    result = build(args.bundle, args.output, args.tag)
    print(json.dumps({key: result[key] for key in ('derived_image_id', 'cleanup_ok')}))
