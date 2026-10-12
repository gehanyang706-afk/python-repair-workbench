"""候选补丁：从不变的基线派生，只允许修改事先批准的源文件。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .snapshot import snapshot_id, verify_snapshot


def validate_source_path(relative: str, allowed_paths: set[str]) -> None:
    """批准列表来自应用/用户，不由模型自行提供；测试与配置不能通过批准绕过。"""
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or ".." in path.parts or "\\" in relative or "\0" in relative):
        raise ValueError("补丁路径必须是规范的项目内相对路径")
    protected = (
        path.suffix != ".py" or any(part in {"tests", "test"} or part.startswith(".") for part in path.parts)
        or path.name.startswith("test_") or path.name.endswith("_test.py")
        or path.name in {"conftest.py", "setup.py"}
    )
    if protected:
        raise ValueError(f"不能修改测试、配置或非 Python 源文件：{relative}")
    if relative not in allowed_paths:
        raise ValueError(f"文件不在批准的修改范围：{relative}")


def make_patch(workspace: Path, changes: dict[str, str], parent: Path) -> bytes:
    """用临时 Git 索引计算标准补丁，支持文件末尾没有换行的情况。

    临时仓库不会放进待测试的 workspace，也不会提交或修改用户的仓库。
    禁用全局 Git 配置和外部 diff/filter 配置来源，避免继承机器上的个性化行为。
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    with tempfile.TemporaryDirectory(prefix="patch-", dir=parent) as directory:
        repo = Path(directory)
        shutil.copytree(workspace, repo, dirs_exist_ok=True)
        def git(*arguments):
            return subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false",
                 "-C", str(repo), *arguments],
                env=env, capture_output=True, check=True, timeout=30,
            ).stdout
        git("init", "-q")
        git("add", "--force", "--all")
        for relative, content in changes.items():
            (repo / relative).write_bytes(content.encode("utf-8"))
        return git("diff", "--binary", "--no-ext-diff", "--no-textconv",
                   "--src-prefix=a/", "--dst-prefix=b/")


def create_candidate(baseline: Path, destination: Path, changes: dict[str, str],
                     allowed_paths: set[str]) -> dict:
    baseline = baseline.resolve(strict=True)
    destination = destination.resolve()
    base = verify_snapshot(baseline)
    source_repo = Path(base["source_repo"]).resolve()
    if any(destination == root or root in destination.parents for root in (baseline, source_repo)):
        raise ValueError("候选目录必须位于基线和原项目之外")
    if destination.exists():
        raise FileExistsError("不能覆盖已有候选记录")
    tracked = {item["path"] for item in base["files"]}
    actual_changes = {}
    for relative, content in changes.items():
        validate_source_path(relative, allowed_paths)
        if relative not in tracked:
            raise ValueError("当前版本只支持修改现有文件，不支持新增或删除")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("候选文件必须是小于 1 MiB 的 UTF-8 文本")
        if content.encode("utf-8") != (baseline / "workspace" / relative).read_bytes():
            actual_changes[relative] = content
    if not actual_changes:
        raise ValueError("空补丁不能作为修复候选")

    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(baseline / "workspace", destination / "workspace")
    for relative, content in actual_changes.items():
        (destination / "workspace" / relative).write_bytes(content.encode("utf-8"))
    files = [{"path": item["path"], "sha256": hashlib.sha256(
        (destination / "workspace" / item["path"]).read_bytes()).hexdigest()} for item in base["files"]]
    manifest = {**base, "kind": "candidate", "base_snapshot_id": snapshot_id(base),
                "files": files, "changed_paths": sorted(actual_changes)}
    patch = make_patch(baseline / "workspace", actual_changes, destination)
    if not patch:
        raise ValueError("文件变化没有产生有效 Git 补丁，拒绝交付")
    (destination / "patch.diff").write_bytes(patch)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
