"""Prepare two reviewed real-history performance development repositories.

The developer has seen both reference changes: these tasks are dev-only. The
script downloads pinned public sources (<=4 MiB combined), verifies entire Git
trees and never imports target code or modifies upstream source/tests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import ssl
import subprocess
import tarfile
import urllib.request


TASKS = [
    {'id': 'sqlparse-comments-dev', 'repo': 'andialbrecht/sqlparse',
     'fix_commit': 'ef2012a5eeb491e604dea2b00d516904a3830c87',
     'source': 'https://github.com/andialbrecht/sqlparse/security/advisories/GHSA-f2ff-p2ww-7p4p',
     'goal': 'latency', 'source_root': 'sqlparse',
     'problem': 'Formatting SQL files containing many comments becomes slow as files grow. Preserve formatting behavior.'},
    {'id': 'more-itertools-memory-dev', 'repo': 'more-itertools/more-itertools',
     'fix_commit': 'ea68e05fbe566a01ab416a0a2dd5e9637ac4e789',
     'source': 'https://github.com/more-itertools/more-itertools/issues/1019',
     'goal': 'memory', 'source_root': 'more_itertools',
     'problem': 'Slicing a long iterator with negative bounds uses substantial memory. Preserve iterator slicing behavior.'},
]


def prepare(output):
    import certifi
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    context = ssl.create_default_context(cafile=certifi.where())
    total = 0
    records = []

    def get(url, maximum):
        request = urllib.request.Request(url, headers={'User-Agent': 'repair-workbench-source-qualification'})
        with urllib.request.urlopen(request, timeout=25, context=context) as response:
            data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('source metadata/download quota exceeded')
        return data

    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')

    def git(repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                       stderr=subprocess.PIPE, timeout=30).decode().strip()

    def extract(archive, repository, expected_tree):
        repository.mkdir()
        seen, expanded, prefix = set(), 0, None
        with tarfile.open(archive, 'r:gz') as stream:
            for entry in stream:
                path = PurePosixPath(entry.name)
                if path.is_absolute() or '..' in path.parts or '.git' in path.parts or '\\' in entry.name:
                    raise ValueError('unsafe source archive')
                if not path.parts:
                    continue
                prefix = prefix or path.parts[0]
                if path.parts[0] != prefix:
                    raise ValueError('source archive has multiple roots')
                if entry.isdir():
                    continue
                if not entry.isfile() or len(path.parts) < 2:
                    raise ValueError('source links/submodules unsupported')
                relative = str(PurePosixPath(*path.parts[1:]))
                expanded += entry.size
                if relative in seen or len(seen) >= 2000 or expanded > 20 * 1024 * 1024:
                    raise ValueError('source archive limits exceeded')
                seen.add(relative)
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.extractfile(entry).read())
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)
        git(repository, 'init', '-q')
        git(repository, '-c', 'core.autocrlf=false', 'add', '--force', '--all')
        git(repository, '-c', 'user.name=Archive', '-c', 'user.email=archive@localhost',
            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
            'commit', '-qm', 'Verified upstream performance development source')
        tree = git(repository, 'rev-parse', 'HEAD^{tree}')
        if tree != expected_tree:
            raise ValueError('upstream source tree mismatch')
        return {'tree': tree, 'normalized_local_commit': git(repository, 'rev-parse', 'HEAD'),
                'files': len(seen), 'expanded_bytes': expanded}

    try:
        for task in TASKS:
            directory = output / task['id']
            directory.mkdir()
            fixed_raw = get(f'https://api.github.com/repos/{task["repo"]}/git/commits/{task["fix_commit"]}', 1024 * 1024)
            fixed = json.loads(fixed_raw)
            if fixed['sha'] != task['fix_commit'] or not fixed['parents']:
                raise ValueError('invalid reference commit metadata')
            base_commit = fixed['parents'][0]['sha']
            record = {**task, 'base_commit': base_commit, 'split': 'dev',
                      'developer_exposure': ['root_cause', 'reference_patch'],
                      'source_qualified': False, 'performance_qualified': False, 'versions': {}}
            records.append(record)
            for role, commit, metadata in [('base', base_commit, None), ('reference', task['fix_commit'], fixed_raw)]:
                raw = metadata or get(f'https://api.github.com/repos/{task["repo"]}/git/commits/{commit}', 1024 * 1024)
                parsed = json.loads(raw)
                if parsed['sha'] != commit:
                    raise ValueError('commit identity mismatch')
                (directory / (role + '-commit.json')).write_bytes(raw)
                archive = get(f'https://codeload.github.com/{task["repo"]}/tar.gz/{commit}',
                              4 * 1024 * 1024 - total)
                total += len(archive)
                archive_path = directory / (role + '.tar.gz')
                archive_path.write_bytes(archive)
                proof = extract(archive_path, directory / role, parsed['tree']['sha'])
                record['versions'][role] = {**proof, 'upstream_commit': commit,
                    'metadata_sha256': hashlib.sha256(raw).hexdigest(),
                    'archive_sha256': hashlib.sha256(archive).hexdigest()}
            record['source_qualified'] = True
    finally:
        (output / 'source-receipt.json').write_text(json.dumps({
            'scope': 'real-history development source qualification only; not model evidence',
            'downloaded_archive_bytes': total, 'tasks': records}, indent=2))
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps([{'id': r['id'], 'source_qualified': r['source_qualified'],
                       'base_commit': r['base_commit']} for r in prepare(args.output)]))
