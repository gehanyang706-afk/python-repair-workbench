"""候选补丁边界验收。只操作临时目录；此文件不启动 Docker 或模型。"""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from repair_workbench.candidate import create_candidate
from repair_workbench.snapshot import create_snapshot, snapshot_id, verify_snapshot
from repair_workbench.verification import assess_repair, verify_candidate


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "pricing.py").write_text("def price(x):\n    return x\n")
        (self.repo / "other.py").write_text("value = 1\n")
        (self.repo / "tests/test_price.py").write_text("def test_price():\n    assert False\n")
        (self.repo / "conftest.py").write_text("# original\n")
        for args in (("init", "-q"), ("add", "--all"), ("commit", "-qm", "fixture")):
            subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-C", str(self.repo), *args], check=True, capture_output=True, timeout=30,
            )
        self.base = self.root / "baseline"
        self.manifest = create_snapshot(self.repo, self.base)
        self.fixed = "def price(x):\n    return x * 0.9\n"

    def create(self, changes=None, allowed=None, destination=None):
        return create_candidate(self.base, destination or self.root / "candidate",
                                {"pricing.py": self.fixed} if changes is None else changes,
                                {"pricing.py"} if allowed is None else allowed)

    def test_patch_is_applicable_and_original_is_preserved(self):
        original = (self.repo / "pricing.py").read_bytes()
        candidate = self.root / "candidate"
        manifest = self.create()
        self.assertEqual((self.repo / "pricing.py").read_bytes(), original)
        self.assertEqual((self.base / "workspace/pricing.py").read_bytes(), original)
        self.assertEqual(verify_snapshot(candidate), manifest)
        self.assertNotEqual(snapshot_id(manifest), snapshot_id(self.manifest))
        export = self.root / "apply-check"
        shutil.copytree(self.base / "workspace", export)
        patch = candidate / "patch.diff"
        for extra in (("--check",), ()):
            subprocess.run(["git", "-C", str(export), "apply", *extra, str(patch)],
                           check=True, capture_output=True, timeout=30)
        self.assertEqual((export / "pricing.py").read_text(), self.fixed)
        self.assertEqual((export / "tests/test_price.py").read_bytes(),
                         (self.base / "workspace/tests/test_price.py").read_bytes())

    def test_test_and_config_edits_are_forbidden_even_if_allowlisted(self):
        for relative in ("tests/test_price.py", "conftest.py", "pyproject.toml", "setup.py"):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                self.create({relative: "pass\n"}, {relative})
        self.assertFalse((self.root / "candidate").exists())

    def test_unapproved_source_is_forbidden(self):
        with self.assertRaises(ValueError):
            self.create({"other.py": "value = 2\n"})

    def test_path_escape_is_forbidden(self):
        for relative in ("../outside.py", "/tmp/outside.py", "a/../pricing.py", "./pricing.py"):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                self.create({relative: "pass\n"}, {relative})

    def test_empty_patch_is_not_a_candidate(self):
        with self.assertRaises(ValueError):
            self.create({})
        with self.assertRaises(ValueError):
            self.create({"pricing.py": (self.base / "workspace/pricing.py").read_text()})

    def test_existing_candidate_cannot_be_overwritten(self):
        self.create()
        with self.assertRaises(FileExistsError):
            self.create()

    def test_candidate_cannot_be_created_inside_original_or_baseline(self):
        for root in (self.repo, self.base):
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.create(destination=root / "candidate")

    def test_rehashed_test_tampering_is_rejected_before_execution(self):
        candidate = self.root / "candidate"
        manifest = self.create()
        # 模拟提交方改了测试，并更新自己的 manifest；验证器仍必须拒绝它。
        changed = candidate / "workspace/tests/test_price.py"
        changed.write_text("def test_price():\n    assert True\n")
        for item in manifest["files"]:
            if item["path"] == "tests/test_price.py":
                item["sha256"] = hashlib.sha256(changed.read_bytes()).hexdigest()
        (candidate / "manifest.json").write_text(json.dumps(manifest))
        reports = self.base / "reports"
        reports.mkdir()
        # 这里只是单元测试的构造数据，不是真实验收结果。
        (reports / "result.json").write_text(json.dumps({
            "snapshot_id": snapshot_id(self.manifest), "status": "tests_failed",
            "cleanup_ok": True, "image_id": "unused-in-this-test",
        }))
        (reports / "pytest.xml").write_text(
            '<testsuites><testsuite><testcase classname="test_price" name="test_price">'
            '<failure/></testcase></testsuite></testsuites>')
        with self.assertRaises(ValueError):
            verify_candidate(self.base, candidate, self.root / "verification", {"pricing.py"})
        self.assertFalse((self.root / "verification").exists())


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.before = {"status": "tests_failed", "image_id": "same-image", "cleanup_ok": True}
        self.after = {**self.before, "status": "tests_passed"}
        self.before_tests = {"target": "failed", "regression": "passed"}
        self.after_tests = {"target": "passed", "regression": "passed"}

    def assess(self, before=None, after=None, tests=None):
        return assess_repair(self.before if before is None else before,
                             self.after if after is None else after, self.before_tests,
                             self.after_tests if tests is None else tests)

    def test_true_fix_passes_original_scope(self):
        self.assertEqual(self.assess()["status"], "passed")

    def test_missing_test_fails(self):
        self.assertEqual(self.assess(tests={"target": "passed"})["reason"], "test_set_changed")

    def test_skipping_target_does_not_fix_it(self):
        tests = {**self.after_tests, "target": "skipped"}
        self.assertEqual(self.assess(tests=tests)["reason"], "target_not_fixed")

    def test_regression_fails_even_when_target_is_fixed(self):
        tests = {**self.after_tests, "regression": "failed"}
        self.assertEqual(self.assess(tests=tests)["reason"], "regression")

    def test_environment_change_is_inconclusive(self):
        self.assertEqual(self.assess(after={**self.after, "image_id": "another"})["reason"], "environment_changed")

    def test_unreproduced_baseline_is_inconclusive(self):
        self.assertEqual(self.assess(before={**self.before, "status": "tests_passed"})["reason"], "baseline_not_reproduced")

    def test_timeout_and_cleanup_failure_are_not_success(self):
        for after in ({**self.after, "status": "timeout"}, {**self.after, "cleanup_ok": False}):
            with self.subTest(after=after):
                self.assertEqual(self.assess(after=after)["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
