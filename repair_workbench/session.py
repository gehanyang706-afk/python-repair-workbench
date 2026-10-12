"""修复任务的应用状态与受控操作，不依赖模型或 Agent 框架。"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path, PurePosixPath

from .candidate import create_candidate, validate_source_path
from .edits import EditRejected, apply_edits, syntax_errors, content_id
from .source_context import PROFILES, outline, receipt, units
from .repair_contract import validate_plan, review_changes
from .snapshot import snapshot_id, verify_snapshot
from .verification import test_outcomes, verify_candidate


class BudgetExceeded(RuntimeError):
    """任务资源预算耗尽。"""


class RepairSession:
    def __init__(self, baseline: Path, output: Path, allowed_paths: set[str], *,
                 max_attempts=3, max_model_calls=12, deadline_seconds=180,
                 test_timeout=30, event_sink=None, cancellation_check=None, profile="v2"):
        if profile not in PROFILES:
            raise ValueError("unknown repair profile")
        self.profile = profile
        self.context_enabled, self.review_enabled = PROFILES[profile]
        self.task = ""
        self.repair_plan = None
        self.read_receipts = {}
        self.seen_lines = set()
        self.context_stats = {"reads": 0, "returned_characters": 0, "unique_lines": 0, "repeated_reads": 0, "suppressed_reads": 0, "invalid_requests": 0}
        self.event_sink = event_sink
        self.cancellation_check = cancellation_check
        self.baseline = baseline.resolve(strict=True)
        self.output = output.resolve()
        self.manifest = verify_snapshot(self.baseline)
        self.allowed_paths = frozenset(allowed_paths)
        self.files = {f['path'] for f in self.manifest['files']}
        if not allowed_paths:
            raise ValueError('必须明确批准至少一个源文件')
        for relative in allowed_paths:
            validate_source_path(relative, set(allowed_paths))
            if relative not in self.files:
                raise ValueError('批准文件必须存在于基线')
        for root in (self.baseline, Path(self.manifest['source_repo']).resolve()):
            if self.output == root or root in self.output.parents:
                raise ValueError('任务目录必须位于基线和原仓库之外')
        if not 1 <= max_attempts <= 5 or not 1 <= max_model_calls <= 30:
            raise ValueError('修复次数须为 1–5，模型调用次数须为 1–30')
        if any(not math.isfinite(v) or v <= 0 for v in (deadline_seconds, test_timeout)):
            raise ValueError('超时必须为有限正数')
        before = json.loads((self.baseline / 'reports/result.json').read_text())
        outcomes = test_outcomes(self.baseline / 'reports/pytest.xml')
        if (before.get('snapshot_id') != snapshot_id(self.manifest)
                or before.get('status') != 'tests_failed' or not before.get('cleanup_ok')
                or not any(v == 'failed' for v in outcomes.values())):
            raise ValueError('必须提供内容一致、已复现断言失败并完成清理的基线')
        self.before = before
        self.max_attempts = max_attempts
        self.max_model_calls = max_model_calls
        self.deadline_seconds = deadline_seconds
        self.test_timeout = test_timeout
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.attempts = []
        self.candidate_fingerprints = {}
        self.last_model_observation = None
        self.model_calls = 0
        self.tool_calls = 0
        self.token_usage = {'input_tokens': 0, 'output_tokens': 0, 'reports': 0}
        self.output.mkdir(parents=True, exist_ok=False)
        self.record('session_started', baseline_id=snapshot_id(self.manifest),
                    allowed_paths=sorted(self.allowed_paths))
        self.save('running')

    def record(self, event, **payload):
        with self.lock:
            entry = {'event': event, 'elapsed_seconds': round(time.monotonic() - self.started, 3), **payload}
            with (self.output / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
            if self.event_sink:
                self.event_sink(entry)

    def check_budget(self):
        if self.cancellation_check and self.cancellation_check():
            raise BudgetExceeded('cancel_requested')
        if time.monotonic() - self.started >= self.deadline_seconds:
            raise BudgetExceeded('deadline_exceeded')

    def reserve_model(self):
        with self.lock:
            self.check_budget()
            if self.model_calls >= self.max_model_calls:
                raise BudgetExceeded('model_call_limit')
            self.model_calls += 1
            self.record('model_started', call=self.model_calls)

    def reserve_tool(self, name):
        with self.lock:
            self.check_budget()
            if self.tool_calls >= 60:
                raise BudgetExceeded('tool_call_limit')
            self.tool_calls += 1
            self.record('tool_started', tool=name, call=self.tool_calls)

    def list_sources(self, offset: int = 0, limit: int = 100):
        self.reserve_tool('list_sources')
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('分页范围无效')
        paths = sorted(self.files)
        return {'files': paths[offset:offset + limit], 'total': len(paths),
                'allowed_paths': sorted(self.allowed_paths)}

    def read_source(self, path: str, start_line: int = 1, line_count: int = 120, raw: bool = False, force: bool = False):
        self.reserve_tool('read_source')
        return self._read_source(path,start_line,line_count,raw,force)

    def _read_source(self,path,start_line,line_count,raw,force):
        relative = PurePosixPath(path)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in path
                or relative.as_posix() != path or path not in self.files):
            raise ValueError('只允许读取快照清单内的规范相对路径')
        if start_line < 1 or not 1 <= line_count <= 200:
            raise ValueError('读取行范围无效')
        verify_snapshot(self.baseline)
        target = self.baseline / 'workspace' / path
        if target.stat().st_size > 1024 * 1024:
            raise ValueError('读取文件上限为 1 MiB')
        lines = target.read_text(encoding='utf-8').splitlines()
        excerpt = '\n'.join(f'{i + start_line}: {line}' for i, line in enumerate(
            lines[start_line - 1:start_line - 1 + line_count]))
        if raw:
            source = target.read_bytes().decode('utf-8')
            excerpt = ''.join(source.splitlines(keepends=True)[start_line - 1:start_line - 1 + line_count])
        result = {'path': path, 'total_lines': len(lines), 'content': excerpt[:16000],
                'truncated': len(excerpt) > 16000, 'content_format': 'raw' if raw else 'numbered',
                'start_line': start_line,
                'sha256': next(f['sha256'] for f in self.manifest['files'] if f['path'] == path)}
        if self.profile != 'v2':
            key = (path, start_line, line_count, raw)
            previous = self.read_receipts.get(key)
            delivered = len(result['content'].splitlines())
            covered = {(path,n) for n in range(start_line,start_line+delivered)}
            novel = len(covered-self.seen_lines)
            self.seen_lines.update(covered)
            self.context_stats['reads'] += 1
            self.context_stats['unique_lines'] += novel
            self.context_stats['repeated_reads'] += bool(previous)
            content_receipt = receipt(result['content'])
            remaining_calls = self.max_model_calls-self.model_calls
            if self.context_enabled:
                result['receipt'] = content_receipt
                result['remaining_model_calls'] = remaining_calls
            if previous and self.context_enabled and not force:
                result['content'] = ''
                result['already_delivered'] = True
                result['next_action'] = 'Use previous content, select a new symbol/range, or force=True if the earlier context is unavailable.'
                self.context_stats['suppressed_reads'] += 1
            self.read_receipts[key] = content_receipt
            self.context_stats['returned_characters'] += len(result['content'])
            self.record('source_read', path=path, sha256=result['sha256'], start_line=start_line,
                        requested_lines=line_count, delivered_lines=delivered if result['content'] else 0,
                        new_unique_lines=novel, raw=raw, force=force,
                        repeated=bool(previous), suppressed=bool(result.get('already_delivered')),
                        returned_characters=len(result['content']), content_sha256=content_receipt,
                        remaining_model_calls=remaining_calls)
        return result



    def source_outline(self, path: str):
        self.reserve_tool('source_outline')
        if path not in self.files:
            raise EditRejected('path_not_in_baseline')
        verify_snapshot(self.baseline)
        target = self.baseline/'workspace'/path
        if target.stat().st_size > 1024*1024:
            raise EditRejected('source_too_large')
        data=target.read_bytes()
        result={'path':path,'sha256':receipt(data.decode()),**outline(data.decode())}
        self.record('source_outline',path=path,returned_characters=len(json.dumps(result)),symbol_count=len(result['symbols']))
        return result

    def search_source(self, query: str, path: str | None = None):
        self.reserve_tool('search_source')
        if not isinstance(query,str) or not 1 <= len(query) <= 160 or '\n' in query:
            raise EditRejected('invalid_literal_search')
        if path is not None and path not in self.files:
            raise EditRejected('path_not_in_baseline')
        verify_snapshot(self.baseline)
        hits=[];truncated=False
        for relative in sorted([path] if path else self.files):
            if not relative.endswith('.py'):
                continue
            file=self.baseline/'workspace'/relative
            if file.stat().st_size > 1024*1024:
                continue
            for number,line in enumerate(file.read_bytes().decode('utf-8',errors='replace').splitlines(),1):
                if query in line:
                    if len(hits)==30:
                        truncated=True;break
                    hits.append({'path':relative,'line':number,'text':line[:180]})
            if truncated:break
        self.record('source_search', query_sha256=receipt(query), path=path, hit_count=len(hits), truncated=truncated)
        return {'matches':hits,'truncated':truncated,'literal_search':True}

    def read_symbol(self, path: str, symbol: str, force: bool = False):
        self.reserve_tool('read_symbol')
        if path not in self.files:
            raise EditRejected('path_not_in_baseline')
        verify_snapshot(self.baseline)
        file=self.baseline/'workspace'/path
        if file.stat().st_size > 1024*1024:
            raise EditRejected('source_too_large')
        node=units(file.read_bytes().decode()).get(symbol)
        if node is None or not hasattr(node,'lineno'):
            raise EditRejected('symbol_not_found')
        start=min([node.lineno]+[d.lineno for d in node.decorator_list])
        result=self._read_source(path,start,min(200,node.end_lineno-start+1),raw=True,force=force)
        result['symbol']=symbol
        result['symbol_end_line']=node.end_lineno
        return result

    def set_repair_plan(self, requirements: list[dict], scope: list[dict]):
        self.reserve_tool('set_repair_plan')
        if not self.task:
            raise EditRejected('task_not_initialized')
        plan=validate_plan(self,requirements,scope)
        self.repair_plan=plan
        self.record('repair_plan_updated',plan_id=plan['plan_id'],requirement_count=len(requirements),scope=scope)
        self.save('running')
        return plan

    def initial_context(self):
        if not self.context_enabled:
            return None
        verify_snapshot(self.baseline)
        items=[]
        for path in sorted(self.allowed_paths)[:8]:
            data=(self.baseline/'workspace'/path).read_bytes()
            if len(data) <= 1024*1024:
                items.append({'path':path,'sha256':next(f['sha256'] for f in self.manifest['files'] if f['path']==path),
                              **outline(data.decode())})
        with (self.baseline/'reports/pytest.log').open('rb') as stream:
            log=stream.read(2500).decode(errors='replace')
        packet={'files':items,'baseline_log_excerpt':log,'scope':'Only visible baseline sources/tests; no oracle or holdout.'}
        encoded=json.dumps(packet,ensure_ascii=False)
        # Retain valid structured JSON instead of truncating through its syntax.
        while len(encoded)>12000 and items:
            items.pop();encoded=json.dumps(packet,ensure_ascii=False)
        self.record('initial_context',characters=len(encoded),file_count=len(items))
        return packet

    def read_test_log(self, attempt: int = 0, offset: int = 0):
        self.reserve_tool('read_test_log')
        if offset < 0 or attempt < 0 or attempt > len(self.attempts):
            raise ValueError('日志位置无效')
        if attempt and self.attempts[attempt - 1].get('phase') == 'preflight':
            return {'attempt': attempt, 'preflight': self.attempts[attempt - 1],
                    'content': 'Candidate rejected before Docker. Correct the reported error against the original baseline.',
                    'next_offset': None}
        directory = self.baseline if attempt == 0 else self.output / f'attempt-{attempt:02d}' / 'verification'
        with (directory / 'reports/pytest.log').open('rb') as stream:
            stream.seek(offset)
            data = stream.read(16001)
        return {'attempt': attempt, 'offset': offset, 'content': data[:16000].decode('utf-8', errors='replace'),
                'next_offset': offset + 16000 if len(data) > 16000 else None}

    def submit_candidate(self, changes: dict[str, str], diagnosis: str, assessment: dict | None = None):
        return self._submit(lambda: changes, diagnosis, 'submit_candidate', assessment)

    def submit_edits(self, edits: list[dict], diagnosis: str, assessment: dict | None = None):
        return self._submit(lambda: apply_edits(self.baseline, edits, set(self.allowed_paths)),
                            diagnosis, 'submit_edits', assessment)

    def _submit(self, build_changes, diagnosis, tool_name, assessment=None):
        # 同一任务的候选执行串行化，避免并行工具调用绕过预算或覆盖记录。
        with self.lock:
            self.reserve_tool(tool_name)
            if any(a.get('status') == 'passed' for a in self.attempts):
                raise ValueError('已有通过验证的候选，任务不再接受修改')
            if len(self.attempts) >= self.max_attempts:
                raise BudgetExceeded('attempt_limit')
            if not isinstance(diagnosis, str) or not diagnosis.strip() or len(diagnosis) > 8000:
                raise ValueError('必须提供不超过 8000 字符的诊断依据')
            number = len(self.attempts) + 1
            attempt = {'number': number, 'status': 'running', 'diagnosis': diagnosis,
                       'phase': 'preflight', 'submission_tool': tool_name}
            self.attempts.append(attempt)
            root = self.output / f'attempt-{number:02d}'
            self.record('attempt_started', number=number)
            self.save('running')
            try:
                changes = build_changes()
                if not isinstance(changes, dict) or not changes:
                    raise EditRejected('invalid_changes_mapping')
                for path, content in changes.items():
                    if not isinstance(path, str):
                        raise EditRejected('invalid_path')
                    try:
                        validate_source_path(path, set(self.allowed_paths))
                    except ValueError:
                        raise EditRejected('path_not_allowed') from None
                    if path not in self.files:
                        raise EditRejected('path_not_in_baseline')
                    if not isinstance(content, str) or len(content.encode()) > 1024 * 1024:
                        raise EditRejected('invalid_candidate_content', path=path)
                if all(content.encode() == (self.baseline / 'workspace' / path).read_bytes()
                       for path, content in changes.items()):
                    raise EditRejected('empty_patch')
                manifest = create_candidate(self.baseline, root / 'candidate', changes, set(self.allowed_paths))
                fingerprint = content_id(manifest)
                attempt['content_id'] = fingerprint
                if fingerprint in self.candidate_fingerprints:
                    raise EditRejected('duplicate_candidate',
                                       previous_attempt=self.candidate_fingerprints[fingerprint])
                errors = syntax_errors(root / 'candidate/workspace', manifest['changed_paths'])
                if errors:
                    self.candidate_fingerprints[fingerprint] = number
                    raise EditRejected('syntax_error', errors=errors)
                if self.review_enabled:
                    attempt['contract_review'] = review_changes(self, changes, assessment)
                    attempt['contract_review']['candidate_content_id'] = fingerprint
                self.candidate_fingerprints[fingerprint] = number
                attempt['phase'] = 'verification'
                self.check_budget()
                remaining = self.deadline_seconds - (time.monotonic() - self.started)
                verdict = verify_candidate(self.baseline, root / 'candidate', root / 'verification',
                                           set(self.allowed_paths), min(self.test_timeout, remaining))
                attempt.update(verdict)
                if verdict['status'] != 'passed':
                    log = root / 'verification/reports/pytest.log'
                    if log.exists():
                        with log.open('rb') as stream:
                            attempt['test_log_excerpt'] = stream.read(4000).decode('utf-8', errors='replace')
                    attempt['next_action'] = 'Read the candidate log with read_test_log(attempt=number); revise against the original baseline.'
            except EditRejected as exc:
                attempt.update(status='inconclusive', reason=exc.reason, **exc.details,
                               next_action=('Review the reported requirement/scope/risk issue and update the plan or assessment before resubmitting; this rejection consumed an attempt.' if self.review_enabled and exc.reason in {'repair_plan_required','assessment_plan_mismatch','incomplete_requirement_coverage','risk_review_required','change_outside_declared_scope'} else 'Use read_source(raw=True) for current sha256 and exact content; correct edits against the original baseline. Do not repeat this candidate.'))
            except BudgetExceeded as exc:
                attempt.update(status='inconclusive', reason=str(exc))
                raise
            except Exception as exc:
                # 供应商、系统异常文本可能带凭据；事件只记录类型，不序列化异常对象。
                attempt.update(status='inconclusive', reason='candidate_rejected_or_execution_error',
                               error_type=type(exc).__name__)
            finally:
                self.record('attempt_finished', number=number, status=attempt['status'],
                            reason=attempt.get('reason'))
                self.save('running')
            return {k: v for k, v in attempt.items() if k != 'diagnosis'}

    def save(self, state, **extra):
        with self.lock:
            passed = next((a for a in self.attempts if a.get('status') == 'passed'), None)
            result = {'state': state, 'status': 'passed' if passed else
                      self.attempts[-1]['status'] if self.attempts else 'inconclusive',
                      'reason': passed['reason'] if passed else
                      self.attempts[-1].get('reason', 'attempt_in_progress') if self.attempts else 'no_candidate',
                      'baseline_id': snapshot_id(self.manifest), 'allowed_paths': sorted(self.allowed_paths),
                      'attempts': self.attempts, 'model_calls': self.model_calls, 'tool_calls': self.tool_calls,
                      'token_usage': self.token_usage, 'last_model_observation': self.last_model_observation,
                      'submission_policy': 'v3: submissions including review rejections consume attempts; review-only corrections may resubmit identical unexecuted content' if self.review_enabled else 'v2: every accepted submission request consumes one attempt, including preflight rejection',
                      'profile': self.profile, 'context_stats': self.context_stats,
                      'repair_plan': self.repair_plan,
                      'limits': {'attempts': self.max_attempts, 'model_calls': self.max_model_calls,
                                 'deadline_seconds': self.deadline_seconds},
                      'patch': f"attempt-{passed['number']:02d}/verification/patch.diff" if passed else None,
                      **extra}
            temporary = self.output / 'result.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(self.output / 'result.json')
            return result
