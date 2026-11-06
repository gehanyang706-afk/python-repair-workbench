"""Evidence-bound diagnostic sessions with independently verified performance goals."""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

from .edits import EditRejected
from .session import BudgetExceeded, RepairSession
from .snapshot import snapshot_id, verify_snapshot
from .diagnostic_reports import visible_evidence


class _SessionControl:
    """All container stages share the session deadline and cancellation signal."""

    def __init__(self, session):
        self.session = session

    def check(self):
        self.session.check_budget()
        if self.session.execution_control:
            self.session.execution_control.check()

    def container(self, name, state):
        self.session.record('diagnostic_container', name=name, state=state)
        if self.session.execution_control:
            self.session.execution_control.container(name, state)


class DiagnosticSession(RepairSession):
    """The Agent selects evidence-gathering actions; code owns the verdict.

    This is an opt-in task type. The original RepairSession still requires a
    reproduced assertion failure and does not gain a performance bypass.
    """

    diagnostics_enabled = True

    def __init__(self, baseline: Path, output: Path, allowed_paths: set[str], *,
                 workload='ugc-export-v1', strategy='adaptive', max_probe_runs=6,
                 minimum_speedup=0.15, max_memory_ratio=1.25, use_skills=False,
                 use_investigator=False, max_delegations=1,
                 investigator_max_model_calls=3, investigator_max_tools=12,
                 investigator_timeout_seconds=30, main_model_reserve=2,
                 main_tool_reserve=4, main_time_reserve_seconds=10,
                 diagnostic_transport='local', tool_concurrency=1, tool_rate_config=None, **kwargs):
        if type(tool_concurrency) is not int or not 1 <= tool_concurrency <= 4:
            raise ValueError('tool_concurrency must be an integer in 1..4')
        self.tool_concurrency = tool_concurrency
        if strategy not in {'adaptive', 'fixed'}:
            raise ValueError('诊断策略必须是 adaptive 或 fixed')
        if workload != 'ugc-export-v1':
            raise ValueError('未注册的诊断工作负载')
        if diagnostic_transport not in {'local', 'mcp'}:
            raise ValueError('诊断工具接入方式必须是local或mcp')
        self.diagnostic_transport = diagnostic_transport
        if type(max_probe_runs) is not int or not 3 <= max_probe_runs <= 12:
            raise ValueError('诊断运行上限必须为3–12')
        if (type(minimum_speedup) not in {int, float}
                or not math.isfinite(minimum_speedup) or not 0 < minimum_speedup < 1
                or type(max_memory_ratio) not in {int, float}
                or not math.isfinite(max_memory_ratio) or not 1 <= max_memory_ratio <= 4):
            raise ValueError('性能阈值无效')
        if kwargs.get('durability') is not None:
            raise ValueError('性能诊断尚未接入持久 Worker 恢复')
        if 'profile' in kwargs:
            raise ValueError('诊断任务使用独立策略，不接受旧修复 profile')
        if (type(use_skills) is not bool or type(use_investigator) is not bool
                or type(max_delegations) is not int or not 1 <= max_delegations <= 2):
            raise ValueError('诊断扩展开关或委派上限无效')
        self.use_skills = use_skills
        self.use_investigator = use_investigator
        self.max_delegations = max_delegations
        self.delegation_count = 0
        self.delegation_history = []
        if (type(investigator_max_model_calls) is not int or not 1 <= investigator_max_model_calls <= 30
                or type(investigator_max_tools) is not int or not 1 <= investigator_max_tools <= 60
                or type(main_model_reserve) is not int or not 1 <= main_model_reserve <= 30
                or type(main_tool_reserve) is not int or not 1 <= main_tool_reserve < 60
                or type(investigator_timeout_seconds) not in {int, float}
                or not math.isfinite(investigator_timeout_seconds) or investigator_timeout_seconds <= 0
                or type(main_time_reserve_seconds) not in {int, float}
                or not math.isfinite(main_time_reserve_seconds) or main_time_reserve_seconds <= 0):
            raise ValueError('子任务配额和主流程预留必须为有效正数，次数须为严格整数')
        if use_investigator:
            root_models = kwargs.get('max_model_calls', 12)
            root_seconds = kwargs.get('deadline_seconds', 180)
            if (type(root_models) is not int or not 1 <= root_models <= 30
                    or type(root_seconds) not in {int, float}
                    or not math.isfinite(root_seconds) or root_seconds <= 0
                    or main_model_reserve > root_models or main_time_reserve_seconds > root_seconds):
                raise ValueError('主流程预留不能超过有效的任务总模型调用或时间上限')
        self.investigator_max_model_calls = investigator_max_model_calls
        self.investigator_max_tools = investigator_max_tools
        self.investigator_timeout_seconds = investigator_timeout_seconds
        self.main_model_reserve = main_model_reserve
        self.main_tool_reserve = main_tool_reserve
        self.main_time_reserve_seconds = main_time_reserve_seconds
        self.skill_manifest = None
        if self.use_skills:
            from .diagnostic_skills import trusted_skill_manifest
            self.skill_manifest = trusted_skill_manifest()
        self.strategy = strategy
        self.workload = workload
        self.max_probe_runs = max_probe_runs
        self.minimum_speedup = minimum_speedup
        self.max_memory_ratio = max_memory_ratio
        self.probe_runs = 0
        self.evidence = {}
        self.hypotheses = {}
        self.diagnostic_cache = {}
        self.fixed_diagnostics_done = False
        self.final_decision = None
        self.diagnostic_stop_reason = None
        from .diagnostic_progress import initial_progress_state
        self.diagnostic_progress = initial_progress_state()
        super().__init__(baseline, output, allowed_paths, profile='simple_context', **kwargs)
        from .tool_runtime import ToolRuntime
        shared_limiter = None
        if tool_rate_config is not None:
            from .redis_rate_limit import RedisRateLimiter
            try:
                shared_limiter = RedisRateLimiter.from_config(tool_rate_config)
            except (BudgetExceeded, ValueError, OSError, ImportError) as exc:
                self.save('stopped', stop_reason=(str(exc) if isinstance(exc, BudgetExceeded)
                          else 'shared_rate_configuration_error'), error_type=type(exc).__name__)
                raise
        self.tool_runtime = ToolRuntime(record=self.record, shared_limiter=shared_limiter)
        self._add_evidence('baseline_tests', {
            'status': self.before['status'], 'counts': self.before['counts'],
            'scope': 'Original tests only; not proof of optimal performance.'},
            snapshot_id(self.manifest))
        self.save('running')

    def check_budget(self):
        super().check_budget()
        if self.diagnostic_stop_reason:
            raise BudgetExceeded(self.diagnostic_stop_reason)

    def investigator_budget_policy(self):
        return {'max_model_calls': self.investigator_max_model_calls,
                'max_tools': self.investigator_max_tools,
                'timeout_seconds': self.investigator_timeout_seconds,
                'main_model_reserve': self.main_model_reserve,
                'main_tool_reserve': self.main_tool_reserve,
                'main_time_reserve_seconds': self.main_time_reserve_seconds,
                'root_tool_limit': 60, 'token_hard_limit': False,
                'scope': 'Subtask limits narrow the existing root budget; no extra quota.'}

    def reserve_investigator_budget(self, record):
        """Narrow a dispatch to current root headroom while retaining main quota."""
        with self.lock:
            self.check_budget()
            if self.model_calls >= self.max_model_calls:
                raise BudgetExceeded('model_call_limit')
            if self.tool_calls >= 60:
                raise BudgetExceeded('tool_call_limit')
            started = time.monotonic()
            available = {'model_calls': self.max_model_calls - self.model_calls - self.main_model_reserve,
                         'tools': 60 - self.tool_calls - self.main_tool_reserve,
                         'seconds': self.deadline_seconds - (started - self.started) - self.main_time_reserve_seconds}
            reason = next((name for name in ('model_calls', 'tools', 'seconds')
                           if available[name] <= 0), None)
            allocation = {'accepted': reason is None, 'available_after_main_reserve': available,
                          'token_hard_limit': False}
            if reason is not None:
                allocation['reason'] = 'main_' + reason + '_reserve'
                record['budget'] = allocation
                return allocation
            allocation.update(max_model_calls=min(self.investigator_max_model_calls, available['model_calls']),
                              max_tools=min(self.investigator_max_tools, available['tools']),
                              timeout_seconds=min(self.investigator_timeout_seconds, available['seconds']))
            record['budget'] = dict(allocation)
            return {**allocation, 'started': started, 'model_calls_used': 0,
                    'tools_admitted': 0, 'tool_calls_used': 0}

    def initial_files(self):
        if not self.use_skills:
            return {}
        from .diagnostic_skills import initial_skill_files
        return initial_skill_files()

    def _validate_baseline(self, before, outcomes):
        if (before.get('snapshot_id') != snapshot_id(self.manifest)
                or before.get('status') != 'tests_passed' or not before.get('cleanup_ok')
                or not outcomes or any(value != 'passed' for value in outcomes.values())
                or not isinstance(before.get('image_id'), str) or not before['image_id']):
            raise ValueError('性能任务需要内容一致、原始测试全部通过且清理完成的基线')
        if 'map_exporter.py' not in self.files:
            raise ValueError('当前工作负载要求 map_exporter.py')

    def _add_evidence(self, kind, observation, binding):
        with self.lock:
            return self._store_evidence(kind, observation, binding)

    def _store_evidence(self, kind, observation, binding):
        # References prove provenance, not the truth of a model's causal claim.
        identity = f'E{len(self.evidence) + 1:04d}'
        item = {'evidence_ref': identity, 'kind': kind, 'snapshot_id': binding,
                'observation': observation, 'trust': 'tool_observation'}
        item['sha256'] = hashlib.sha256(json.dumps(item, sort_keys=True,
                                                   ensure_ascii=False).encode()).hexdigest()
        self.evidence[identity] = item
        directory = self.output / 'evidence'
        directory.mkdir(exist_ok=True)
        (directory / f'{identity}.json').write_text(
            json.dumps(item, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.record('evidence_registered', evidence_ref=identity, kind=kind,
                    snapshot_id=binding, sha256=item['sha256'])
        return identity

    def _read_source(self, *args, **kwargs):
        with self.lock:
            return self._read_source_locked(*args, **kwargs)

    def _read_source_locked(self, *args, **kwargs):
        result = super()._read_source(*args, **kwargs)
        if result.get('content'):
            result['evidence_ref'] = self._add_evidence('source_excerpt', {
                'path': result['path'], 'start_line': result['start_line'],
                'content_sha256': hashlib.sha256(result['content'].encode()).hexdigest(),
                'source_ref': result.get('source_ref'), 'truncated': result['truncated']},
                snapshot_id(self.manifest))
        return result

    def list_sources(self, offset=0, limit=100):
        self.reserve_tool('list_sources')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise EditRejected('invalid_page_range')
        paths = sorted(self.files)
        page = paths[offset:offset + limit]
        # Very long legal paths must not defeat the tool-result context bound.
        while len(json.dumps(page, ensure_ascii=False)) > 8000 and len(page) > 1:
            page.pop()
        return {'files': page, 'total': len(paths),
                'allowed_paths': [path for path in page if path in self.allowed_paths],
                'allowed_path_count': len(self.allowed_paths),
                'allowed_paths_scope': 'Current file page only; page through all files to inspect the rest.',
                'next_offset': offset + len(page) if offset + len(page) < len(paths) else None}

    def initial_context(self):
        self.check_budget()
        verify_snapshot(self.baseline)
        packet = {'task_type': 'performance_optimization', 'strategy': self.strategy,
                'workload_id': self.workload, 'entrypoint': 'map_exporter.py:export_map',
                'contract': {'objects': 'list of id/resource_id',
                             'resources': 'list of id/path; first duplicate id wins',
                             'output': 'ordered list of object_id/resource_path',
                             'missing_resource': 'raise ValueError',
                             'input_mutation': 'forbidden'},
                'files': sorted(self.files)[:100], 'file_count': len(self.files),
                'allowed_paths': sorted(self.allowed_paths)[:32],
                'allowed_path_count': len(self.allowed_paths),
                'probes': ['cpu', 'memory', 'benchmark'],
                'evidence_index': [{key: item[key] for key in ('evidence_ref', 'kind', 'snapshot_id')}
                                   for item in list(self.evidence.values())[-20:]],
                'diagnostic_summaries': [visible_evidence(item) for item in list(self.evidence.values())[-4:]],
                'thresholds': {'minimum_speedup': self.minimum_speedup,
                               'max_memory_ratio': self.max_memory_ratio},
                'scope': 'Public development workload; no independent answers or reference patch.',
                'context_truncated': len(self.files) > 100 or len(self.allowed_paths) > 32,
                'navigation': 'Use list_sources(offset,limit) and read_evidence(ref) for omitted details.'}
        while len(json.dumps(packet, ensure_ascii=False)) > 12000:
            packet['context_truncated'] = True
            if packet['diagnostic_summaries']:
                packet['diagnostic_summaries'].pop(0)
            elif packet['files']:
                packet['files'].pop()
            elif packet['allowed_paths']:
                packet['allowed_paths'].pop()
            else:
                break
        self.record('diagnostic_initial_context', characters=len(json.dumps(packet, ensure_ascii=False)),
                    truncated=packet['context_truncated'])
        return packet

    def _check_refs(self, evidence_refs):
        if (not isinstance(evidence_refs, list) or len(evidence_refs) > 12
                or any(not isinstance(ref, str) or ref not in self.evidence for ref in evidence_refs)):
            raise EditRejected('unknown_evidence_reference', field='evidence_refs')
        return list(dict.fromkeys(evidence_refs))

    def record_hypothesis(self, claim: str, status='open', evidence_refs=None,
                          hypothesis_id=None, next_action=''):
        with self.lock:
            self.reserve_tool('record_hypothesis')
            if self.final_decision:
                raise EditRejected('diagnosis_already_concluded')
            if (not isinstance(claim, str) or not 1 <= len(claim.strip()) <= 1200
                    or status not in {'open', 'supported', 'rejected'}
                    or not isinstance(next_action, str) or len(next_action) > 500):
                raise EditRejected('invalid_hypothesis')
            refs = self._check_refs([] if evidence_refs is None else evidence_refs)
            if status != 'open' and not refs:
                raise EditRejected('evidence_required_for_hypothesis_update')
            if hypothesis_id is not None:
                if not isinstance(hypothesis_id, str) or hypothesis_id not in self.hypotheses:
                    raise EditRejected('unknown_hypothesis')
                identity = hypothesis_id
            else:
                if len(self.hypotheses) >= 12:
                    raise EditRejected('hypothesis_limit')
                identity = f'H{len(self.hypotheses) + 1:03d}'
            previous = self.hypotheses.get(identity)
            item = {'hypothesis_id': identity, 'claim': claim.strip(), 'status': status,
                    'evidence_refs': refs, 'next_action': next_action,
                    'trust': 'model_judgement_not_verified_fact',
                    'revision': previous['revision'] + 1 if previous else 1}
            self.hypotheses[identity] = item
            self.record('hypothesis_updated', **item)
            self.save('running')
            return item

    def run_diagnostic(self, probe: str, hypothesis_id=None, attempt=0):
        from .diagnostics import run_probe
        if self.diagnostic_transport == 'mcp':
            from .diagnostic_mcp_client import run_mcp_probe, MCPProbeError
            execute = run_mcp_probe
        else:
            execute = run_probe
        with self.lock:
            self.reserve_tool('run_probe')
            if self.final_decision:
                raise EditRejected('diagnosis_already_concluded')
            if probe not in {'cpu', 'memory', 'benchmark'}:
                raise EditRejected('unknown_probe', field='probe', allowed=['cpu', 'memory', 'benchmark'])
            if hypothesis_id is not None and (not isinstance(hypothesis_id, str)
                                               or hypothesis_id not in self.hypotheses):
                raise EditRejected('unknown_hypothesis')
            if type(attempt) is not int or not 0 <= attempt <= len(self.attempts):
                raise EditRejected('invalid_probe_attempt')
            target = self.baseline
            if attempt:
                target = self.output / f'attempt-{attempt:02d}' / 'candidate'
                if not (target / 'manifest.json').is_file():
                    raise EditRejected('candidate_not_materialized')
            binding = snapshot_id(verify_snapshot(target))
            key = (binding, probe, self.workload)
            if key in self.diagnostic_cache:
                ref = self.diagnostic_cache[key]
                self.record('diagnostic_reused', evidence_ref=ref, probe=probe,
                            hypothesis_id=hypothesis_id)
                return {**visible_evidence(self.evidence[ref]), 'reused': True,
                        'scope': 'Cached immutable observation; no new experiment was run.'}
            if self.probe_runs >= self.max_probe_runs:
                raise BudgetExceeded('diagnostic_probe_limit')
            self.probe_runs += 1
            destination = self.output / 'diagnostics' / f'probe-{self.probe_runs:02d}'
            remaining = self.deadline_seconds - (time.monotonic() - self.started)
            self.record('diagnostic_started', probe=probe, hypothesis_id=hypothesis_id,
                        snapshot_id=binding, run=self.probe_runs)
            try:
                report = self.tool_runtime.invoke(
                    'mcp:diagnostics' if self.diagnostic_transport == 'mcp' else 'local',
                    'run_probe', {'snapshot_id': binding, 'probe': probe, 'workload': self.workload},
                    lambda: execute(target, destination, self.before['image_id'], probe,
                                    self.workload, min(self.test_timeout, remaining),
                                    execution_control=_SessionControl(self)),
                    check=_SessionControl(self).check)
            except ValueError as exc:
                if self.diagnostic_transport == 'mcp' and isinstance(exc, MCPProbeError):
                    self.diagnostic_stop_reason = exc.reason
                    raise BudgetExceeded(exc.reason) from None
                raise
            observation = {'probe': probe, 'status': report['status'],
                           'cleanup_ok': report.get('cleanup_ok', False),
                           'summary': report.get('summary', {}),
                           'report': str(destination.relative_to(self.output)),
                           'transport': self.diagnostic_transport,
                           'hypothesis_id': hypothesis_id}
            result_path = destination / 'result.json'
            if result_path.is_file():
                observation['report_sha256'] = hashlib.sha256(result_path.read_bytes()).hexdigest()
            ref = self._add_evidence('diagnostic_probe', observation, binding)
            # Never treat a failed or incompletely cleaned run as reusable success.
            if report['status'] == 'ok' and report.get('cleanup_ok'):
                self.diagnostic_cache[key] = ref
            self.record('diagnostic_finished', probe=probe, evidence_ref=ref,
                        status=report['status'], cleanup_ok=report.get('cleanup_ok', False))
            self.save('running')
            self.check_budget()
            if not report.get('cleanup_ok'):
                self.diagnostic_stop_reason = 'diagnostic_cleanup_unconfirmed'
                raise BudgetExceeded('diagnostic_cleanup_unconfirmed')
            return visible_evidence(self.evidence[ref])

    def read_evidence(self, evidence_ref):
        self.reserve_tool('read_evidence')
        self._check_refs([evidence_ref])
        return visible_evidence(self.evidence[evidence_ref])

    def read_probe_report(self, evidence_ref, section='summary', scale='large', offset=0, limit=5):
        """Admit every read, then query/cache a version-bound measured report page."""
        from .diagnostic_reports import freeze_probe, write_bundle, ReportStore, read_json
        from .tool_runtime import digest
        self.reserve_tool('read_probe_report')
        self._check_refs([evidence_ref])
        record = freeze_probe(self.output, self.evidence[evidence_ref])
        identity = record['report_id']
        bundle = self.output / 'report_queries' / f'{identity}.json'
        with self.lock:
            if bundle.exists():
                _, sha = read_json(bundle, 2 * 1024 * 1024)
                if ReportStore(bundle, sha).reports != {identity: record}:
                    raise ValueError('report_query_bundle_changed')
            else:
                sha = write_bundle([record], bundle)
        store = ReportStore(bundle, sha)
        arguments = {'report_id': identity, 'section': section, 'scale': scale,
                     'offset': offset, 'limit': limit}
        # Validate before either transport and never let a tool argument choose
        # a bundle path, executable, MCP server or snapshot version.
        store.query(**arguments)
        def execute():
            if self.diagnostic_transport == 'mcp':
                from .diagnostic_report_mcp import query_mcp_report
                remaining = self.deadline_seconds - (time.monotonic() - self.started)
                return query_mcp_report(bundle, sha, arguments, execution_control=_SessionControl(self),
                                        timeout_seconds=min(10, remaining))
            return store.query(**arguments)
        result = self.tool_runtime.invoke(
            'mcp:reports' if self.diagnostic_transport == 'mcp' else 'local',
            'query_report', arguments, execute, version=sha, check=_SessionControl(self).check)
        self.record('diagnostic_report_queried', evidence_ref=evidence_ref,
                    bundle=str(bundle.relative_to(self.output)), bundle_sha256=sha,
                    arguments=arguments, result_sha256=digest(result),
                    response_bytes=len(json.dumps(result, ensure_ascii=False).encode()),
                    transport=self.diagnostic_transport)
        self.save('running')
        return result

    def conclude_diagnosis(self, decision: str, explanation: str, evidence_refs: list[str]):
        with self.lock:
            self.reserve_tool('conclude_diagnosis')
            if (decision not in {'no_change', 'insufficient_evidence'}
                    or not isinstance(explanation, str) or not 1 <= len(explanation.strip()) <= 1500):
                raise EditRejected('invalid_diagnostic_conclusion')
            if self.attempts or self.final_decision:
                raise EditRejected('diagnosis_already_modified_or_concluded')
            refs = self._check_refs(evidence_refs)
            if not refs:
                raise EditRejected('conclusion_requires_evidence')
            self.final_decision = {'decision': decision, 'explanation': explanation,
                                   'evidence_refs': refs,
                                   'trust': 'model_proposal_not_independent_acceptance'}
            self.record('diagnosis_concluded', **self.final_decision)
            self.save('running')
            return self.final_decision

    def prepare_fixed_diagnostics(self):
        if self.strategy != 'fixed' or self.fixed_diagnostics_done:
            return
        # Comparator has identical tools and budgets; its exploration order is fixed.
        for probe in ('cpu', 'memory', 'benchmark'):
            self.run_diagnostic(probe)
        self.fixed_diagnostics_done = True
        self.save('running')

    def _submit(self, *args, **kwargs):
        with self.lock:
            if self.final_decision:
                raise EditRejected('diagnosis_already_concluded')
            if self.strategy == 'fixed' and not self.fixed_diagnostics_done:
                raise EditRejected('fixed_diagnostics_not_completed')
            result = super()._submit(*args, **kwargs)
        # Full measurements remain in the verification artifact, outside the model context.
        visible = {'number', 'status', 'reason', 'phase', 'evidence_ref', 'scope',
                   'metrics', 'thresholds', 'changed_paths', 'next_action',
                   'test_log_excerpt', 'errors', 'previous_attempt'}
        return {key: value for key, value in result.items() if key in visible}

    def _verify_candidate(self, candidate, destination, timeout_seconds):
        from .performance import verify_performance_candidate
        verdict = verify_performance_candidate(
            self.baseline, candidate, destination, set(self.allowed_paths),
            self.workload, timeout_seconds, execution_control=_SessionControl(self),
            minimum_speedup=self.minimum_speedup, max_memory_ratio=self.max_memory_ratio)
        observation = {key: verdict[key] for key in
                       ('status', 'reason', 'scope', 'changed_paths', 'thresholds',
                        'metrics', 'harness_sha256', 'workload_fingerprint') if key in verdict}
        observation['report'] = str((destination / 'performance-verification.json').relative_to(self.output))
        ref = self._add_evidence('candidate_verification', observation,
                                 snapshot_id(verify_snapshot(candidate)))
        if verdict.get('reason') == 'container_cleanup_unconfirmed':
            self.diagnostic_stop_reason = 'diagnostic_cleanup_unconfirmed'
            raise BudgetExceeded(self.diagnostic_stop_reason)
        return {**verdict, 'evidence_ref': ref}

    def save(self, state, **extra):
        return super().save(state, task_type='performance_optimization',
                            diagnostic_strategy=self.strategy,
                            diagnostic_workload=self.workload,
                            diagnostic_transport=self.diagnostic_transport,
                            tool_concurrency=self.tool_concurrency,
                            tool_runtime=self.tool_runtime.summary() if hasattr(self, 'tool_runtime') else None,
                            diagnostic_probe_runs=self.probe_runs,
                            diagnostic_stop_reason=self.diagnostic_stop_reason,
                            diagnostic_progress={key: value for key, value in self.diagnostic_progress.items()
                                                 if key not in {'records', 'processed_rounds'}},
                            diagnostic_options={'skills': self.use_skills,
                                                'investigator': self.use_investigator},
                            skill_manifest=self.skill_manifest,
                            delegation_count=self.delegation_count,
                            delegation_history=self.delegation_history,
                            investigator_budget_policy=self.investigator_budget_policy(),
                            diagnostic_limits={'exploratory_probe_runs': self.max_probe_runs,
                                               'verification_probe_runs_per_candidate': 8},
                            hypotheses=self.hypotheses,
                            evidence_index=[{key: value[key] for key in
                                             ('evidence_ref', 'kind', 'snapshot_id', 'sha256')}
                                            for value in self.evidence.values()],
                            final_decision=self.final_decision, **extra)
