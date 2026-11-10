"""Explicit large-repository snapshots omit doc links, never follow code links."""
from pathlib import Path
import json
import subprocess
import tarfile
import tempfile
import unittest

from repair_workbench.snapshot import create_snapshot, verify_snapshot, snapshot_limits
from repair_workbench.swebench_adapter import git_blobs, import_dataset, prepare_archive_workspace
from repair_workbench.swebench_worker import parse_source_blobs


class SnapshotProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'module.py').write_text('value = 1\n')

    def commit(self):
        for args in [('init', '-q'), ('add', '--all'), ('-c', 'user.name=Test',
            '-c', 'user.email=test@localhost', '-c', 'commit.gpgsign=false',
            '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Pinned source')]:
            subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def test_default_rejects_doc_links_explicit_profile_omits_with_receipt(self):
        assets = self.repo / 'docs/assets'
        assets.mkdir(parents=True)
        (assets / 'original.png').write_bytes(b'not executed')
        (assets / 'alias.png').symlink_to('original.png')
        self.commit()
        with self.assertRaisesRegex(ValueError, '符号链接'):
            create_snapshot(self.repo, self.root / 'strict')
        snapshot = self.root / 'large'
        manifest = create_snapshot(self.repo, snapshot, profile='python-repository-v1')
        self.assertIn('docs/assets/alias.png', manifest['omitted'])
        self.assertFalse((snapshot / 'workspace/docs/assets/alias.png').exists())
        self.assertIn('module.py', {f['path'] for f in manifest['files']})
        self.assertEqual(verify_snapshot(snapshot), manifest)
        raw = subprocess.check_output(['git', '-C', str(self.repo), 'ls-tree', '-rz', 'HEAD'])
        self.assertEqual(parse_source_blobs(raw), git_blobs(self.repo, 'HEAD'))
        self.assertIn('docs/assets/alias.png', parse_source_blobs(raw))

    def test_code_and_test_links_remain_rejected_in_large_profile(self):
        (self.repo / 'alias.py').symlink_to('module.py')
        self.commit()
        with self.assertRaisesRegex(ValueError, '符号链接'):
            create_snapshot(self.repo, self.root / 'large', profile='python-repository-v1')

    def test_profile_byte_limit_is_opt_in_and_finite(self):
        (self.repo / 'asset.bin').write_bytes(b'x' * (20 * 1024 * 1024 + 1))
        self.commit()
        with self.assertRaisesRegex(ValueError, 'bounded-v1'):
            create_snapshot(self.repo, self.root / 'strict')
        manifest = create_snapshot(self.repo, self.root / 'large', profile='python-repository-v1')
        self.assertEqual(len(manifest['files']), 2)
        self.assertEqual(snapshot_limits('python-repository-v1'), (10000, 64 * 1024 * 1024))
        for value in (True, '', 'unlimited'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshot_limits(value)

    def test_archive_doc_links_keep_upstream_tree_proof_but_cannot_escape(self):
        from test_swebench_adapter import fixture_module
        assets = self.repo / 'docs/assets'
        assets.mkdir(parents=True)
        (assets / 'real.png').write_bytes(b'public asset')
        (assets / 'alias.png').symlink_to('real.png')
        self.commit()
        commit = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD']).decode().strip()
        tree = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD^{tree}']).decode().strip()
        row = fixture_module.prepare_fixture(self.root / 'fixture')
        row['base_commit'] = commit
        source = self.root / 'row.json'
        source.write_text(json.dumps([row]))
        dataset = self.root / 'dataset'
        import_dataset(source, dataset, dataset_name='fixture', revision='a' * 40)
        metadata = self.root / 'commit.json'
        metadata.write_text(json.dumps({'sha': commit, 'tree': {'sha': tree}}))
        archive = self.root / 'source.tar.gz'
        archive.write_bytes(subprocess.check_output(['git', '-C', str(self.repo), 'archive',
                                                     '--format=tar.gz', '--prefix=repo/', 'HEAD']))
        output = self.root / 'valid'
        receipt = prepare_archive_workspace(dataset, row['instance_id'], archive, metadata, output,
                                             snapshot_profile='python-repository-v1')
        self.assertEqual(receipt['base_git_blobs'], git_blobs(self.repo, 'HEAD'))
        self.assertFalse((output / 'baseline/workspace/docs/assets/alias.png').exists())
        outside = self.root / 'outside.png'
        outside.write_bytes(b'private outside content')
        bad = self.root / 'escape.tar.gz'
        with tarfile.open(archive) as original, tarfile.open(bad, 'w:gz') as changed:
            for member in original:
                if member.issym():
                    member.linkname = '../../../outside.png'
                changed.addfile(member, original.extractfile(member) if member.isfile() else None)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            prepare_archive_workspace(dataset, row['instance_id'], bad, metadata, self.root / 'invalid',
                                      snapshot_profile='python-repository-v1')
        self.assertEqual(outside.read_bytes(), b'private outside content')


if __name__ == '__main__':
    unittest.main()
