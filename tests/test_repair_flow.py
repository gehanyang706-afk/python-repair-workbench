"""真实容器中验证修复闭环，同时覆盖命令行协议。"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS") == "1", "需要显式启用 Docker")
class RepairFlowTests(unittest.TestCase):
    def run_case(self, replacement, expected, reason):
        run = ROOT / "runtime" / ("acceptance-" + uuid4().hex[:10])
        source = run / "source"
        shutil.copytree(ROOT / "tests/fixtures/broken_discount", source,
                        ignore=shutil.ignore_patterns("__pycache__"))

        def git(*args):
            return subprocess.run([
                "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-C", str(source), *args], check=True, capture_output=True, timeout=30).stdout

        def cli(*args, code=0):
            process = subprocess.run([sys.executable, "-m", "repair_workbench", *map(str, args)],
                                     cwd=ROOT, text=True, capture_output=True, timeout=90)
            self.assertEqual(process.returncode, code, process.stdout + process.stderr)
            return json.loads(process.stdout)

        git("init", "-q")
        git("add", "--all")
        git("commit", "-qm", "Regression fixture")
        original = (source / "pricing.py").read_text()
        baseline, candidate, verification = [run / name for name in ("baseline", "candidate", "verification")]
        cli("snapshot", "--repo", source, "--output", baseline)
        before = cli("test", "--snapshot", baseline, "--image",
                     os.environ.get("REPAIR_TEST_IMAGE", "study-agent-deepagents:0.1"), code=1)
        self.assertEqual(before["counts"]["failed"], 1)
        changes = run / "changes.json"
        changes.write_text(json.dumps({"pricing.py": replacement(original)}))
        cli("candidate", "--baseline", baseline, "--changes", changes,
            "--allow", "pricing.py", "--output", candidate)
        verdict = cli("verify", "--baseline", baseline, "--candidate", candidate,
                      "--allow", "pricing.py", "--output", verification,
                      code=0 if expected == "passed" else 1)
        self.assertEqual(verdict["status"], expected)
        self.assertEqual(verdict["reason"], reason)
        self.assertEqual((source / "pricing.py").read_text(), original)
        self.assertEqual(git("status", "--porcelain"), b"")
        self.assertTrue(json.loads((verification / "reports/result.json").read_text())["cleanup_ok"])
        self.assertTrue((verification / "patch.diff").read_text())

    def test_correct_repair(self):
        self.run_case(lambda s: s.replace("amount > 100", "amount >= 100"),
                      "passed", "passed_original_test_scope")

    def test_ineffective_repair(self):
        self.run_case(lambda s: s.replace("amount > 100", "amount > 110"),
                      "failed", "target_not_fixed")

    def test_regression(self):
        self.run_case(lambda s: "def discounted_price(amount):\n    return 90.0\n",
                      "failed", "regression")
