"""这些测试检查工作台边界；fixtures 里的失败测试则属于待修复项目。"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repair_workbench.snapshot import create_snapshot, verify_snapshot
from repair_workbench.runner import classify_result


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-C", str(self.repo), *args],
            check=True, capture_output=True, timeout=30,
        ).stdout

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")

    def test_snapshot_uses_commit_and_preserves_dirty_source(self):
        source = self.repo / "app.py"
        source.write_text("value = 1\n")
        (self.repo / ".env").write_text("DUMMY=not-a-real-secret\n")
        self.commit()
        source.write_text("value = 2\n")
        (self.repo / "untracked.txt").write_text("must stay here")
        before = self.git("status", "--porcelain", "-z")
        destination = self.root / "snapshot"
        manifest = create_snapshot(self.repo, destination)
        self.assertEqual((destination / "workspace/app.py").read_text(), "value = 1\n")
        self.assertEqual(source.read_text(), "value = 2\n")
        self.assertEqual(before, self.git("status", "--porcelain", "-z"))
        self.assertFalse((destination / "workspace/untracked.txt").exists())
        self.assertFalse((destination / "workspace/.env").exists())
        self.assertEqual(manifest["omitted"], [".env"])
        self.assertEqual(verify_snapshot(destination), manifest)

    def test_reject_symlink_without_copying_external_data(self):
        outside = self.root / "outside.txt"
        outside.write_text("private fixture")
        (self.repo / "link").symlink_to(outside)
        self.commit()
        destination = self.root / "snapshot"
        with self.assertRaises(ValueError):
            create_snapshot(self.repo, destination)
        self.assertFalse(destination.exists())

    def test_reject_overwrite_and_destination_inside_source(self):
        (self.repo / "app.py").write_text("pass\n")
        self.commit()
        destination = self.root / "snapshot"
        create_snapshot(self.repo, destination)
        with self.assertRaises(FileExistsError):
            create_snapshot(self.repo, destination)
        with self.assertRaises(ValueError):
            create_snapshot(self.repo, self.repo / "nested")

    def test_modified_or_extra_file_invalidates_baseline(self):
        (self.repo / "app.py").write_text("pass\n")
        self.commit()
        destination = self.root / "snapshot"
        create_snapshot(self.repo, destination)
        (destination / "workspace/app.py").write_text("changed\n")
        with self.assertRaises(ValueError):
            verify_snapshot(destination)
        (destination / "workspace/app.py").write_text("pass\n")
        (destination / "workspace/extra.py").write_text("extra\n")
        with self.assertRaises(ValueError):
            verify_snapshot(destination)


class ResultTests(unittest.TestCase):
    def classify(self, exit_code, cases):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.xml"
            report.write_text("<testsuites><testsuite>" + cases + "</testsuite></testsuites>")
            return classify_result(exit_code, report)

    def test_real_assertion_failure_is_not_environment_error(self):
        status, counts = self.classify(1, '<testcase/><testcase><failure/></testcase>')
        self.assertEqual(status, "tests_failed")
        self.assertEqual(counts["passed"], 1)
        self.assertEqual(counts["failed"], 1)

    def test_collection_error_is_not_a_reproduced_assertion(self):
        self.assertEqual(self.classify(2, '<testcase><error/></testcase>')[0], "execution_error")

    def test_no_tests_and_all_skipped_are_not_pass(self):
        self.assertEqual(self.classify(5, "")[0], "no_tests")
        self.assertEqual(self.classify(0, '<testcase><skipped/></testcase>')[0], "no_tests")

    def test_pass_requires_report_and_consistent_exit(self):
        self.assertEqual(self.classify(0, '<testcase/>')[0], "tests_passed")
        self.assertEqual(self.classify(1, '<testcase/>')[0], "report_error")
        self.assertEqual(classify_result(0, Path("/nonexistent-repair-report.xml"))[0], "report_error")


if __name__ == "__main__":
    unittest.main()
