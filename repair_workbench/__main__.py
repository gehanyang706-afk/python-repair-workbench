"""工作台命令行：快照、测试、候选补丁及独立验证。"""
import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .source_context import PROFILES
from .candidate import create_candidate
from .runner import run_tests
from .snapshot import create_snapshot
from .verification import verify_candidate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本机任务工作台")
    serve.add_argument("--repo-root", type=Path, action="append", required=True)
    serve.add_argument("--data-dir", type=Path, default=Path("runtime/workbench"))
    serve.add_argument("--image", required=True)
    serve.add_argument("--env-file", type=Path)
    serve.add_argument("--profile", choices=PROFILES, default="v2")
    serve.add_argument("--port", type=int, default=8765)
    snapshot = commands.add_parser("snapshot", help="从 Git 提交导出快照")
    snapshot.add_argument("--repo", type=Path, required=True)
    snapshot.add_argument("--revision", default="HEAD")
    snapshot.add_argument("--output", type=Path, required=True)
    test = commands.add_parser("test", help="在本地 Docker 镜像中运行原始测试")
    test.add_argument("--snapshot", type=Path, required=True)
    test.add_argument("--image", required=True)
    test.add_argument("--timeout", type=float, default=60)
    repair = commands.add_parser("repair", help="使用 Deep Agents 诊断已有失败基线并验证候选")
    repair.add_argument("--baseline", type=Path, required=True)
    repair.add_argument("--output", type=Path, required=True)
    repair.add_argument("--allow", action="append", required=True)
    repair.add_argument("--task", required=True)
    repair.add_argument("--env-file", type=Path)
    repair.add_argument("--model")
    repair.add_argument("--profile", choices=PROFILES, default="v2")
    repair.add_argument("--max-attempts", type=int, default=3)
    repair.add_argument("--max-model-calls", type=int, default=12)
    repair.add_argument("--deadline", type=float, default=180)
    repair.add_argument("--timeout", type=float, default=30)
    for name in ("candidate", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--baseline", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--allow", action="append", required=True,
                             help="应用批准修改的源文件，可重复指定")
        if name == "candidate":
            command.add_argument("--changes", type=Path, required=True,
                                 help="JSON 对象：相对文件路径映射到完整新内容")
        else:
            command.add_argument("--candidate", type=Path, required=True)
            command.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .api import create_app
            from .service import TaskService
            import uvicorn
            if not 1 <= args.port <= 65535:
                raise ValueError("端口必须为 1–65535")
            service = TaskService(args.data_dir, args.repo_root, args.image, args.env_file, profile=args.profile)
            uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, access_log=False)
            return 0
        elif args.command == "snapshot":
            result = create_snapshot(args.repo, args.output, args.revision)
            code = 0
        elif args.command == "test":
            result = run_tests(args.snapshot, args.image, args.timeout)
            code = (0 if result["status"] == "tests_passed" else
                    1 if result["status"] in {"tests_failed", "no_tests"} else 2)
            if not result["cleanup_ok"]:
                code = 2
        elif args.command == "candidate":
            changes = json.loads(args.changes.read_text(encoding="utf-8"))
            if not isinstance(changes, dict):
                raise ValueError("changes 必须是文件路径到完整内容的 JSON 对象")
            result = create_candidate(args.baseline, args.output, changes, set(args.allow))
            code = 0
        elif args.command == "verify":
            result = verify_candidate(args.baseline, args.candidate, args.output,
                                      set(args.allow), args.timeout)
            code = {"passed": 0, "failed": 1, "inconclusive": 2}[result["status"]]
        else:
            from .agent import run_agent
            from .session import RepairSession
            from .settings import ModelSettings
            if not args.task.strip() or len(args.task) > 12000:
                raise ValueError("任务说明必须为 1–12000 字符")
            settings = ModelSettings.load(args.env_file, args.model)
            session = RepairSession(args.baseline, args.output, set(args.allow),
                                    max_attempts=args.max_attempts, max_model_calls=args.max_model_calls,
                                    deadline_seconds=args.deadline, test_timeout=args.timeout, profile=args.profile)
            try:
                model = settings.create_model()
            except Exception as exc:
                result = session.save("stopped", stop_reason="model_configuration_error",
                                      error_type=type(exc).__name__)
            else:
                session.record("model_configured", provider="dashscope", model=settings.model)
                result = run_agent(session, model, args.task)
            code = {"passed": 0, "failed": 1, "inconclusive": 2}.get(result["status"], 2)
    except (OSError, ValueError, KeyError, TypeError, ImportError, ET.ParseError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
