"""从 Git 提交创建独立副本，不读取未提交修改。"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath


def git(repo: Path, *args: str) -> bytes:
    """用参数列表调用 Git；不经过 Shell，也不改全局 Git 配置。"""
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30
    ).stdout


def create_snapshot(repo: Path, destination: Path, revision: str = "HEAD") -> dict:
    """输入提交，输出 workspace/ 与 manifest.json；目标目录必须不存在。

    用 Git 对象里的内容，而不是 shutil.copytree(repo)：后者会把未提交改动、
    .git、虚拟环境等一起带走。记录实际提交和文件哈希，才能说明测试的是什么。
    快照只接受普通文件，拒绝符号链接和子模块，不自动跟随它们读取外部内容。
    """
    repo = repo.resolve(strict=True)
    destination = destination.resolve()
    if destination == repo or repo in destination.parents:
        raise ValueError("快照目录必须位于原项目之外")
    if destination.exists():
        raise FileExistsError("不能覆盖已有快照")
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    entries = git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0")
    files, omitted, total_size = [], [], 0
    for entry in filter(None, entries):
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        relative = raw_path.decode("utf-8")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError("快照包含不允许的路径")
        if any(part in {".git", ".venv", "venv", "node_modules", "__pycache__"}
               or part.startswith(".env") for part in path.parts):
            omitted.append(relative)
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError(f"不支持符号链接或子模块：{relative}")
        size = int(git(repo, "cat-file", "-s", oid))
        total_size += size
        if len(files) >= 2000 or total_size > 20 * 1024 * 1024:
            raise ValueError("快照限制为 2000 个文件、总计 20 MiB")
        files.append((relative, mode, oid))

    # 所有路径检查完以后才建立目录；任何异常都不碰原项目。
    destination.mkdir(parents=True, exist_ok=False)
    workspace = destination / "workspace"
    workspace.mkdir()
    manifest = {"source_repo": str(repo), "commit": commit, "files": [], "omitted": omitted}
    for relative, mode, oid in files:
        content = git(repo, "cat-file", "blob", oid)
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if mode == "100755" else 0o644)
        manifest["files"].append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def verify_snapshot(destination: Path) -> dict:
    """运行前再核对内容：提交编号不能被用来描述后来被改过的副本。"""
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    workspace = destination / "workspace"
    expected = {item["path"]: item["sha256"] for item in manifest["files"]}
    actual = {}
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise ValueError("副本中出现符号链接")
        if path.is_file():
            actual[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("副本已改变，请建立新的候选版本，不能覆盖原始测试基线")
    return manifest


def snapshot_id(manifest: dict) -> str:
    """内容标识与目录位置无关；候选副本不能只沿用原始提交编号冒充原版。"""
    content = {"commit": manifest["commit"], "files": sorted(manifest["files"], key=lambda item: item["path"])}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
