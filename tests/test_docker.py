"""显式启用的真实容器验收：不下载镜像，不访问网络，不调用模型。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from repair_workbench.runner import run_tests
from repair_workbench.snapshot import create_snapshot


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS") == "1", "设置 RUN_DOCKER_TESTS=1 才运行真实容器验收")
class DockerTests(unittest.TestCase):
    def run_fixture(self, content, timeout=15):
        runtime = Path(__file__).resolve().parents[1] / "runtime"
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            root = Path(directory)
            repo = root / "source"
            (repo / "tests").mkdir(parents=True)
            test_file = repo / "tests/test_case.py"
            test_file.write_text(content)
            for args in (("init", "-q"), ("add", "tests/test_case.py"), ("commit", "-qm", "fixture")):
                subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                     "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "-C", str(repo), *args], check=True, capture_output=True, timeout=30,
                )
            snapshot = root / "baseline"
            create_snapshot(repo, snapshot)
            result = run_tests(snapshot, os.environ.get("REPAIR_TEST_IMAGE", "study-agent-deepagents:0.1"), timeout)
            self.assertEqual(test_file.read_text(), content)
            self.assertTrue(result["cleanup_ok"], "本次容器没有被确认清理")
            return result

    def test_normal_pass(self):
        result = self.run_fixture("def test_ok():\n    assert 1 + 1 == 2\n")
        self.assertEqual(result["status"], "tests_passed")
        self.assertEqual(result["counts"]["passed"], 1)

    def test_all_skipped_does_not_pass(self):
        result = self.run_fixture("import pytest\ndef test_skip():\n    pytest.skip('fixture')\n")
        self.assertEqual(result["status"], "no_tests")

    def test_import_error_is_execution_error(self):
        result = self.run_fixture("import nonexistent_package_for_repair_fixture\n")
        self.assertEqual(result["status"], "execution_error")

    def test_timeout_cleans_container(self):
        result = self.run_fixture("import time\ndef test_slow():\n    time.sleep(30)\n", timeout=2)
        self.assertEqual(result["status"], "timeout")


if __name__ == "__main__":
    unittest.main()
