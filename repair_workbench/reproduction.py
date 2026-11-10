"""Version-bound reproduction programs in disposable, resource-limited containers.

Programs are parsed, never imported on the host. AST checks provide syntax/size
preflight only; Docker supplies the controlled-project execution boundary.
This module does not infer that an observed symptom is a confirmed business bug.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
from uuid import uuid4

from . import reproduction_worker
from .evaluation_budget import _atomic_write
from .snapshot import snapshot_id, verify_snapshot
from .session import BudgetExceeded


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def resolve_image(image):
    if type(image) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', image):
        raise ValueError('invalid reproduction image')
    value = subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                    stderr=subprocess.PIPE, timeout=15).decode().strip()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', value):
        raise ValueError('invalid immutable image identity')
    return value


class ReproductionWorkspace:
    """Application-owned execution workspace, separate from source and grading.

    ``admit`` can connect propose/run to the main session's shared tool budget;
    ``execution_control.check`` connects cancellation/deadline. No provider or
    model is loaded here. The eventual Agent tools must wire both explicitly.
    """
    def __init__(self, snapshot, output, image, *, max_programs=3, max_runs=6,
                 execution_control=None, admit=None):
        for value in (max_programs, max_runs):
            if type(value) is not int or not 1 <= value <= 12:
                raise ValueError('reproduction counts must be integers in 1..12')
        if admit is not None and not callable(admit):
            raise ValueError('admission callback must be callable')
        self.snapshot, self.output = Path(snapshot).resolve(strict=True), Path(output).resolve()
        self.manifest = verify_snapshot(self.snapshot)
        self.snapshot_ref = snapshot_id(self.manifest)
        source = Path(self.manifest['source_repo']).resolve()
        if any(self.output == p or p in self.output.parents for p in (source, self.snapshot)):
            raise ValueError('reproduction output must be outside source/snapshot')
        self.image_id = resolve_image(image)
        self.wrapper_hash = _hash(Path(reproduction_worker.__file__).read_bytes())
        self.max_programs, self.max_runs = max_programs, max_runs
        self.control, self.admit = execution_control, admit
        self.programs, self.proposals, self.runs = {}, 0, 0
        self.blocked_reason = None
        self._lock = threading.RLock()
        self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.wrapper_path = self.output / '_worker.py'
        self.wrapper_path.write_bytes(Path(reproduction_worker.__file__).read_bytes())
        if _hash(self.wrapper_path.read_bytes()) != self.wrapper_hash:
            raise ValueError('reproduction_wrapper_changed')
        self._save()

    def _save(self):
        _atomic_write(self.output / 'workspace.json', {
            'snapshot_ref': self.snapshot_ref, 'programs': self.programs,
            'image_id': self.image_id, 'wrapper_sha256': self.wrapper_hash,
            'proposals': self.proposals, 'runs': self.runs,
            'blocked_reason': self.blocked_reason,
            'limits': {'max_programs': self.max_programs, 'max_runs': self.max_runs}})

    def _check(self, operation):
        if self.admit is not None:
            self.admit(operation)
        if self.control is not None:
            self.control.check()
        if self.blocked_reason is not None:
            raise ValueError(self.blocked_reason)
        self._verify_inputs()

    def _verify_inputs(self, program=None, program_hash=None):
        if snapshot_id(verify_snapshot(self.snapshot)) != self.snapshot_ref:
            raise ValueError('reproduction_snapshot_changed')
        if _hash(Path(reproduction_worker.__file__).read_bytes()) != self.wrapper_hash:
            raise ValueError('reproduction_wrapper_changed')
        if self.wrapper_path.is_symlink() or _hash(self.wrapper_path.read_bytes()) != self.wrapper_hash:
            raise ValueError('reproduction_wrapper_changed')
        if program is not None and (program.is_symlink() or _hash(program.read_bytes()) != program_hash):
            raise ValueError('reproducer_program_changed')

    def propose_reproducer(self, code):
        with self._lock:
            self._check('propose_reproducer')
            if self.proposals >= self.max_programs:
                raise ValueError('reproduction_program_limit')
            self.proposals += 1
            self._save()  # Rejected proposals also consume their local allowance.
            if type(code) is not str or not 1 <= len(code.encode()) <= 16384:
                raise ValueError('reproducer must be UTF-8 code within 16 KiB')
            parsed = ast.parse(code)
            if sum(1 for _ in ast.walk(parsed)) > 4000:
                raise ValueError('reproducer AST is oversized')
            if not any(isinstance(n, ast.FunctionDef) and n.name == 'run' for n in parsed.body):
                raise ValueError('reproducer must define run()')
            digest = _hash(code.encode())
            ref = 'rp-' + _hash((self.snapshot_ref + self.image_id + self.wrapper_hash + digest).encode())[:32]
            if ref not in self.programs:
                path = self.output / ref
                path.mkdir(mode=0o700)
                (path / 'program.py').write_text(code)
                (path / 'program.py').chmod(0o600)
                self.programs[ref] = {'code_sha256': digest, 'snapshot_ref': self.snapshot_ref}
                self._save()
            return {'reproducer_ref': ref, **self.programs[ref],
                    'scope': 'proposed program; behavior must be independently checked'}

    def run_reproducer(self, reproducer_ref, *, timeout_seconds=30, max_output_bytes=65536):
        with self._lock:
            self._check('run_reproducer')
            if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 60):
                raise ValueError('reproducer timeout must be within 60 seconds')
            if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 131072:
                raise ValueError('invalid reproduction output limit')
            if type(reproducer_ref) is not str or reproducer_ref not in self.programs:
                raise ValueError('unknown_reproducer_ref')
            if self.runs >= self.max_runs:
                raise ValueError('reproduction_run_limit')
            program = self.output / reproducer_ref / 'program.py'
            program_hash = self.programs[reproducer_ref]['code_sha256']
            self._verify_inputs(program, program_hash)
            self.runs += 1
            self._save()
            output = self.output / ('run-%02d' % self.runs)
            output.mkdir()
            wrapper = self.wrapper_path
            name = 'repair-reproducer-' + uuid4().hex[:16]
            result = {'status': 'execution_error', 'cleanup_ok': False,
                      'reproducer_ref': reproducer_ref, 'snapshot_ref': self.snapshot_ref,
                      'code_sha256': self.programs[reproducer_ref]['code_sha256'],
                      'image_id': self.image_id, 'wrapper_sha256': self.wrapper_hash,
                      'trust': 'model_program_output',
                      'scope': 'untrusted reproduction observation; not an independent verdict'}
            process, data = None, bytearray()
            started = time.monotonic()
            try:
                if any(',' in str(p) for p in (self.snapshot, program, wrapper)):
                    raise ValueError('unsupported reproduction mount path')
                self._verify_inputs(program, program_hash)
                if resolve_image(self.image_id) != self.image_id:
                    raise ValueError('reproduction_image_changed')
                if self.control is not None:
                    self.control.check()
                if time.monotonic() - started >= timeout_seconds:
                    result['status'] = 'timeout'
                    return result
                command = ['docker', 'run', '--pull', 'never', '--name', name, '--network', 'none',
                    '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '256m', '--cpus', '1',
                    '--pids-limit', '64', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
                    '--mount', f'type=bind,src={self.snapshot / "workspace"},dst=/workspace,readonly',
                    '--mount', f'type=bind,src={program},dst=/reproducer/program.py,readonly',
                    '--mount', f'type=bind,src={wrapper},dst=/trusted/worker.py,readonly',
                    '--workdir', '/workspace', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                    '--env', 'HOME=/tmp', '--entrypoint', 'python', self.image_id, '-I', '/trusted/worker.py']
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        if self.control is not None:
                            self.control.check()
                        if time.monotonic() - started >= timeout_seconds:
                            result['status'] = 'timeout'
                            break
                        if not selector.select(.05):
                            continue
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            process.wait(timeout=5)
                            result['returncode'] = process.returncode
                            if process.returncode == 0:
                                try:
                                    packet = json.loads(bytes(data).decode().splitlines()[-1],
                                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite output')))
                                    if type(packet.get('schema_version')) is not int or packet['schema_version'] != 1:
                                        raise ValueError('invalid observation protocol')
                                    result.update(status='completed', observation=packet['observation'])
                                except (ValueError, KeyError, IndexError, AttributeError, UnicodeError, RecursionError):
                                    result['status'] = 'report_error'
                            break
                        remaining = max_output_bytes - len(data)
                        data.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            result['status'] = 'output_limit'
                            break
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                result['error_type'] = type(exc).__name__
            except (BudgetExceeded, KeyboardInterrupt) as exc:
                result.update(status='stopped', reason=str(exc) if isinstance(exc, BudgetExceeded)
                              else 'cancel_requested')
                raise
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
                    check = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
                    result['cleanup_ok'] = check.returncode != 0 and 'no such' in check.stderr.lower()
                except (OSError, subprocess.SubprocessError) as exc:
                    result['cleanup_error_type'] = type(exc).__name__
                try:
                    self._verify_inputs(program, program_hash)
                    result['input_integrity_ok'] = True
                except (OSError, ValueError) as exc:
                    result.update(input_integrity_ok=False, integrity_error_type=type(exc).__name__)
                    if result['status'] != 'stopped':
                        result['status'] = 'input_changed'
                    self.blocked_reason = 'reproduction_inputs_changed'
                if not result['cleanup_ok']:
                    result['status'] = 'cleanup_unconfirmed'
                    self.blocked_reason = 'reproduction_cleanup_unconfirmed'
                (output / 'stdout.log').write_bytes(data)
                result['captured_bytes'] = len(data)
                result['elapsed_seconds'] = round(time.monotonic() - started, 6)
                _atomic_write(output / 'result.json', result, create=True)
                self._save()
            return result
