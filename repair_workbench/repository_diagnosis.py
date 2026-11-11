"""Public-only repository diagnosis, with single final submission and no grading.

The trusted preparer supplies a development projection and original-suite
qualification. Full WorkloadSpec objects, final inputs and oracle feedback never
enter this Session. Hidden acceptance belongs to the application after closure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .candidate import create_candidate
from .diagnosis import DiagnosticSession, _SessionControl
from .diagnostic_reports import visible_evidence
from .edits import EditRejected
from .evaluation_budget import _atomic_write
from .repository_performance import _commit_matches, original_suite_sha256
from .session import BudgetExceeded, RepairSession
from .snapshot import snapshot_id, verify_snapshot
from .workload_contract import canonical_json


QUALIFICATION_FIELDS = {'schema_version', 'task_id', 'repo', 'base_commit',
    'baseline_ref', 'image_id', 'suite_sha256', 'report_sha256', 'inventory_sha256',
    'original_tests_accepted'}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RepositoryDiagnosticSession(DiagnosticSession):
    repository_diagnostics_enabled = True
    candidate_evaluation_is_docker = False

    def __init__(self, baseline, output, allowed_paths, development_workload,
                 baseline_qualification, **kwargs):
        from .repository_probes import validate_development_workload
        self.development_workload = validate_development_workload(development_workload)
        self.public_workload_sha256 = _digest(canonical_json(self.development_workload).encode())
        if (type(baseline_qualification) is not dict or set(baseline_qualification) != QUALIFICATION_FIELDS
                or type(baseline_qualification.get('schema_version')) is not int
                or baseline_qualification['schema_version'] != 1
                or baseline_qualification.get('original_tests_accepted') is not True):
            raise ValueError('invalid_repository_baseline_qualification')
        self.baseline_qualification = json.loads(canonical_json(baseline_qualification))
        for key in ('baseline_ref', 'suite_sha256', 'report_sha256', 'inventory_sha256'):
            if type(self.baseline_qualification[key]) is not str or not re.fullmatch(
                    '[a-f0-9]{64}', self.baseline_qualification[key]):
                raise ValueError('invalid_repository_qualification_hash')
        if (type(self.baseline_qualification['image_id']) is not str
                or not re.fullmatch('sha256:[a-f0-9]{64}', self.baseline_qualification['image_id'])):
            raise ValueError('invalid_repository_qualification_image')
        for key in ('task_id', 'repo', 'base_commit'):
            if self.baseline_qualification[key] != self.development_workload[key]:
                raise ValueError('repository_public_task_qualification_mismatch')
        for key, required in (('diagnostic_transport', 'local'), ('use_skills', False),
                              ('use_investigator', False), ('tool_concurrency', 1)):
            value = kwargs.get(key, required)
            if type(value) is not type(required) or value != required:
                raise ValueError('unsupported_repository_' + key)
        for key in ('workload', 'minimum_speedup', 'max_memory_ratio'):
            if key in kwargs:
                raise ValueError('repository_workload_configuration_is_prepared')
        self.repository_probe_workspace = None
        self._probe_evidence_by_report = {}
        self.final_submission = None
        self.final_submission_sha256 = None
        self.submitted_number = None
        thresholds = self.development_workload['thresholds']
        super().__init__(Path(baseline), Path(output), allowed_paths,
            workload=self.development_workload['workload_id'],
            minimum_speedup=thresholds['min_speedup'],
            max_memory_ratio=thresholds['max_memory_ratio'], **kwargs)

    def _validate_workload_configuration(self, workload, minimum_speedup, max_memory_ratio):
        data = self.development_workload
        if (workload != data['workload_id'] or minimum_speedup != data['thresholds']['min_speedup']
                or max_memory_ratio != data['thresholds']['max_memory_ratio']):
            raise ValueError('repository_workload_configuration_mismatch')

    def _validate_baseline(self, before, outcomes):
        q, data = self.baseline_qualification, self.development_workload
        inventory = before.get('test_inventory')
        if (snapshot_id(self.manifest) != q['baseline_ref']
                or before.get('snapshot_id') != q['baseline_ref']
                or not _commit_matches(self.manifest, data['base_commit'])
                or original_suite_sha256(self.manifest) != q['suite_sha256']
                or before.get('status') != 'tests_passed' or before.get('cleanup_ok') is not True
                or before.get('image_id') != q['image_id']
                or type(inventory) is not dict or type(inventory.get('outcomes')) is not dict
                or not inventory['outcomes']
                or any(value not in ('passed', 'xfailed', 'xpassed') for value in inventory['outcomes'].values())
                or _digest(canonical_json(inventory['outcomes']).encode()) != q['inventory_sha256']
                or _digest((self.baseline / 'reports/result.json').read_bytes()) != q['report_sha256']):
            raise ValueError('repository_baseline_qualification_mismatch')

    def check_budget(self):
        workspace = self.repository_probe_workspace
        if workspace is not None and workspace.blocked_reason:
            self.diagnostic_stop_reason = workspace.blocked_reason
        if self.final_submission is not None:
            raise BudgetExceeded('repository_submission_sealed')
        super().check_budget()

    def initial_context(self):
        self.check_budget()
        verify_snapshot(self.baseline)
        packet = {'task_type': 'repository_performance_optimization', 'strategy': self.strategy,
            'public_workload': json.loads(canonical_json(self.development_workload)),
            'allowed_paths': sorted(self.allowed_paths)[:32], 'allowed_path_count': len(self.allowed_paths),
            'files': sorted(self.files)[:50], 'file_count': len(self.files),
            'context_truncated': len(self.allowed_paths) > 32 or len(self.files) > 50,
            'probes': ['cpu', 'memory', 'benchmark'],
            'baseline_tests': {'status': self.before['status'], 'counts': self.before['counts'],
                'scope': 'Qualified original suite, including explicitly accepted historical XFAIL/XPASS.'},
            'evidence_index': [{key: item[key] for key in ('evidence_ref', 'kind', 'snapshot_id')}
                               for item in list(self.evidence.values())[-20:]],
            'diagnostic_summaries': [visible_evidence(item) for item in list(self.evidence.values())[-4:]
                                     if item['kind'] == 'repository_probe'],
            'submission_policy': 'First valid patch or conclusion seals this run. Hidden acceptance runs after Agent closure.'}
        if self.enable_reproduction:
            packet['reproduction'] = {'tools': ['propose_reproducer', 'run_reproducer'],
                'target': 'immutable baseline only', 'max_programs': self.max_repro_programs,
                'max_runs': self.max_repro_runs, 'trust': 'model_program_output'}
        while len(json.dumps(packet, ensure_ascii=False)) > 16000:
            packet['context_truncated'] = True
            if packet['files']:
                packet['files'].pop()
            elif packet['allowed_paths']:
                packet['allowed_paths'].pop()
            else:
                # Do not silently replace the frozen public workload with a
                # partial experiment contract or hand an unbounded prompt out.
                raise ValueError('repository_public_context_too_large')
        return packet

    def _get_repository_probe_workspace(self):
        if self.repository_probe_workspace is None:
            from .repository_probes import RepositoryProbeWorkspace
            self.repository_probe_workspace = RepositoryProbeWorkspace(
                self.baseline, self.output / 'repository-probes', self.before['image_id'],
                self.development_workload, max_runs=self.max_probe_runs,
                execution_control=_SessionControl(self), admit=lambda operation: self.check_budget())
        return self.repository_probe_workspace

    def run_diagnostic(self, probe, hypothesis_id=None, attempt=0, case_id=None):
        with self.lock:
            self.reserve_tool('run_probe')
            if type(attempt) is not int or attempt != 0:
                raise EditRejected('repository_probe_requires_baseline')
            if hypothesis_id is not None and hypothesis_id not in self.hypotheses:
                raise EditRejected('unknown_hypothesis')
            workspace = self._get_repository_probe_workspace()
            try:
                result = self.tool_runtime.invoke('local', 'run_repository_probe',
                    {'probe': probe, 'case_id': case_id},
                    lambda: workspace.run_probe(probe, case_id=case_id), check=_SessionControl(self).check)
            finally:
                self.probe_runs = workspace.runs
                if workspace.cleanup_ok is not True:
                    self.diagnostic_stop_reason = 'repository_probe_cleanup_unconfirmed'
                elif workspace.blocked_reason:
                    self.diagnostic_stop_reason = workspace.blocked_reason
            ref = self._probe_evidence_by_report.get(result['report_ref'])
            if ref is None:
                observation = {key: result[key] for key in ('status', 'cleanup_ok', 'input_integrity_ok',
                    'image_id', 'public_workload_sha256', 'workload_id', 'probe', 'case_id',
                    'input_sha256', 'adapter_sha256', 'report_ref', 'report_sha256', 'summary')}
                observation.update(hypothesis_id=hypothesis_id, transport='local')
                ref = self._add_evidence('repository_probe', observation, result['snapshot_ref'])
                self._probe_evidence_by_report[result['report_ref']] = ref
            self.record('repository_probe_finished', probe=probe, case_id=result['case_id'],
                evidence_ref=ref, status=result['status'], cache_hit=result['cache_hit'],
                dispatched=result['dispatched'], cleanup_ok=result['cleanup_ok'])
            self.save('running')
            self.check_budget()
            return {**visible_evidence(self.evidence[ref]), 'reused': result['cache_hit']}

    def read_probe_report(self, evidence_ref, section='summary', scale=None, offset=0, limit=5):
        self.reserve_tool('read_probe_report')
        self._check_refs([evidence_ref])
        item = self.evidence[evidence_ref]
        if item['kind'] != 'repository_probe' or scale is not None:
            raise EditRejected('not_a_repository_probe_reference')
        workspace = self._get_repository_probe_workspace()
        ref = item['observation']['report_ref']
        # A query cache cannot waive source/implementation/report integrity.
        # Validate against the original artifacts before consulting that cache.
        page = workspace.read_report(ref, section=section, offset=offset, limit=limit)
        return self.tool_runtime.invoke('local', 'query_repository_report',
            {'report_ref': ref, 'section': section, 'offset': offset, 'limit': limit},
            lambda: page,
            version=item['observation']['report_sha256'], check=_SessionControl(self).check)

    def _seal(self, decision, **details):
        if self.final_submission is not None:
            raise BudgetExceeded('repository_submission_sealed')
        receipt = {'schema_version': 1, 'task_id': self.development_workload['task_id'],
            'public_workload_sha256': self.public_workload_sha256,
            'baseline_ref': snapshot_id(self.manifest), 'decision': decision,
            'trust': 'sealed_submission_not_independent_acceptance', **details}
        path = self.output / 'final-submission.json'
        try:
            _atomic_write(path, receipt, create=True)
        except OSError:
            self.diagnostic_stop_reason = 'repository_submission_persistence_failed'
            raise BudgetExceeded(self.diagnostic_stop_reason) from None
        self.final_submission_sha256 = _digest(path.read_bytes())
        self.final_submission = receipt
        self.record('repository_submission_sealed', decision=decision,
                    submission_sha256=self.final_submission_sha256)

    def _verify_candidate(self, candidate, destination, timeout_seconds):
        proposed = verify_snapshot(candidate)
        old = {item['path']: item['sha256'] for item in self.manifest['files']}
        if (proposed.get('kind') != 'candidate'
                or proposed.get('base_snapshot_id') != snapshot_id(self.manifest)
                or set(old) != {item['path'] for item in proposed['files']}):
            raise ValueError('repository_submission_baseline_or_file_set_mismatch')
        changes = {item['path']: (candidate / 'workspace' / item['path']).read_bytes().decode('utf-8')
                   for item in proposed['files'] if item['sha256'] != old[item['path']]}
        rebuilt = create_candidate(self.baseline, destination, changes, set(self.allowed_paths))
        if snapshot_id(rebuilt) != snapshot_id(proposed):
            raise ValueError('repository_submission_reconstruction_mismatch')
        number = len(self.attempts)
        candidate_path = 'attempt-%02d/verification' % number
        if destination != self.output / candidate_path:
            raise ValueError('repository_submission_path_mismatch')
        self._seal('candidate', candidate_ref=snapshot_id(rebuilt), candidate_path=candidate_path,
                   patch_sha256=_digest((destination / 'patch.diff').read_bytes()))
        self.submitted_number = number
        return {'status': 'submitted', 'reason': 'awaiting_independent_repository_evaluation',
                'phase': 'submission', 'scope': 'Rebuilt and sealed, not a correctness or performance pass.'}

    def conclude_diagnosis(self, decision, explanation, evidence_refs):
        result = super().conclude_diagnosis(decision, explanation, evidence_refs)
        self._seal(decision, proposal=result)
        self.save('running')
        return {**result, 'status': 'proposal_sealed', 'scope': 'Review proposal, not independent acceptance.'}

    def save(self, state, **extra):
        probe, reproduction = self.repository_probe_workspace, getattr(self, 'reproduction_workspace', None)
        fields = {'task_type': 'repository_performance_optimization',
            'diagnostic_strategy': getattr(self, 'strategy', None),
            'diagnostic_workload': getattr(self, 'workload', None), 'diagnostic_transport': 'local',
            'tool_concurrency': 1,
            'tool_runtime': self.tool_runtime.summary() if hasattr(self, 'tool_runtime') else None,
            'diagnostic_probe_runs': getattr(self, 'probe_runs', 0),
            'diagnostic_stop_reason': getattr(self, 'diagnostic_stop_reason', None),
            'diagnostic_progress': {key: value for key, value in self.diagnostic_progress.items()
                                    if key not in {'records', 'processed_rounds'}},
            'hypotheses': self.hypotheses,
            'evidence_index': [{key: item[key] for key in ('evidence_ref', 'kind', 'snapshot_id', 'sha256')}
                               for item in self.evidence.values()],
            'final_decision': self.final_decision,
            'public_workload_sha256': self.public_workload_sha256,
            'baseline_qualification': self.baseline_qualification,
            'selected_attempt': self.submitted_number,
            'final_submission_ref': 'final-submission.json' if self.final_submission is not None else None,
            'final_submission_sha256': self.final_submission_sha256,
            'probe_cleanup_ok': probe is None or probe.cleanup_ok is True,
            'reproduction_cleanup_ok': reproduction is None or reproduction.cleanup_ok is True,
            'cleanup_ok': (probe is None or probe.cleanup_ok is True)
                          and (reproduction is None or reproduction.cleanup_ok is True),
            'submission_policy': 'One valid sealed submission; hidden evaluation never returns to this Agent.',
            **extra}
        if self.final_submission is not None:
            if self.final_submission['decision'] == 'candidate':
                fields.update(status='submitted', patch=self.final_submission['candidate_path'] + '/patch.diff')
            else:
                fields.update(status='proposal_sealed', reason='awaiting_independent_review')
        return RepairSession.save(self, state, **fields)
