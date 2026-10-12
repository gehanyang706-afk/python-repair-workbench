"""修复验证：重新从基线构建环境，用原始测试核对候选源代码。"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .candidate import create_candidate
from .runner import run_tests
from .snapshot import snapshot_id, verify_snapshot


def test_outcomes(report: Path) -> dict[str, str]:
    """保留测试身份，不能只用通过数量判定：减少测试也可能让数量变好看。"""
    outcomes = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        identity = case.get("classname", "") + "::" + case.get("name", "")
        if not case.get("name") or identity in outcomes:
            raise ValueError("测试身份缺失或重复，无法可靠比较")
        status = "passed"
        for tag, value in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            if case.find(tag) is not None:
                status = value
                break
        outcomes[identity] = status
    return outcomes


def assess_repair(before: dict, after: dict, before_tests: dict, after_tests: dict) -> dict:
    """这是应用的评分规则，不让 Agent 自己宣布成功。"""
    if before.get("status") != "tests_failed" or not any(v == "failed" for v in before_tests.values()):
        return {"status": "inconclusive", "reason": "baseline_not_reproduced"}
    if not before.get("cleanup_ok") or not after.get("cleanup_ok"):
        return {"status": "inconclusive", "reason": "container_cleanup_unconfirmed"}
    if before.get("image_id") != after.get("image_id"):
        return {"status": "inconclusive", "reason": "environment_changed"}
    if after.get("status") in {"timeout", "execution_error", "report_error"}:
        return {"status": "inconclusive", "reason": after["status"]}
    if not before_tests or before_tests.keys() != after_tests.keys():
        return {"status": "failed", "reason": "test_set_changed"}
    regressed = [key for key, value in before_tests.items()
                 if value == "passed" and after_tests[key] != "passed"]
    if regressed:
        return {"status": "failed", "reason": "regression", "tests": regressed}
    unresolved = [key for key, value in before_tests.items()
                  if value == "failed" and after_tests[key] != "passed"]
    if unresolved:
        return {"status": "failed", "reason": "target_not_fixed", "tests": unresolved}
    if after.get("status") != "tests_passed":
        return {"status": "failed", "reason": "candidate_tests_not_passed"}
    return {"status": "passed", "reason": "passed_original_test_scope",
            "scope": "仅代表原始测试范围内通过；不证明所有输入或语义均正确"}


def verify_candidate(baseline: Path, candidate: Path, destination: Path,
                     allowed_paths: set[str], timeout_seconds: float = 60) -> dict:
    base = verify_snapshot(baseline)
    proposed = verify_snapshot(candidate)
    if proposed.get("base_snapshot_id") != snapshot_id(base):
        raise ValueError("候选修改与当前基线不匹配")
    before = json.loads((baseline / "reports/result.json").read_text(encoding="utf-8"))
    if before.get("snapshot_id") != snapshot_id(base):
        raise ValueError("基线运行记录与快照不匹配；旧记录请通过新基线重新生成")
    before_tests = test_outcomes(baseline / "reports/pytest.xml")
    if before.get("status") != "tests_failed" or not before.get("cleanup_ok"):
        raise ValueError("必须先成功复现原始测试失败并完成容器清理")
    base_files = {item["path"]: item["sha256"] for item in base["files"]}
    proposed_files = {item["path"]: item["sha256"] for item in proposed["files"]}
    if base_files.keys() != proposed_files.keys():
        raise ValueError("当前版本不允许候选新增或删除文件")
    # 不信任模型提交的 changed_paths：从实际文件哈希推导变化。
    changes = {relative: (candidate / "workspace" / relative).read_text(encoding="utf-8")
               for relative in base_files if base_files[relative] != proposed_files[relative]}
    create_candidate(baseline, destination, changes, allowed_paths)
    # 使用基线记录里的不可变镜像 ID，在新容器中测试重建后的目录。
    after = run_tests(destination, before["image_id"], timeout_seconds)
    try:
        after_tests = test_outcomes(destination / "reports/pytest.xml")
        verdict = assess_repair(before, after, before_tests, after_tests)
    except (OSError, ET.ParseError, ValueError):
        verdict = {"status": "inconclusive", "reason": "unusable_candidate_test_report"}
    verdict.update(base_snapshot_id=snapshot_id(base), candidate_snapshot_id=after["snapshot_id"],
                   changed_paths=sorted(changes), before=before["counts"], after=after["counts"])
    (destination / "verification.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return verdict
