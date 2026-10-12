"""本机单执行器：固定提交排队，运行证据保存在独立任务目录。"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import re
import threading

from .candidate import validate_source_path
from .runner import run_tests
from .source_context import PROFILES
from .session import RepairSession
from .settings import ModelSettings
from .snapshot import create_snapshot, git
from .storage import TaskStore


class TaskService:
    def __init__(self, data_dir: Path, repo_roots: list[Path], image: str,
                 env_file: Path | None = None, model_factory=None, profile="v2"):
        if profile not in PROFILES:
            raise ValueError("unknown repair profile")
        self.profile = profile
        self.data_dir = data_dir.resolve()
        self.repo_roots = [root.resolve(strict=True) for root in repo_roots]
        if not self.repo_roots or not all(root.is_dir() for root in self.repo_roots):
            raise ValueError('至少配置一个有效仓库根目录')
        self.image = image
        self.store = TaskStore(self.data_dir / 'tasks.sqlite')
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.worker = None
        self.lock_file = None
        self.model_name = None
        self.model_factory = model_factory
        if model_factory is None:
            try:
                settings = ModelSettings.load(env_file)
                self.model_name = settings.model
                self.model_factory = settings.create_model
            except ValueError:
                pass

    def repository(self, raw: str, revision='HEAD'):
        repo = Path(raw).expanduser().resolve(strict=True)
        if not any(repo == root or root in repo.parents for root in self.repo_roots):
            raise ValueError('仓库不在服务允许的目录中')
        if repo == self.data_dir or repo in self.data_dir.parents or self.data_dir in repo.parents:
            raise ValueError('仓库与任务数据目录必须相互独立')
        toplevel = Path(git(repo, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
        if toplevel != repo:
            raise ValueError('请填写 Git 仓库根目录')
        commit = git(repo, 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
        paths = [p.decode('utf-8') for p in git(repo, 'ls-tree', '-rz', '--name-only', commit).split(b'\0') if p]
        if len(paths) > 2000:
            raise ValueError('仓库超过 2000 个文件的支持范围')
        approved = []
        for path in paths:
            try:
                validate_source_path(path, {path})
                approved.append(path)
            except ValueError:
                pass
        return {'repo': str(repo), 'commit': commit, 'source_files': sorted(approved),
                'has_tests': any(p.startswith('tests/') for p in paths)}

    def submit(self, request):
        if self.model_factory is None:
            raise ValueError('服务尚未配置模型密钥，请在启动时指定配置文件')
        info = self.repository(request['repo'], request.get('revision', 'HEAD'))
        allowed = sorted(set(request['allowed_paths']))
        if not allowed or not set(allowed) <= set(info['source_files']):
            raise ValueError('请选择当前提交中允许修改的 Python 源文件')
        if not info['has_tests']:
            raise ValueError('当前提交中没有 tests/ 测试目录')
        request = {**request, 'repo': info['repo'], 'revision': info['commit'], 'allowed_paths': allowed}
        task = self.store.create(request)
        self.wake.set()
        return task

    def start(self):
        if self.worker is not None:
            raise RuntimeError('执行器已启动')
        lock_file = (self.data_dir / 'worker.lock').open('a')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            raise RuntimeError('同一数据目录只能运行一个服务进程') from None
        self.lock_file = lock_file
        self.store.recover()
        self.worker = threading.Thread(target=self.loop, name='repair-worker', daemon=True)
        self.worker.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=90)
            if self.worker.is_alive():
                # 仍在执行时不释放进程锁，避免第二个执行器接管同一目录。
                raise RuntimeError('当前操作尚未停止；执行器锁仍被保留')
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def loop(self):
        while not self.stopping.is_set():
            task = self.store.claim()
            if task is None:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            self.execute(task)

    def execute(self, task):
        task_id, request = task['id'], task['request']
        root = self.data_dir / 'runs' / task_id
        outcome = {'baseline': None, 'agent': None}

        def cancelled():
            if self.stopping.is_set():
                self.store.cancel(task_id)
            return self.store.get(task_id)['state'] in {'cancelled', 'cancelling'}

        def progress(payload):
            phase = 'verifying' if payload['event'] == 'attempt_started' else 'diagnosing' if payload['event'] in {
                'model_started', 'attempt_finished'} else None
            self.store.event(task_id, payload, phase)

        try:
            if cancelled():
                return
            # 提交时已固定 SHA；执行时再次检查路径，避免排队期间目录改指向别处。
            self.repository(request['repo'], request['revision'])
            create_snapshot(Path(request['repo']), root / 'baseline', request['revision'])
            if cancelled():
                return
            self.store.event(task_id, {'event': 'baseline_started'}, 'baseline')
            baseline = run_tests(root / 'baseline', self.image, request['test_timeout'])
            outcome['baseline'] = baseline
            self.store.event(task_id, {'event': 'baseline_finished', 'status': baseline['status'],
                                       'counts': baseline['counts']}, result=outcome)
            if cancelled():
                return
            if baseline['status'] != 'tests_failed' or not baseline['cleanup_ok']:
                outcome['reason'] = 'no_failure_to_repair' if baseline['status'] == 'tests_passed' else 'baseline_not_reproduced'
                return
            self.store.event(task_id, {'event': 'diagnosis_started'}, 'diagnosing')
            session = RepairSession(root / 'baseline', root / 'agent', set(request['allowed_paths']),
                                    max_attempts=request['max_attempts'], max_model_calls=request['max_model_calls'],
                                    deadline_seconds=request['deadline'], test_timeout=request['test_timeout'],
                                    event_sink=progress, cancellation_check=cancelled, profile=self.profile)
            from .agent import run_agent
            session.record('model_configured', provider='dashscope', model=self.model_name)
            outcome['agent'] = run_agent(session, self.model_factory(), request['task'])
        except Exception as exc:
            self.store.finish(task_id, outcome, error_type=type(exc).__name__)
            return
        finally:
            # 即使在阶段边界取消或无失败可修复，也必须持久化终态。
            if self.store.get(task_id)['state'] not in {'completed', 'error', 'cancelled'}:
                self.store.finish(task_id, outcome)

    def artifact(self, task_id, kind, attempt=0):
        if not re.fullmatch(r'[0-9a-f]{32}', task_id) or self.store.get(task_id) is None:
            raise FileNotFoundError('任务不存在')
        root = self.data_dir / 'runs' / task_id
        if kind == 'patch':
            task = self.store.get(task_id)
            result = (task.get('result') or {}).get('agent') or {}
            relative = result.get('patch') or ''
            if not re.fullmatch(r'attempt-[0-9]{2}/verification/patch\.diff', relative):
                raise FileNotFoundError('没有通过验证的补丁')
            path = root / 'agent' / relative
        else:
            if not 0 <= attempt <= 5:
                raise ValueError('候选编号无效')
            base = root / 'baseline' if attempt == 0 else root / 'agent' / f'attempt-{attempt:02d}' / 'verification'
            path = base / 'reports/pytest.log'
        resolved = path.resolve(strict=True)
        if root.resolve() not in resolved.parents:
            raise ValueError('产物路径无效')
        return resolved
