"""容器中运行 pytest，分别记录测试失败与执行失败。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from .snapshot import verify_snapshot, snapshot_id


def classify_result(returncode: int, report: Path) -> tuple[str, dict]:
    """不能把非零退出都称为 bug，也不能把零退出都称为验证通过。"""
    try:
        cases = list(ET.parse(report).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return "report_error" if returncode == 0 else "execution_error", {}
    counts = {
        "total": len(cases),
        "failed": sum(c.find("failure") is not None for c in cases),
        "errors": sum(c.find("error") is not None for c in cases),
        "skipped": sum(c.find("skipped") is not None for c in cases),
    }
    counts["passed"] = counts["total"] - counts["failed"] - counts["errors"] - counts["skipped"]
    if returncode == 5:
        return "no_tests", counts
    if counts["errors"] or returncode not in {0, 1}:
        return "execution_error", counts
    if returncode == 1 and counts["failed"]:
        return "tests_failed", counts
    if returncode == 0 and not counts["failed"]:
        return ("tests_passed" if counts["passed"] else "no_tests"), counts
    return "report_error", counts


def run_tests(snapshot: Path, image: str, timeout_seconds: float = 60) -> dict:
    """仅运行固定的 tests/ 目录。不接受模型生成的任意命令。

    Docker 限制进程资源与宿主机访问；当前仅支持自有受控项目。
    XML 来自测试进程，本模块不是面向恶意项目的防作弊验证器。
    """
    snapshot = snapshot.resolve(strict=True)
    workspace = snapshot / "workspace"
    if not workspace.is_dir() or not (workspace / "tests").is_dir():
        raise ValueError("快照必须包含 workspace/tests 目录")
    if timeout_seconds <= 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image):
        raise ValueError("镜像名或超时参数无效")
    if "," in str(snapshot):
        raise ValueError("暂不支持路径中包含逗号")
    manifest = verify_snapshot(snapshot)
    # 先确认镜像已在本机，再用不可变镜像 ID 执行，绝不自动拉取。
    image_id = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    reports = snapshot / "reports"
    reports.mkdir(exist_ok=False)  # 重跑必须建立新的记录，不能覆盖旧结果。
    log_path, xml_path = reports / "pytest.log", reports / "pytest.xml"
    name = "repair-run-" + uuid.uuid4().hex[:16]
    command = [
        "docker", "run", "--name", name, "--pull", "never",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
        "--pids-limit", "64", "--memory", "256m", "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
        "--mount", f"type=bind,src={workspace},dst=/workspace,readonly",
        "--mount", f"type=bind,src={reports},dst=/reports",
        "--workdir", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "HOME=/tmp",
        "--entrypoint", "python", image_id, "-m", "pytest", "tests",
        "-q", "-p", "no:cacheprovider", "--junitxml=/reports/pytest.xml",
    ]
    started = time.monotonic()
    result = {"commit": manifest["commit"], "snapshot_id": snapshot_id(manifest),
              "image_id": image_id, "command": command,
              "status": "execution_error", "returncode": None, "counts": {},
              "timeout_seconds": timeout_seconds, "cleanup_ok": False}
    try:
        with log_path.open("w", encoding="utf-8") as log:
            # 输出直接落盘，避免一次性把完整测试日志读入内存。
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=timeout_seconds, check=False)
        result["returncode"] = completed.returncode
        result["status"], result["counts"] = classify_result(completed.returncode, xml_path)
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
    except OSError as exc:
        result["error_type"] = type(exc).__name__
    finally:
        # 杀掉 docker 客户端不保证容器已经停止。只清理本次生成的唯一容器名。
        try:
            cleanup = subprocess.run(["docker", "rm", "-f", name],
                                     capture_output=True, text=True, timeout=15)
            result["cleanup_ok"] = cleanup.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (reports / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return result
