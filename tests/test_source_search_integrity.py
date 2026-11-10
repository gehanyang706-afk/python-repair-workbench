"""Bound full-tree scans per operation without relaxing source integrity."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from repair_workbench.edits import EditRejected
from repair_workbench.session import RepairSession
from repair_workbench.snapshot import create_snapshot, snapshot_id, verify_snapshot
from test_agent import fixture


class SearchIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        original = fixture(self.root)
        repo = self.root / 'source'
        for index in range(32):
            (repo / ('module_%02d.py' % index)).write_text('def public_call():\n    return 1\n')
        subprocess.run(['git', '-C', str(repo), 'add', '--all'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgsign=false', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'Additional static source files'], check=True, capture_output=True)
        self.baseline = self.root / 'expanded'
        manifest = create_snapshot(repo, self.baseline)
        shutil.copytree(original / 'reports', self.baseline / 'reports')
        result = self.baseline / 'reports/result.json'
        result.write_text(json.dumps({**json.loads(result.read_text()), 'snapshot_id': snapshot_id(manifest)}))
        self.session = RepairSession(self.baseline, self.root / 'agent', {'pricing.py'},
                                     profile='simple_context')

    def test_no_match_search_checks_whole_snapshot_twice_instead_of_per_file(self):
        with patch('repair_workbench.session.verify_snapshot', wraps=verify_snapshot) as verify:
            result = self.session.search_source('ABSENT_SEARCH_SENTINEL')
        self.assertEqual(result['matches'], [])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.session.tool_calls, 1)

    def test_mid_search_tree_drift_is_rejected_without_returning_partial_hits(self):
        source = self.session._source_text
        def read(path):
            text = source(path)
            if path == 'module_00.py':
                (self.baseline / 'workspace/module_31.py').write_text('def public_call(): return "corrupted"\n')
            return text
        with patch.object(self.session, '_source_text', side_effect=read):
            with self.assertRaisesRegex(EditRejected, 'baseline_version_changed'):
                self.session.search_source('public_call')
        self.assertFalse(any('corrupted' in str(ref) for ref in self.session.source_refs.values()))

    def test_each_read_checks_file_content_even_within_validated_operation(self):
        with self.session._source_read_operation():
            target = self.baseline / 'workspace/pricing.py'
            target.write_text('UNTRUSTED_DRIFT_SENTINEL\n')
            with self.assertRaisesRegex(EditRejected, 'baseline_version_changed'):
                self.session._source_text('pricing.py')
            # Restore only to allow the enclosing post-check to complete.
            target.write_bytes((self.root / 'source/pricing.py').read_bytes())

    def test_read_scope_is_thread_local_and_cannot_skip_another_threads_check(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def worker():
            try:
                with self.session._source_read_operation():
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test synchronization timeout')
            except BaseException as exc:
                errors.append(exc)
        with patch('repair_workbench.session.verify_snapshot', wraps=verify_snapshot) as verify:
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.session._source_text('pricing.py')
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(verify.call_count, 3)

    def test_rewritten_manifest_cannot_rebind_an_existing_session(self):
        path = self.baseline / 'workspace/pricing.py'
        path.write_text('def changed(): return 0\n')
        manifest_path = self.baseline / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        next(row for row in manifest['files'] if row['path'] == 'pricing.py')['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        verify_snapshot(self.baseline)  # Locally self-consistent, but a different original version.
        with self.assertRaisesRegex(EditRejected, 'baseline_version_changed'):
            self.session.read_source('pricing.py')


if __name__ == '__main__':
    unittest.main()
