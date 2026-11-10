"""Download a bounded, hash-checked historical test-dependency bundle.

No installation/build, model access, image pull or test modification occurs.
Run with the project's locked environment (certifi is required for TLS).
"""
import argparse
import hashlib
import json
from pathlib import Path
import ssl
import tarfile
import urllib.request
from urllib.parse import urlparse
import zipfile


PACKAGES = [('pytest-httpbin', '0.2.0'), ('httpbin', '0.4.1'), ('Flask', '0.10.1'),
            ('Werkzeug', '0.11.4'), ('Jinja2', '2.8'), ('MarkupSafe', '0.23'),
            ('itsdangerous', '0.24'), ('decorator', '4.0.9'), ('six', '1.10.0')]


def prepare(output, max_bytes=4 * 1024 * 1024):
    import certifi
    if type(max_bytes) is not int or not 1 <= max_bytes <= 4 * 1024 * 1024:
        raise ValueError('download cap must be 1..4 MiB')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    context = ssl.create_default_context(cafile=certifi.where())
    records, total = [], 0
    report = {'source': 'PyPI HTTPS metadata and release SHA256', 'artifacts': records}
    try:
        for name, version in PACKAGES:
            with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json',
                                        timeout=20, context=context) as response:
                metadata = json.loads(response.read(2 * 1024 * 1024))
            (output / (name + '-metadata.json')).write_text(json.dumps(metadata, indent=2))
            wheels = [u for u in metadata['urls'] if u['filename'].endswith('none-any.whl')]
            choices = wheels or [u for u in metadata['urls'] if u['packagetype'] == 'sdist']
            artifact = min(choices, key=lambda item: item['size'])
            if (Path(artifact['filename']).name != artifact['filename']
                    or urlparse(artifact['url']).scheme != 'https'
                    or urlparse(artifact['url']).hostname != 'files.pythonhosted.org'):
                raise ValueError('invalid artifact source')
            if total + artifact['size'] > max_bytes:
                raise ValueError('artifact download cap exceeded')
            with urllib.request.urlopen(artifact['url'], timeout=20, context=context) as response:
                content = response.read(max_bytes - total + 1)
            digest = hashlib.sha256(content).hexdigest()
            if len(content) != artifact['size'] or digest != artifact['digests']['sha256']:
                raise ValueError('artifact size/hash mismatch')
            path = output / artifact['filename']
            path.write_bytes(content)
            total += len(content)
            records.append({'package': name, 'version': version, 'file': path.name,
                            'bytes': len(content), 'sha256': digest, 'url': artifact['url']})
            # Inspect metadata as bytes; never execute setup.py on the host.
            if path.suffix == '.whl':
                with zipfile.ZipFile(path) as archive:
                    bodies = [archive.read(n).decode() for n in archive.namelist()
                              if n.endswith('.dist-info/METADATA')
                              and archive.getinfo(n).file_size < 100000]
            else:
                with tarfile.open(path) as archive:
                    members = [m for m in archive.getmembers() if m.isfile() and m.size < 100000
                               and (m.name.endswith('/setup.py') or m.name.endswith('/requirements.txt'))]
                    bodies = [archive.extractfile(m).read().decode() for m in members]
            (output / (name + '-inspection.txt')).write_text('\n'.join(bodies))
    except BaseException as exc:
        report['error_type'] = type(exc).__name__
        raise
    finally:
        report['downloaded_bytes'] = total
        (output / 'artifacts.json').write_text(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    receipt = prepare(args.output)
    print(json.dumps({'artifacts': len(receipt['artifacts']),
                      'downloaded_bytes': receipt['downloaded_bytes']}))
