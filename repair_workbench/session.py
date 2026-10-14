"""修复任务的应用状态与受控操作，不依赖模型或 Agent 框架。"""
from __future__ import annotations

import json
import math
import secrets
import threading
import time
from pathlib import Path, PurePosixPath

from .candidate import create_candidate, validate_source_path
from .edits import EditRejected, apply_edits, syntax_errors, content_id
from .source_context import PROFILES, SIMPLE_PROFILES, outline, receipt, units
from .repair_contract import validate_plan, review_changes, advisory_review
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
        self.simple_edit_enabled = profile in SIMPLE_PROFILES
        self.advisory_enabled = profile in {'simple_review', 'simple_full'}
        self.behavior_summary = None
        self.closed = False
        self.context_enabled, self.review_enabled = PROFILES[profile]
        self.task = ""
        self.repair_plan = None
        self.read_receipts = {}
        self.source_refs = {}
        self.invalid_tool_fingerprint = None
        self.invalid_tool_recoveries = 0
        self.active_tool_round = None
        self.processed_tool_results = set()
        self.pending_parse_errors = []
        self.submission_counts = {'requests': 0, 'schema_rejections': 0,
                                  'preflight_rejections': 0, 'materialized': 0,
                                  'docker_verifications': 0}
        self.tool_statistics = {'calls':0,'schema_rejections':0,'by_tool':{},
            'by_category':{name:{'calls':0,'schema_rejections':0}
                           for name in ('edit','read_search','analysis','framework','unknown')}}
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

    def record_tool_statistic(self, tool, *, schema_error=False):
        if tool in {'submit_ref_edits','submit_edits','submit_candidate'}:
            category='edit'
        elif tool in {'read_source','read_test_log','list_sources','source_outline','read_symbol','search_source'}:
            category='read_search'
        elif tool in {'record_behavior_summary','set_repair_plan'}:
            category='analysis'
        elif tool in {'read_file','write_todos'}:
            category='framework'
        else:
            tool,category='unknown_tool','unknown'
        with self.lock:
            field='schema_rejections' if schema_error else 'calls'
            self.tool_statistics[field]+=1
            self.tool_statistics['by_category'][category][field]+=1
            self.tool_statistics['by_tool'].setdefault(tool,{'calls':0,'schema_rejections':0})[field]+=1
            if schema_error:
                # Legacy aggregate retained; it is not an edit-only denominator.
                self.submission_counts['schema_rejections']+=1
        return category

    def check_budget(self):
        if self.simple_edit_enabled and self.closed:
            raise BudgetExceeded('session_closed')
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
            raise EditRejected('invalid_page_range', field='offset/limit', allowed={'offset_min':0,'limit':[1,100]})
        paths = sorted(self.files)
        return {'files': paths[offset:offset + limit], 'total': len(paths),
                'allowed_paths': sorted(self.allowed_paths)}

    def read_source(self, path: str, start_line: int = 1, line_count: int = 120, raw: bool = False, force: bool = False):
        if self.simple_edit_enabled and self.closed:
            raise EditRejected('session_closed')
        self.reserve_tool('read_source')
        return self._read_source(path,start_line,line_count,raw,force)

    def _source_text(self, path):
        if not isinstance(path, str):
            raise EditRejected('invalid_source_path', field='path', expected_type='canonical relative string')
        relative = PurePosixPath(path)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in path
                or relative.as_posix() != path):
            raise EditRejected('invalid_source_path', field='path', allowed='canonical snapshot-relative path; no traversal')
        if path not in self.files:
            raise EditRejected('path_not_in_baseline', field='path', allowed='paths returned by list_sources')
        try:
            verify_snapshot(self.baseline)
        except ValueError:
            raise EditRejected('baseline_version_changed', field='baseline') from None
        except OSError:
            raise EditRejected('source_unavailable', field='path') from None
        target = self.baseline / 'workspace' / path
        try:
            if target.stat().st_size > 1024 * 1024:
                raise EditRejected('source_too_large', field='path', allowed={'max_bytes':1024*1024})
            return target.read_bytes().decode('utf-8')
        except UnicodeError:
            raise EditRejected('source_not_utf8', field='path', allowed='UTF-8 source') from None
        except OSError:
            raise EditRejected('source_unavailable', field='path') from None

    def _read_source(self,path,start_line,line_count,raw,force, *, character_limit=16000, purpose='tool'):
        if type(start_line) is not int or start_line < 1:
            raise EditRejected('invalid_start_line', field='start_line', allowed={'min':1})
        if type(line_count) is not int or not 1 <= line_count <= 200:
            raise EditRejected('invalid_line_count', field='line_count', allowed={'min':1,'max':200})
        source = self._source_text(path)
        lines = source.splitlines()
        if start_line > max(1,len(lines)):
            raise EditRejected('start_line_out_of_range', field='start_line', allowed={'min':1,'max':max(1,len(lines))})
        excerpt = '\n'.join(f'{i + start_line}: {line}' for i, line in enumerate(
            lines[start_line - 1:start_line - 1 + line_count]))
        if raw:
            excerpt = ''.join(source.splitlines(keepends=True)[start_line - 1:start_line - 1 + line_count])
        result = {'path': path, 'total_lines': len(lines), 'content': excerpt[:character_limit],
                'truncated': len(excerpt) > character_limit, 'content_format': 'raw' if raw else 'numbered',
                'start_line': start_line,
                'sha256': next(f['sha256'] for f in self.manifest['files'] if f['path'] == path)}
        if self.simple_edit_enabled and raw and result['content']:
            ref = secrets.token_urlsafe(24)
            self.source_refs[ref] = {'baseline_id': snapshot_id(self.manifest),
                                     'path': path, 'sha256': result['sha256'],
                                     'content': result['content'], 'start_line': start_line}
            result['source_ref'] = ref
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
            if previous and self.context_enabled and not self.simple_edit_enabled and not force:
                result['content'] = ''
                result['already_delivered'] = True
                result['next_action'] = 'Use previous content, select a new symbol/range, or force=True if the earlier context is unavailable.'
                self.context_stats['suppressed_reads'] += 1
            self.read_receipts[key] = content_receipt
            self.context_stats['returned_characters'] += len(result['content'])
            self.record('source_read', path=path, purpose=purpose, sha256=result['sha256'], start_line=start_line,
                        requested_lines=line_count, delivered_lines=delivered if result['content'] else 0,
                        new_unique_lines=novel, raw=raw, force=force,
                        repeated=bool(previous), suppressed=bool(result.get('already_delivered')),
                        returned_characters=len(result['content']), content_sha256=content_receipt,
                        remaining_model_calls=remaining_calls)
        return result



    def source_outline(self, path: str):
        self.reserve_tool('source_outline')
        text=self._source_text(path)
        result={'path':path,'sha256':receipt(text),**outline(text)}
        self.record('source_outline',path=path,returned_characters=len(json.dumps(result)),symbol_count=len(result['symbols']))
        return result

    def search_source(self, query: str, path: str | None = None):
        self.reserve_tool('search_source')
        if not isinstance(query,str) or not 1 <= len(query) <= 160 or '\n' in query:
            raise EditRejected('invalid_literal_search',field='query',allowed={'characters':[1,160],'newlines':False})
        if path is not None:
            self._source_text(path)
        hits=[];truncated=False;skipped=0
        enhanced=self.simple_edit_enabled and self.context_enabled
        maximum=8 if enhanced else 30
        for relative in sorted([path] if path else self.files, key=lambda value:(value not in self.allowed_paths,value)):
            self.check_budget()
            if not relative.endswith('.py'):continue
            try:
                text=self._source_text(relative)
            except EditRejected:
                if path is not None:raise
                skipped+=1;continue
            for number,line in enumerate(text.splitlines(),1):
                if query not in line:continue
                if len(hits)==maximum:
                    truncated=True;break
                hit={'path':relative,'line':number,'text':line[:180]}
                if enhanced:
                    from .source_context import location
                    hit['owner']=location(text,number)
                    read=self._read_source(relative,max(1,number-2),5,True,True,
                                           character_limit=800,purpose='search_context')
                    hit.update(content=read['content'],source_ref=read['source_ref'],start_line=read['start_line'])
                hits.append(hit)
            if truncated:break
        self.record('source_search',query_sha256=receipt(query),path=path,hit_count=len(hits),
                    truncated=truncated,skipped_files=skipped)
        return {'matches':hits,'truncated':truncated,'literal_search':True,'skipped_files':skipped}

    def read_symbol(self, path: str, symbol: str, force: bool = False):
        self.reserve_tool('read_symbol')
        text=self._source_text(path)
        try:
            node=units(text).get(symbol)
        except (ValueError,SyntaxError,RecursionError):
            raise EditRejected('source_symbol_parse_failed', field='symbol', allowed='use bounded read_source') from None
        if node is None or not hasattr(node,'lineno'):
            raise EditRejected('symbol_not_found', field='symbol', allowed='names returned by source_outline')
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

    def record_behavior_summary(self, change: str, preserve: str, related_entries: list[str]):
        self.reserve_tool('record_behavior_summary')
        if not self.advisory_enabled:
            raise EditRejected('behavior_summary_not_enabled')
        if not all(isinstance(value,str) and 0<len(value.strip())<=2000 for value in (change,preserve)):
            raise EditRejected('invalid_behavior_summary',field='change/preserve',allowed={'characters':[1,2000]})
        if (not isinstance(related_entries,list) or len(related_entries)>8
                or not all(isinstance(item,str) and len(item)<=160 for item in related_entries)):
            raise EditRejected('invalid_related_entries',field='related_entries',allowed={'max_entries':8,'max_characters':160})
        self.behavior_summary={'change':change,'preserve':preserve,'related_entries':list(related_entries),
                               'trust':'Model analysis; may be incomplete or incorrect.'}
        self.record('behavior_summary_updated',related_entry_count=len(related_entries))
        self.save('running')
        return self.behavior_summary

    def initial_context(self):
        if not self.context_enabled:
            return None
        if self.simple_edit_enabled:
            from .source_context import failure_context
            return failure_context(self)
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
        if type(offset) is not int or offset < 0:
            raise EditRejected('invalid_log_offset', field='offset', allowed={'min':0})
        if type(attempt) is not int or not 0 <= attempt <= len(self.attempts):
            raise EditRejected('invalid_log_attempt', field='attempt', allowed={'min':0,'max':len(self.attempts)})
        if attempt and self.attempts[attempt - 1].get('phase') == 'preflight':
            return {'attempt': attempt, 'preflight': self.attempts[attempt - 1],
                    'content': 'Candidate rejected before Docker. Correct the reported error against the original baseline.',
                    'next_offset': None}
        directory = self.baseline if attempt == 0 else self.output / f'attempt-{attempt:02d}' / 'verification'
        try:
            with (directory / 'reports/pytest.log').open('rb') as stream:
                stream.seek(offset)
                data = stream.read(16001)
        except OSError:
            raise EditRejected('test_log_unavailable', field='attempt') from None
        return {'attempt': attempt, 'offset': offset, 'content': data[:16000].decode('utf-8', errors='replace'),
                'next_offset': offset + 16000 if len(data) > 16000 else None}

    def submit_candidate(self, changes: dict[str, str], diagnosis: str, assessment: dict | None = None):
        return self._submit(lambda: changes, diagnosis, 'submit_candidate', assessment)

    def submit_edits(self, edits: list[dict], diagnosis: str, assessment: dict | None = None):
        return self._submit(lambda: apply_edits(self.baseline, edits, set(self.allowed_paths)),
                            diagnosis, 'submit_edits', assessment)

    def submit_ref_edits(self, edits: list[dict], diagnosis: str):
        """Resolve opaque read references before the existing candidate pipeline."""
        if self.closed:
            raise EditRejected('expired_source_ref')
        return self._submit(lambda: self._resolve_ref_edits(edits), diagnosis,
                            'submit_ref_edits')

    def _resolve_ref_edits(self, edits):
        if not self.simple_edit_enabled:
            raise EditRejected('reference_edits_not_enabled')
        if self.closed:
            raise EditRejected('expired_source_ref')
        if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
            raise EditRejected('invalid_edit_count', maximum=32)
        try:
            verify_snapshot(self.baseline)
        except (ValueError, OSError):
            raise EditRejected('baseline_version_changed') from None
        resolved = []
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict) or set(edit) != {'source_ref', 'old_text', 'new_text'}:
                raise EditRejected('invalid_edit_fields', index=index)
            ref = self.source_refs.get(edit['source_ref']) if isinstance(edit['source_ref'], str) else None
            if ref is None or ref['baseline_id'] != snapshot_id(self.manifest):
                raise EditRejected('invalid_source_ref', index=index)
            path, old, new = ref['path'], edit['old_text'], edit['new_text']
            if path not in self.allowed_paths:
                raise EditRejected('path_not_allowed', index=index)
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise EditRejected('invalid_edit_content', index=index)
            if ref['content'].find(old) < 0:
                raise EditRejected('text_outside_read_range', index=index)
            if ref['content'].find(old, ref['content'].find(old) + 1) >= 0:
                raise EditRejected('text_not_unique_in_read_range', index=index)
            resolved.append({'path': path, 'sha256': ref['sha256'],
                             'start_line': ref['start_line'] + ref['content'].count('\n', 0, ref['content'].find(old)),
                             'old_text': old, 'new_text': new})
        return apply_edits(self.baseline, resolved, set(self.allowed_paths))

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
            self.submission_counts['requests'] += 1
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
                self.submission_counts['materialized'] += 1
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
                if self.advisory_enabled:
                    attempt['risk_review']=advisory_review(self,changes)
                    self.record('candidate_risk_report',number=number,
                                risk_kinds=[risk['kind'] for risk in attempt['risk_review']['risks']])
                self.candidate_fingerprints[fingerprint] = number
                attempt['phase'] = 'verification'
                self.check_budget()
                remaining = self.deadline_seconds - (time.monotonic() - self.started)
                self.submission_counts['docker_verifications'] += 1
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
                self.submission_counts['preflight_rejections'] += 1
                attempt.update(status='inconclusive', reason=exc.reason, **exc.details,
                               next_action=('Review the reported requirement/scope/risk issue and update the plan or assessment before resubmitting; this rejection consumed an attempt.' if self.review_enabled and exc.reason in {'repair_plan_required','assessment_plan_mismatch','incomplete_requirement_coverage','risk_review_required','change_outside_declared_scope'} else 'Use read_source(raw=True) for current sha256 and exact content; correct edits against the original baseline. Do not repeat this candidate.'))
            except BudgetExceeded as exc:
                attempt.update(status='inconclusive', reason=str(exc))
                raise
            except KeyboardInterrupt:
                attempt.update(status='inconclusive',reason='cancel_requested')
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
            if self.simple_edit_enabled and state in {'completed', 'stopped'}:
                self.closed = True
            passed = next((a for a in self.attempts if a.get('status') == 'passed'), None)
            result = {'state': state, 'status': 'passed' if passed else
                      self.attempts[-1]['status'] if self.attempts else 'inconclusive',
                      'reason': passed['reason'] if passed else
                      self.attempts[-1].get('reason', 'attempt_in_progress') if self.attempts else 'no_candidate',
                      'baseline_id': snapshot_id(self.manifest), 'allowed_paths': sorted(self.allowed_paths),
                      'attempts': self.attempts, 'model_calls': self.model_calls, 'tool_calls': self.tool_calls,
                      'token_usage': self.token_usage, 'last_model_observation': self.last_model_observation,
                      'usage_complete':self.token_usage['reports']==self.model_calls,
                      'usage_missing_calls':self.model_calls-self.token_usage['reports'],
                      'tool_statistics':self.tool_statistics,
                      'tool_recovery_policy':('revision 2: per-call-ID error receipts; at most two admitted recovery rounds; shared model budget'
                                              if self.simple_edit_enabled else 'legacy: no added application schema recovery; shared model budget'),
                      'submission_policy': 'v3: submissions including review rejections consume attempts; review-only corrections may resubmit identical unexecuted content' if self.review_enabled else 'v2: every accepted submission request consumes one attempt, including preflight rejection',
                      'profile': self.profile, 'context_stats': self.context_stats,
                      'features':{'simple_edit':self.simple_edit_enabled,'context':self.context_enabled,
                                  'strict_review':self.review_enabled,'advisory_review':self.advisory_enabled},
                      'behavior_summary':self.behavior_summary,
                      'submission_counts': self.submission_counts,
                      'invalid_tool_recoveries': self.invalid_tool_recoveries,
                      'repair_plan': self.repair_plan,
                      'limits': {'attempts': self.max_attempts, 'model_calls': self.max_model_calls,
                                 'deadline_seconds': self.deadline_seconds},
                      'patch': f"attempt-{passed['number']:02d}/verification/patch.diff" if passed else None,
                      **extra}
            temporary = self.output / 'result.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(self.output / 'result.json')
            return result
