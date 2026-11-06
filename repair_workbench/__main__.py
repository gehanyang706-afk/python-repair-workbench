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
    serve.add_argument("--backend", choices=('sqlite','mysql'), default='sqlite')
    serve.add_argument("--db-env", type=Path)
    migrate = commands.add_parser('init-db', help='初始化独立 MySQL 数据库表，不修改 SQLite')
    migrate.add_argument('--db-env', type=Path)
    worker = commands.add_parser('worker', help='运行独立 MySQL 修复 Worker')
    worker.add_argument('--db-env', type=Path)
    worker.add_argument('--data-dir', type=Path, required=True)
    worker.add_argument('--repo-root', type=Path, action='append', required=True)
    worker.add_argument('--image', required=True)
    worker.add_argument('--env-file', type=Path)
    worker.add_argument('--lease', type=float, default=15)
    worker.add_argument('--once', action='store_true')
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
    probe = commands.add_parser('probe', help='在隔离容器中运行注册诊断，不调用模型')
    probe.add_argument('--snapshot', type=Path, required=True)
    probe.add_argument('--output', type=Path, required=True)
    probe.add_argument('--image', required=True)
    probe.add_argument('--kind', choices=('cpu', 'memory', 'benchmark'), required=True)
    probe.add_argument('--workload', default='ugc-export-v1')
    probe.add_argument('--timeout', type=float, default=30)
    probe.add_argument('--transport', choices=('local', 'mcp'), default='local')
    performance = commands.add_parser('verify-performance', help='独立复验性能候选，不调用模型')
    performance.add_argument('--baseline', type=Path, required=True)
    performance.add_argument('--candidate', type=Path, required=True)
    performance.add_argument('--output', type=Path, required=True)
    performance.add_argument('--allow', action='append', required=True)
    performance.add_argument('--workload', default='ugc-export-v1')
    performance.add_argument('--timeout', type=float, default=30)
    performance.add_argument('--minimum-speedup', type=float, default=0.15)
    performance.add_argument('--max-memory-ratio', type=float, default=1.25)
    diagnose = commands.add_parser('diagnose', help='使用单Agent选择诊断实验；真实调用须单独授权')
    diagnose.add_argument('--baseline', type=Path, required=True)
    diagnose.add_argument('--output', type=Path, required=True)
    diagnose.add_argument('--allow', action='append', required=True)
    diagnose.add_argument('--task', required=True)
    diagnose.add_argument('--workload', default='ugc-export-v1')
    diagnose.add_argument('--strategy', choices=('adaptive', 'fixed'), default='adaptive')
    diagnose.add_argument('--env-file', type=Path)
    diagnose.add_argument('--model')
    diagnose.add_argument('--max-attempts', type=int, default=3)
    diagnose.add_argument('--max-model-calls', type=int, default=12)
    diagnose.add_argument('--max-probe-runs', type=int, default=6)
    diagnose.add_argument('--deadline', type=float, default=300)
    diagnose.add_argument('--timeout', type=float, default=30)
    diagnose.add_argument('--minimum-speedup', type=float, default=0.15)
    diagnose.add_argument('--max-memory-ratio', type=float, default=1.25)
    diagnose.add_argument('--skills', action='store_true', help='启用候选诊断策略的按需加载')
    diagnose.add_argument('--investigator', action='store_true', help='允许按需只读调查委派')
    diagnose.add_argument('--max-delegations', type=int, default=1)
    diagnose.add_argument('--diagnostic-transport', choices=('local', 'mcp'), default='local')
    diagnose.add_argument('--tool-concurrency', type=int, choices=(1, 2, 3, 4), default=1,
                          help='独立只读工具的图内并发上限；状态工具必须单独调用')
    diagnose.add_argument('--tool-rate-config', type=Path,
                          help='可选Redis共享业务操作速率配置；由操作方指定，不能由模型选择')
    diagnose.add_argument('--investigator-max-model-calls', type=int, default=3)
    diagnose.add_argument('--investigator-max-tools', type=int, default=12)
    diagnose.add_argument('--investigator-timeout', type=float, default=30)
    diagnose.add_argument('--main-model-reserve', type=int, default=2)
    diagnose.add_argument('--main-tool-reserve', type=int, default=4)
    diagnose.add_argument('--main-time-reserve', type=float, default=10)
    diagnose.add_argument('--allow-real-model', action='store_true',
                          help='仅在明确批准模型与批次上限后使用；环境变量不替代批准')
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
        if args.command in {'init-db','worker'}:
            from .mysql_store import DatabaseSettings, MySQLStore
            store = MySQLStore(DatabaseSettings.load(args.db_env))
            if args.command == 'init-db':
                store.migrate()
                result = {'schema_version':1,'status':'ready'}
            else:
                from .durable_worker import Worker
                model_factory = None
                if args.env_file is not None:
                    from .settings import ModelSettings
                    model_factory = ModelSettings.load(args.env_file).create_model
                executor = Worker(store,args.data_dir,args.repo_root,args.image,
                                  model_factory=model_factory,lease_seconds=args.lease)
                if args.once:
                    result = {'claimed':executor.run_once()}
                else:
                    try:
                        executor.run()
                    except KeyboardInterrupt:
                        executor.stop()
                    result = {'status':'stopped'}
            code = 0
        elif args.command == "serve":
            from .api import create_app
            from .service import TaskService
            import uvicorn
            if not 1 <= args.port <= 65535:
                raise ValueError("端口必须为 1–65535")
            if args.backend == 'mysql':
                from .mysql_service import MySQLService
                from .mysql_store import DatabaseSettings, MySQLStore
                service = MySQLService(MySQLStore(DatabaseSettings.load(args.db_env)), args.data_dir,
                                       args.repo_root,args.image,profile=args.profile)
            else:
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
        elif args.command == 'probe':
            if args.transport == 'mcp':
                from .diagnostic_mcp_client import run_mcp_probe as execute
            else:
                from .diagnostics import run_probe as execute
            result = execute(args.snapshot, args.output, args.image, args.kind,
                             args.workload, args.timeout)
            code = 0 if result['status'] == 'ok' and result['cleanup_ok'] else 2
        elif args.command == 'verify-performance':
            from .performance import verify_performance_candidate
            result = verify_performance_candidate(
                args.baseline, args.candidate, args.output, set(args.allow),
                args.workload, args.timeout, minimum_speedup=args.minimum_speedup,
                max_memory_ratio=args.max_memory_ratio)
            code = {'passed': 0, 'failed': 1, 'inconclusive': 2}[result['status']]
        elif args.command == 'diagnose':
            if not args.allow_real_model:
                raise ValueError('诊断真实模型入口默认关闭；请先批准精确模型、任务和预算，再显式启用')
            from .agent import run_agent
            from .diagnosis import DiagnosticSession
            from .settings import ModelSettings
            if not args.task.strip() or len(args.task) > 12000:
                raise ValueError('任务说明必须为1–12000字符')
            settings = ModelSettings.load(args.env_file, args.model)
            session = DiagnosticSession(
                args.baseline, args.output, set(args.allow), workload=args.workload,
                strategy=args.strategy, max_probe_runs=args.max_probe_runs,
                use_skills=args.skills, use_investigator=args.investigator,
                max_delegations=args.max_delegations,
                diagnostic_transport=args.diagnostic_transport,
                tool_concurrency=args.tool_concurrency,
                tool_rate_config=args.tool_rate_config,
                investigator_max_model_calls=args.investigator_max_model_calls,
                investigator_max_tools=args.investigator_max_tools,
                investigator_timeout_seconds=args.investigator_timeout,
                main_model_reserve=args.main_model_reserve,
                main_tool_reserve=args.main_tool_reserve,
                main_time_reserve_seconds=args.main_time_reserve,
                minimum_speedup=args.minimum_speedup, max_memory_ratio=args.max_memory_ratio,
                max_attempts=args.max_attempts, max_model_calls=args.max_model_calls,
                deadline_seconds=args.deadline, test_timeout=args.timeout)
            try:
                model = settings.create_model()
            except Exception as exc:
                result = session.save('stopped', stop_reason='model_configuration_error',
                                      error_type=type(exc).__name__)
            else:
                session.record('model_configured', provider='dashscope', model=settings.model)
                result = run_agent(session, model, args.task)
            code = {'passed': 0, 'failed': 1, 'inconclusive': 2}.get(result['status'], 2)
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
