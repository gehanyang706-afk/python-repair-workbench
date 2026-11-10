"""Run a registered, version-bound repository measurement in an owned container."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from uuid import uuid4

from . import repository_measure_worker as worker
from .evaluation_budget import _atomic_write
from .reproduction import resolve_image
from .session import BudgetExceeded
from .snapshot import snapshot_id, verify_snapshot


def validate_measurement(report, job):
    fields = {'schema_version', 'workload_id', 'probe', 'input_sha256', 'samples',
              'output_sha256', 'output_lengths', 'environment'}
    if (type(report) is not dict or set(report) != fields or type(report['schema_version']) is not int
            or report['schema_version'] != 1 or any(report[key] != job[key] for key in ('workload_id', 'probe', 'input_sha256'))):
        raise ValueError('measurement_protocol_or_identity_mismatch')
    samples, digests, lengths = (report[key] for key in ('samples', 'output_sha256', 'output_lengths'))
    if (any(type(value) is not list or len(value) != job['repeats'] for value in (samples, digests, lengths))
            or any(type(v) not in (int, float) or not 0 <= v <= 2**63 - 1 or not math.isfinite(v) for v in samples)
            or (job['probe'] == 'memory' and any(type(v) is not int for v in samples))
            or any(type(v) is not str or len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in digests)
            or any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in lengths)):
        raise ValueError('invalid_measurement_samples_or_outputs')
    environment = report['environment']
    if (type(environment) is not dict or set(environment) != {'python', 'implementation', 'system', 'machine', 'clock'}
            or any(type(v) is not str or not 1 <= len(v) <= 120 for v in environment.values())
            or environment['clock'] != 'perf_counter_ns'):
        raise ValueError('invalid_measurement_environment')
    return report


def run_repository_measurement(snapshot, destination, image, job, *, timeout_seconds=90,
                               execution_control=None, cpu_affinity=None):
    """Application/evaluator-owned job; never expose final jobs as Agent tools.

    Uses the fixed 1 CPU/256 MiB policy. Returned observations are valid only
    when status=ok, cleanup_ok and input_integrity_ok are all confirmed. JSON
    originates in a controlled project process, not an adversarial attestation.
    """
    worker.validate_job(job)
    job = json.loads(worker.canonical(job))
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120:
        raise ValueError('measurement_timeout_out_of_range')
    if cpu_affinity is not None and (type(cpu_affinity) is not str or not cpu_affinity.isascii()
            or not cpu_affinity.isdigit() or not 1 <= len(cpu_affinity) <= 2):
        raise ValueError('invalid_single_cpu_affinity')
    snapshot, destination = Path(snapshot).resolve(strict=True), Path(destination).resolve()
    manifest = verify_snapshot(snapshot)
    roots = (snapshot, Path(manifest['source_repo']).resolve())
    if any(destination == root or root in destination.parents for root in roots):
        raise ValueError('measurement_output_must_be_outside_source')
    worker_path = Path(worker.__file__).resolve(strict=True)
    worker_hash = hashlib.sha256(worker_path.read_bytes()).hexdigest()
    if execution_control:
        execution_control.check()
    image_id = resolve_image(image)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    job_path = destination / 'job.json'
    _atomic_write(job_path, job, create=True)
    job_hash = hashlib.sha256(job_path.read_bytes()).hexdigest()
    name = 'repair-repository-measure-' + uuid4().hex[:16]
    result = {'status': 'execution_error', 'cleanup_ok': False, 'input_integrity_ok': False,
              'snapshot_ref': snapshot_id(manifest), 'image_id': image_id,
              'adapter_sha256': worker_hash, 'job_sha256': job_hash,
              'execution_policy': {'cpus': 1, 'memory_bytes': 268435456, 'pids_limit': 64,
                                   'network': 'none', 'auto_pull': False, 'cpu_affinity': cpu_affinity}}
    started, process, captured = time.monotonic(), None, bytearray()
    def check():
        if execution_control:
            execution_control.check()
        if time.monotonic() - started >= timeout_seconds:
            raise subprocess.TimeoutExpired('registered repository measurement', timeout_seconds)
    try:
        check()
        if any(',' in str(p) for p in (snapshot, worker_path, destination)):
            raise ValueError('unsupported_measurement_mount_path')
        command = ['docker', 'run', '--pull', 'never', '--name', name, '--network', 'none',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--cpus', '1', '--memory', '256m', '--pids-limit', '64',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
            '--mount', f'type=bind,src={snapshot / "workspace"},dst=/workspace,readonly',
            '--mount', f'type=bind,src={worker_path},dst=/trusted/worker.py,readonly',
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
                remaining = 1024 * 1024 - len(captured)
                captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    result['status'] = 'output_limit'
                    break
        if result['status'] == 'output_limit':
            return result
        result['returncode'] = process.returncode
        if process.returncode == 0:
            def invalid(_):
                raise ValueError('nonfinite_measurement_number')
            def unique(pairs):
                values = {}
                for key, value in pairs:
                    if key in values:
                        raise ValueError('duplicate_measurement_field')
                    values[key] = value
                return values
            lines = captured.decode().splitlines()
            if not lines or len(lines[-1].encode()) > 256 * 1024:
                raise ValueError('missing_or_oversized_measurement_report')
            report = validate_measurement(json.loads(lines[-1], parse_constant=invalid,
                object_pairs_hook=unique), job)
            report_path = destination / 'measurement.json'
            _atomic_write(report_path, report, create=True)
            result.update(status='ok', measurement=report,
                          measurement_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
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
            probe = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
            result['cleanup_ok'] = probe.returncode != 0 and 'no such' in probe.stderr.lower()
        except (OSError, subprocess.SubprocessError):
            result['cleanup_ok'] = False
        try:
            result['input_integrity_ok'] = (snapshot_id(verify_snapshot(snapshot)) == result['snapshot_ref']
                and hashlib.sha256(worker_path.read_bytes()).hexdigest() == worker_hash
                and not job_path.is_symlink() and hashlib.sha256(job_path.read_bytes()).hexdigest() == job_hash)
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
