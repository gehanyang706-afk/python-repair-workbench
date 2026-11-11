"""Version-bound public repository observations, separate from final acceptance.

Only a reviewed development projection enters this workspace. The application
owns sampling, image and dispatch limits; the model selects a registered probe
and public case. Cached observations never waive root tool admission or checks.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import statistics
import subprocess
import threading
import time
from uuid import uuid4

from . import repository_measure_worker as registry
from . import repository_measurement, repository_probe_worker
from .evaluation_budget import _atomic_write
from .reproduction import resolve_image
from .session import BudgetExceeded
from .snapshot import git, snapshot_id, verify_snapshot
from .workload_contract import canonical_json, input_sha256


PROBES = ('cpu', 'memory', 'benchmark')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_DEFAULT_SAMPLING = {'warmups': 1, 'repeats': 3, 'timeout_seconds': 30,
                     'batch_iterations': 1, 'gc_policy': 'unmodified',
                     'warmup_seconds': 0, 'cpu_affinity': None}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _digest(path):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file(), 'repository_probe_implementation_unavailable')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def validate_development_workload(data):
    """Validate the exact public projection; never accept hidden evaluator fields."""
    fields = {'schema_version', 'task_id', 'repo', 'base_commit', 'workload_id',
              'workload_version', 'goal', 'thresholds', 'development_inputs', 'semantic_contract'}
    _require(type(data) is dict and set(data) == fields, 'invalid_public_workload_fields')
    canonical_json(data)
    _require(type(data['schema_version']) is int and data['schema_version'] == 1,
             'invalid_public_workload_version')
    for key in ('task_id', 'workload_version'):
        _require(type(data[key]) is str and _ID.fullmatch(data[key]), 'invalid_public_workload_identity')
    _require(type(data['repo']) is str and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', data['repo']),
             'invalid_public_repository')
    _require(type(data['base_commit']) is str and re.fullmatch(r'[a-f0-9]{40}', data['base_commit']),
             'invalid_public_base_commit')
    _require(type(data['workload_id']) is str and data['workload_id'] in registry.WORKLOADS,
             'unregistered_public_workload')
    _require(data['goal'] in ('speed', 'memory'), 'invalid_public_goal')
    thresholds = data['thresholds']
    _require(type(thresholds) is dict and set(thresholds) == {'min_speedup', 'min_memory_reduction',
        'max_time_ratio', 'max_memory_ratio', 'max_relative_noise'}, 'invalid_public_thresholds')
    _require(all(_number(thresholds[k], 0, .99) for k in ('min_speedup', 'min_memory_reduction'))
             and all(_number(thresholds[k], 1, 4) for k in ('max_time_ratio', 'max_memory_ratio'))
             and _number(thresholds['max_relative_noise'], 0, 1)
             and thresholds['max_relative_noise'] > 0, 'invalid_public_threshold_values')
    primary, secondary = (('min_speedup', 'min_memory_reduction') if data['goal'] == 'speed'
                          else ('min_memory_reduction', 'min_speedup'))
    _require(thresholds[primary] > 0 and thresholds[secondary] == 0, 'public_threshold_goal_mismatch')
    semantic = data['semantic_contract']
    _require(type(semantic) is dict and set(semantic) == {'strategy', 'baseline_behavior_confirmed',
        'output_comparison', 'contract_reference'}, 'invalid_public_semantic_fields')
    _require(semantic['strategy'] in ('baseline_equivalence', 'trusted_reference')
             and type(semantic['baseline_behavior_confirmed']) is bool
             and (semantic['strategy'] != 'baseline_equivalence' or semantic['baseline_behavior_confirmed'])
             and semantic['output_comparison'] in ('canonical_json', 'exact_bytes')
             and type(semantic['contract_reference']) is str
             and 1 <= len(semantic['contract_reference']) <= 512
             and not any(c in semantic['contract_reference'] for c in '\n\r\x00'),
             'invalid_public_semantic_contract')
    cases = data['development_inputs']
    _require(type(cases) is list and 1 <= len(cases) <= 128, 'invalid_public_case_count')
    ids, hashes, roles = set(), set(), set()
    for case in cases:
        _require(type(case) is dict and set(case) == {'case_id', 'role', 'input', 'sha256'},
                 'invalid_public_case_fields')
        _require(type(case['case_id']) is str and _ID.fullmatch(case['case_id'])
                 and case['case_id'] not in ids, 'invalid_or_duplicate_public_case')
        _require(case['role'] in ('target', 'guardrail'), 'invalid_public_case_role')
        registry.validate_input(data['workload_id'], case['input'])
        _require(type(case['sha256']) is str and _SHA.fullmatch(case['sha256'])
                 and case['sha256'] == input_sha256(case['input']) and case['sha256'] not in hashes,
                 'invalid_or_duplicate_public_input')
        ids.add(case['case_id']); hashes.add(case['sha256']); roles.add(case['role'])
    _require('target' in roles, 'public_target_case_required')
    return json.loads(canonical_json(data))


def _sampling(value):
    _require(value is None or type(value) is dict and set(value) <= set(_DEFAULT_SAMPLING),
             'invalid_public_sampling_fields')
    result = {**_DEFAULT_SAMPLING, **(value or {})}
    _require(_number(result['timeout_seconds'], 0, 120) and result['timeout_seconds'] > 0,
             'invalid_public_probe_timeout')
    affinity = result['cpu_affinity']
    _require(affinity is None or type(affinity) is str and re.fullmatch(r'[0-9]{1,2}', affinity),
             'invalid_public_probe_affinity')
    # The same registered worker validates all sampling fields for every probe.
    registry.validate_job({'workload_id': 'sql-comments-v3', 'input': {'size': 1, 'kind': 'line_comments'},
        'input_sha256': input_sha256({'size': 1, 'kind': 'line_comments'}), 'probe': 'benchmark',
        **{k: v for k, v in result.items() if k not in ('timeout_seconds', 'cpu_affinity')}})
    return copy.deepcopy(result)


def validate_cpu_report(report, job):
    fields = {'schema_version', 'workload_id', 'probe', 'input_sha256', 'profiled_seconds',
              'output_sha256', 'output_lengths', 'top_functions', 'environment'}
    _require(type(report) is dict and set(report) == fields and type(report['schema_version']) is int
             and report['schema_version'] == 1 and report['probe'] == 'cpu'
             and all(report[k] == job[k] for k in ('workload_id', 'input_sha256')),
             'invalid_cpu_report_identity')
    for key in ('profiled_seconds', 'output_sha256', 'output_lengths'):
        _require(type(report[key]) is list and len(report[key]) == job['repeats'], 'invalid_cpu_sample_count')
    _require(all(_number(v, 0, 2**63 - 1) for v in report['profiled_seconds'])
             and all(type(v) is str and _SHA.fullmatch(v) for v in report['output_sha256'])
             and all(type(v) is int and 0 <= v <= 2**63 - 1 for v in report['output_lengths']),
             'invalid_cpu_samples')
    environment = report['environment']
    _require(type(environment) is dict and set(environment) == {'python', 'implementation', 'system', 'machine', 'clock'}
             and all(type(v) is str and 1 <= len(v) <= 120 for v in environment.values())
             and environment['clock'] == 'perf_counter_ns', 'invalid_cpu_environment')
    functions = report['top_functions']
    _require(type(functions) is list and len(functions) <= 15, 'invalid_cpu_function_count')
    for row in functions:
        _require(type(row) is dict and set(row) == {'file', 'line', 'function', 'calls', 'own_seconds', 'cumulative_seconds'},
                 'invalid_cpu_function_fields')
        for key in ('file', 'function'):
            _require(type(row[key]) is str and 1 <= len(row[key]) <= 180
                     and not any(c in row[key] for c in '\n\r\x00'), 'invalid_cpu_function_text')
        path = PurePosixPath(row['file'])
        _require(not path.is_absolute() and '..' not in path.parts and '\\' not in row['file'],
                 'cpu_report_absolute_or_traversing_path')
        _require(type(row['line']) is int and 0 <= row['line'] <= 2**31 - 1
                 and type(row['calls']) is int and 0 <= row['calls'] <= 2**63 - 1
                 and all(_number(row[k], 0, 2**63 - 1) for k in ('own_seconds', 'cumulative_seconds')),
                 'invalid_cpu_function_metrics')
    return report


def _read_json(path, limit=256 * 1024):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file() and path.stat().st_size <= limit, 'invalid_probe_report_file')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, 'duplicate_probe_report_field')
            result[key] = value
        return result
    return json.loads(path.read_bytes().decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_probe_report')))


def _run_cpu_probe(snapshot, destination, image_id, job, *, timeout_seconds, execution_control, cpu_affinity=None):
    """CPU counterpart of the fixed measurement runner; no final-data mounts."""
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    worker_path, registry_path = Path(repository_probe_worker.__file__).resolve(), Path(registry.__file__).resolve()
    manifest = verify_snapshot(snapshot)
    hashes = {worker_path: _digest(worker_path), registry_path: _digest(registry_path)}
    job_path = destination / 'job.json'
    _atomic_write(job_path, job, create=True)
    job_hash = _digest(job_path)
    name = 'repair-repository-cpu-' + uuid4().hex[:16]
    result = {'status': 'execution_error', 'cleanup_ok': False, 'input_integrity_ok': False,
              'snapshot_ref': snapshot_id(manifest), 'image_id': image_id,
              'adapter_sha256': hashes[registry_path]}
    process, captured, started = None, bytearray(), time.monotonic()
    def check():
        if execution_control is not None:
            execution_control.check()
        if time.monotonic() - started >= timeout_seconds:
            raise subprocess.TimeoutExpired('repository CPU probe', timeout_seconds)
    try:
        check()
        _require(not any(',' in str(p) for p in (snapshot, worker_path, registry_path, job_path)),
                 'unsupported_cpu_mount_path')
        command = ['docker', 'run', '--pull', 'never', '--name', name, '--network', 'none',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--cpus', '1', '--memory', '256m', '--pids-limit', '64',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
            '--mount', f'type=bind,src={snapshot / "workspace"},dst=/workspace,readonly',
            '--mount', f'type=bind,src={worker_path},dst=/trusted/worker.py,readonly',
            '--mount', f'type=bind,src={registry_path},dst=/trusted/registry.py,readonly',
            '--mount', f'type=bind,src={job_path},dst=/trusted/job.json,readonly',
            '--workdir', '/workspace', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/tmp',
            '--entrypoint', 'python', image_id, '-I', '/trusted/worker.py', '--job', '/trusted/job.json']
        if cpu_affinity is not None:
            command[2:2] = ['--cpuset-cpus', cpu_affinity]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                check()
                if not selector.select(.05):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    process.wait(timeout=5)
                    break
                remaining = 65536 - len(captured)
                captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    result['status'] = 'output_limit'
                    break
        result['returncode'] = process.returncode
        if result['status'] != 'output_limit' and process.returncode == 0:
            lines = captured.decode('utf-8').splitlines()
            _require(bool(lines) and len(lines[-1].encode()) <= 65536, 'missing_cpu_report')
            report_path = destination / 'cpu-report.json'
            def unique(pairs):
                values = {}
                for key, value in pairs:
                    _require(key not in values, 'duplicate_cpu_report_field')
                    values[key] = value
                return values
            report = validate_cpu_report(json.loads(lines[-1], object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_cpu_report'))), job)
            _atomic_write(report_path, report, create=True)
            result.update(status='ok', cpu_report=report, report_sha256=_digest(report_path))
    except subprocess.TimeoutExpired:
        result['status'] = 'timeout'
    except (BudgetExceeded, KeyboardInterrupt):
        result['status'] = 'stopped'
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result['error_type'] = type(exc).__name__
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                result['process_cleanup_error_type'] = type(exc).__name__
            finally:
                process.stdout.close()
        try:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
            inspected = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
            result['cleanup_ok'] = inspected.returncode != 0 and 'no such' in inspected.stderr.lower()
        except (OSError, subprocess.SubprocessError):
            result['cleanup_ok'] = False
        try:
            result['input_integrity_ok'] = (snapshot_id(verify_snapshot(snapshot)) == result['snapshot_ref']
                and all(_digest(p) == digest for p, digest in hashes.items()) and _digest(job_path) == job_hash)
        except (OSError, ValueError):
            result['input_integrity_ok'] = False
        if not result['cleanup_ok']:
            result['status'] = 'cleanup_unconfirmed'
        elif not result['input_integrity_ok']:
            result['status'] = 'input_changed'
        result['elapsed_seconds'] = time.monotonic() - started
        (destination / 'stdout.log').write_bytes(captured)
        _atomic_write(destination / 'result.json', result, create=True)
    return result


class RepositoryProbeWorkspace:
    """One owned public baseline, bounded physical probes and immutable reports."""
    def __init__(self, snapshot, output, image, development_workload, *, max_runs=6,
                 execution_control=None, admit=None, sampling=None):
        self.development_workload = validate_development_workload(development_workload)
        self.public_workload_sha256 = _hash(self.development_workload)
        self.sampling = _sampling(sampling)
        self.sampling_sha256 = _hash(self.sampling)
        _require(type(max_runs) is int and 1 <= max_runs <= 64, 'invalid_public_probe_run_limit')
        _require(admit is None or callable(admit), 'invalid_public_probe_admission')
        self.snapshot, self.output = Path(snapshot).resolve(strict=True), Path(output).resolve()
        manifest = verify_snapshot(self.snapshot)
        expected = self.development_workload['base_commit']
        matches = manifest['commit'] == expected
        if not matches and manifest.get('upstream_commit') == expected and manifest.get('upstream_tree'):
            matches = manifest['upstream_tree'] == git(Path(manifest['source_repo']), 'rev-parse',
                                                       manifest['commit'] + '^{tree}').decode().strip()
        _require(matches, 'public_workload_baseline_mismatch')
        _require(not any(self.output == p or p in self.output.parents for p in
                         (self.snapshot, Path(manifest['source_repo']).resolve())), 'public_probe_output_inside_source')
        self.snapshot_ref, self.manifest_sha256 = snapshot_id(manifest), _digest(self.snapshot / 'manifest.json')
        self.adapter_sha256 = _digest(registry.__file__)
        self.implementation = {str(Path(m.__file__).resolve()): _digest(m.__file__)
                               for m in (registry, repository_probe_worker, repository_measurement)}
        self.implementation[str(Path(__file__).resolve())] = _digest(__file__)
        if execution_control is not None:
            execution_control.check()
        self.image_id = resolve_image(image)
        self.control, self.admit, self.max_runs = execution_control, admit, max_runs
        self.runs, self.cleanup_ok, self.blocked_reason = 0, True, None
        self.cache, self.reports = {}, {}
        self._lock = threading.RLock()
        self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        _atomic_write(self.output / 'public-workload.json', self.development_workload, create=True)
        self._save()

    def _save(self):
        try:
            _atomic_write(self.output / 'workspace.json', {'schema_version': 1, 'snapshot_ref': self.snapshot_ref,
                'public_workload_sha256': self.public_workload_sha256, 'image_id': self.image_id,
                'adapter_sha256': self.adapter_sha256, 'sampling': self.sampling, 'runs': self.runs,
                'max_runs': self.max_runs, 'cleanup_ok': self.cleanup_ok, 'blocked_reason': self.blocked_reason,
                'reports': {ref: {'sha256': v['sha256'], 'path': v['path']} for ref, v in self.reports.items()}})
        except BaseException:
            self.blocked_reason = 'repository_probe_persistence_failed'
            raise

    def _block(self, reason):
        if self.blocked_reason is None or reason == 'repository_probe_cleanup_unconfirmed':
            self.blocked_reason = reason
        self._save()

    def _verify_inputs(self):
        try:
            _require(snapshot_id(verify_snapshot(self.snapshot)) == self.snapshot_ref
                     and _digest(self.snapshot / 'manifest.json') == self.manifest_sha256
                     and _hash(self.development_workload) == self.public_workload_sha256
                     and _hash(self.sampling) == self.sampling_sha256
                     and _read_json(self.output / 'public-workload.json') == self.development_workload
                     and all(_digest(p) == h for p, h in self.implementation.items()),
                     'repository_probe_inputs_changed')
        except (OSError, ValueError):
            self._block('repository_probe_inputs_changed')
            raise

    def _check(self, operation):
        if self.admit is not None:
            self.admit(operation)
        if self.control is not None:
            self.control.check()
        _require(self.blocked_reason is None, self.blocked_reason or 'repository_probe_blocked')
        self._verify_inputs()

    def _binding(self, probe, case):
        return {'snapshot_ref': self.snapshot_ref, 'image_id': self.image_id,
                'public_workload_sha256': self.public_workload_sha256,
                'workload_id': self.development_workload['workload_id'], 'probe': probe,
                'case_id': case['case_id'], 'input_sha256': case['sha256'], 'adapter_sha256': self.adapter_sha256}

    def _recover_cleanup(self, run_path):
        try:
            result = _read_json(run_path / 'result.json', 2 * 1024 * 1024)
            self.cleanup_ok = result.get('cleanup_ok') is True
            return result
        except (OSError, ValueError):
            return None

    def run_probe(self, probe, case_id=None):
        with self._lock:
            self._check('run_probe')
            _require(type(probe) is str and probe in PROBES, 'unknown_repository_probe')
            cases = self.development_workload['development_inputs']
            case_id = cases[0]['case_id'] if case_id is None else case_id
            _require(type(case_id) is str, 'invalid_public_case_id')
            case = next((v for v in cases if v['case_id'] == case_id), None)
            _require(case is not None, 'unknown_public_case_id')
            binding = self._binding(probe, case)
            key = _hash({'binding': binding, 'sampling': self.sampling, 'implementation': self.implementation})
            if key in self.cache:
                report = self._report(self.cache[key])
                return {**report, 'cache_hit': True, 'dispatched': False}
            _require(self.runs < self.max_runs, 'repository_probe_run_limit')
            job = {'workload_id': binding['workload_id'], 'input': copy.deepcopy(case['input']),
                   'input_sha256': binding['input_sha256'], 'probe': probe,
                   **{k: v for k, v in self.sampling.items() if k not in ('timeout_seconds', 'cpu_affinity')}}
            self.runs += 1
            self.cleanup_ok = False
            self._save()
            run_path = self.output / ('run-%02d' % self.runs)
            result = None
            try:
                options = {'timeout_seconds': self.sampling['timeout_seconds'], 'execution_control': self.control,
                           'cpu_affinity': self.sampling['cpu_affinity']}
                result = (_run_cpu_probe(self.snapshot, run_path, self.image_id, job, **options)
                          if probe == 'cpu' else repository_measurement.run_repository_measurement(
                              self.snapshot, run_path, self.image_id, job, **options))
                self.cleanup_ok = result.get('cleanup_ok') is True
                self._verify_inputs()
                _require(result.get('input_integrity_ok') is True, 'repository_probe_inputs_changed')
                _require(self.cleanup_ok, 'repository_probe_cleanup_unconfirmed')
                _require(result.get('status') == 'ok', 'repository_probe_execution_failed')
                _require(all(result.get(k) == binding[k] for k in ('snapshot_ref', 'image_id', 'adapter_sha256')),
                         'repository_probe_result_binding_mismatch')
                payload_key = 'cpu_report' if probe == 'cpu' else 'measurement'
                payload = (validate_cpu_report(result[payload_key], job) if probe == 'cpu'
                           else repository_measurement.validate_measurement(result[payload_key], job))
                outputs = set(zip(payload['output_sha256'], payload['output_lengths']))
                _require(len(outputs) == 1, 'repository_probe_output_not_deterministic')
                digest, length = outputs.pop()
                summary = ({'profiled_seconds': statistics.median(payload['profiled_seconds']),
                            'top_functions': payload['top_functions'][:3], 'metric': 'profiled_wall_seconds'}
                           if probe == 'cpu' else {'metric': ('unprofiled_wall_seconds' if probe == 'benchmark'
                            else 'python_traced_peak_bytes'), 'median': statistics.median(payload['samples']),
                            'samples': payload['samples'], 'output_sha256': digest, 'output_length': length})
                ref = 'rpp-' + key[:32]
                report = {**binding, 'status': 'ok', 'cleanup_ok': True, 'input_integrity_ok': True,
                          'report_ref': ref, 'summary': summary, payload_key: payload,
                          'scope': 'public development observation; not independent final acceptance'}
                path = self.output / (ref + '.json')
                _atomic_write(path, report, create=True)
                self._verify_inputs()
                self.reports[ref] = {'path': path.name, 'sha256': _digest(path)}
                self.cache[key] = ref
                self._save()
                return {**copy.deepcopy(report), 'report_sha256': self.reports[ref]['sha256'],
                        'cache_hit': False, 'dispatched': True}
            except BaseException as exc:
                if result is None:
                    result = self._recover_cleanup(run_path)
                reason = ('repository_probe_cleanup_unconfirmed' if not self.cleanup_ok else
                          str(exc) if isinstance(exc, ValueError) and str(exc).startswith('repository_probe_')
                          else 'repository_probe_stopped' if isinstance(exc, (BudgetExceeded, KeyboardInterrupt))
                          else 'repository_probe_execution_failed')
                self._block(reason)
                if isinstance(exc, (BudgetExceeded, KeyboardInterrupt)) or not isinstance(exc, Exception):
                    raise
                return {**binding, 'status': ('cleanup_unconfirmed' if not self.cleanup_ok else
                        'input_changed' if self.blocked_reason == 'repository_probe_inputs_changed' else
                        (result or {}).get('status', 'execution_error') if (result or {}).get('status') != 'ok'
                        else 'execution_error'), 'cleanup_ok': self.cleanup_ok,
                        'input_integrity_ok': (result or {}).get('input_integrity_ok') is True
                            and self.blocked_reason != 'repository_probe_inputs_changed',
                        'cache_hit': False, 'dispatched': True, 'report_ref': None, 'report_sha256': None,
                        'summary': {'reason': self.blocked_reason}, 'error_type': type(exc).__name__}

    def _report(self, ref):
        _require(type(ref) is str and ref in self.reports, 'unknown_repository_report')
        info = self.reports[ref]
        path = self.output / info['path']
        try:
            _require(_digest(path) == info['sha256'], 'repository_probe_report_changed')
            return {**_read_json(path), 'report_sha256': info['sha256']}
        except (OSError, ValueError):
            self._block('repository_probe_report_changed')
            raise

    def read_report(self, ref, section='summary', offset=0, limit=5):
        with self._lock:
            self._check('read_probe_report')
            _require(section in ('summary', 'samples', 'top_functions')
                     and type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 10,
                     'invalid_repository_report_page')
            report = self._report(ref)
            if section == 'summary':
                values = [report['summary']]
            elif section == 'top_functions':
                values = report.get('cpu_report', {}).get('top_functions', [])
            else:
                values = report.get('measurement', {}).get('samples', report.get('cpu_report', {}).get('profiled_seconds', []))
            page = values[offset:offset + limit]
            result = {k: report[k] for k in ('snapshot_ref', 'image_id', 'public_workload_sha256', 'workload_id',
                'probe', 'case_id', 'input_sha256', 'adapter_sha256', 'report_ref', 'report_sha256')}
            result.update(section=section, offset=offset, total=len(values), items=page,
                          next_offset=offset + len(page) if offset + len(page) < len(values) else None)
            _require(len(canonical_json(result).encode()) <= 8192, 'repository_report_page_too_large')
            return result
