"""容器中运行 pytest，分别记录测试失败与执行失败。"""
from __future__ import annotations

import json
import hashlib
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


def validate_node_report(path, returncode, counts):
    """Validate exact pytest identities against the separate JUnit summary.

    Reports come from a controlled project process; this is not authentication
    of malicious Python code or an attestation of container integrity.
    """
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('missing_or_oversized_node_report')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_node_report_field')
            result[key] = value
        return result
    report = json.loads(path.read_text(), object_pairs_hook=unique)
    if (type(report) is not dict or set(report) != {'schema_version', 'exitcode', 'collected',
            'outcomes', 'collection_errors', 'subtest_counts'} or type(report['schema_version']) is not int
            or report['schema_version'] != 1 or type(report['exitcode']) is not int
            or report['exitcode'] != returncode):
        raise ValueError('invalid_node_report_protocol')
    collected, outcomes = report['collected'], report['outcomes']
    if (type(collected) is not list or not 1 <= len(collected) <= 20000
            or any(type(node) is not str or not 1 <= len(node) <= 1024 for node in collected)
            or len(set(collected)) != len(collected) or type(outcomes) is not dict
            or set(outcomes) != set(collected) or type(report['collection_errors']) is not list
            or report['collection_errors']
            or any(type(status) is not str or status not in {'passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed'}
                   for status in outcomes.values())):
        raise ValueError('incomplete_or_invalid_node_inventory')
    subtests = report['subtest_counts']
    if (type(subtests) is not dict or not set(subtests) <= set(collected)
            or any(type(v) is not dict or not set(v) <= {'passed', 'failed', 'skipped'}
                   or any(type(n) is not int or n < 1 for n in v.values()) for v in subtests.values())):
        raise ValueError('invalid_subtest_inventory')
    if any((counts.get('failed') and outcomes[node] != 'failed')
           or (counts.get('skipped') and outcomes[node] == 'passed') for node, counts in subtests.items()):
        raise ValueError('subtest_failure_was_discarded')
    observed = {status: sum(v == status for v in outcomes.values())
                for status in ('passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed')}
    if (counts.get('total') != len(collected) or counts.get('passed') != observed['passed'] + observed['xpassed']
            or counts.get('failed') != observed['failed'] or counts.get('errors') != observed['error']
            or counts.get('skipped') != observed['skipped'] + observed['xfailed']):
        raise ValueError('node_inventory_junit_mismatch')
    return {'outcomes': outcomes, 'status_counts': observed,
            'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def run_tests(snapshot: Path, image: str, timeout_seconds: float = 60, *, execution_control=None,
              collect_nodeids=False, test_pids_limit=64) -> dict:
    """仅运行固定的 tests/ 目录。不接受模型生成的任意命令。

    Docker 限制进程资源与宿主机访问；当前仅支持自有受控项目。
    XML 来自测试进程，本模块不是面向恶意项目的防作弊验证器。
    """
    snapshot = snapshot.resolve(strict=True)
    if type(collect_nodeids) is not bool:
        raise ValueError('collect_nodeids must be a strict application-side boolean')
    if type(test_pids_limit) is not int or not 64 <= test_pids_limit <= 256:
        raise ValueError('test_pids_limit must be an application-side integer in 64..256')
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
    if execution_control:
        execution_control.check()
        execution_control.container(name, 'reserved')
    command = [
        "docker", "run", "--name", name, "--pull", "never",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
        "--pids-limit", str(test_pids_limit), "--memory", "256m", "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
        "--mount", f"type=bind,src={workspace},dst=/workspace,readonly",
        "--mount", f"type=bind,src={reports},dst=/reports",
        "--workdir", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "HOME=/tmp",
        "--entrypoint", "python", image_id, "-m", "pytest", "tests",
        "-q", "-p", "no:cacheprovider", "--junitxml=/reports/pytest.xml",
    ]
    if collect_nodeids:
        worker = Path(__file__).with_name('test_receipt_worker.py').resolve(strict=True)
        worker_hash = hashlib.sha256(worker.read_bytes()).hexdigest()
        position = command.index('--workdir')
        command[position:position] = ['--mount', f'type=bind,src={worker},dst=/trusted/test_receipt_worker.py,readonly']
        position = command.index('--entrypoint')
        command[position:] = ['--entrypoint', 'python', image_id, '-I', '/trusted/test_receipt_worker.py']
    started = time.monotonic()
    result = {"commit": manifest["commit"], "snapshot_id": snapshot_id(manifest),
              "image_id": image_id, "command": command,
              "status": "execution_error", "returncode": None, "counts": {},
              "timeout_seconds": timeout_seconds, "cleanup_ok": False}
    result['test_execution_policy'] = {'pids_limit': test_pids_limit, 'memory_bytes': 268435456,
                                     'cpus': 1, 'network': 'none', 'auto_pull': False}
    try:
        with log_path.open("w", encoding="utf-8") as log:
            # 输出直接落盘，避免一次性把完整测试日志读入内存。
            if execution_control:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                try:
                    while process.poll() is None:
                        execution_control.check()
                        if time.monotonic() - started >= timeout_seconds:
                            raise subprocess.TimeoutExpired(command, timeout_seconds)
                        time.sleep(.05)
                    completed = process
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
            else:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=timeout_seconds, check=False)
        result["returncode"] = completed.returncode
        result["status"], result["counts"] = classify_result(completed.returncode, xml_path)
        if collect_nodeids:
            try:
                checked = validate_node_report(reports / 'nodeids.json', completed.returncode, result['counts'])
                if (snapshot_id(verify_snapshot(snapshot)) != result['snapshot_id']
                        or hashlib.sha256(worker.read_bytes()).hexdigest() != worker_hash):
                    raise ValueError('original_test_inputs_changed')
                result['test_inventory'] = checked
                result['test_receipt_worker_sha256'] = worker_hash
            except (OSError, ValueError, TypeError):
                result['status'] = 'report_error'
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
        if execution_control and result['cleanup_ok']:
            execution_control.container(name, 'cleaned')
    return result
