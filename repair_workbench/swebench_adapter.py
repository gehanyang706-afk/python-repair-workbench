"""SWE-bench import, issue-only workspaces and official-harness evaluation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import time
from uuid import uuid4

from .candidate import create_candidate, validate_source_path
from .edits import EditRejected
from .snapshot import create_snapshot, snapshot_id, verify_snapshot, snapshot_limits, documentation_link
from .session import RepairSession, BudgetExceeded
from .swebench_worker import (NETWORK_POLICIES, REQUESTS_NETWORK_POLICY,
                             execution_policy, network_plan)

HARNESS_COMMIT = '02e7a74ffd0b707aab73d203fe87bdc7c76afc8e'
PUBLIC_FIELDS = ('instance_id', 'repo', 'base_commit', 'problem_statement')
ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def load(path):
    return json.loads(Path(path).read_text())


def validate_record(row):
    required = {*PUBLIC_FIELDS, 'patch', 'test_patch', 'FAIL_TO_PASS', 'PASS_TO_PASS',
                'image', 'eval_script', 'log_parser', 'eval_type', 'version'}
    if not isinstance(row, dict) or not required <= row.keys():
        raise ValueError('modern SWE-bench row required, including official image and eval_script')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+', row['instance_id']):
        raise ValueError('invalid instance id')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', row['repo']):
        raise ValueError('invalid repository')
    if not row['instance_id'].startswith(row['repo'].replace('/', '__') + '-'):
        raise ValueError('repository/instance mismatch')
    if not re.fullmatch('[a-f0-9]{40}', row['base_commit']):
        raise ValueError('base commit must be pinned')
    if not isinstance(row['problem_statement'], str) or not 1 <= len(row['problem_statement']) <= 12000:
        raise ValueError('unsupported problem statement size')
    if not all(isinstance(row[k], str) and len(row[k].encode()) <= 4 * 1024 * 1024
               for k in ('patch', 'test_patch', 'image', 'eval_script', 'log_parser', 'eval_type', 'version')):
        raise ValueError('invalid evaluator fields')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', row['image']):
        raise ValueError('invalid image')
    if row['eval_type'] != 'pass_and_fail':
        raise ValueError('this track supports Python pass_and_fail tasks only')
    if not row['log_parser'].startswith('parse_log_'):
        raise ValueError('invalid log parser')
    for key in ('FAIL_TO_PASS', 'PASS_TO_PASS'):
        cases = json.loads(row[key]) if isinstance(row[key], str) else row[key]
        if (not isinstance(cases, list) or (key == 'FAIL_TO_PASS' and not cases)
                or any(not isinstance(c, str) or not c for c in cases) or len(set(cases)) != len(cases)):
            raise ValueError('invalid expected tests')
    if row.get('image_assets'):
        raise ValueError('multimodal assets are outside the Python source track')
    return row


def import_dataset(source, destination, *, dataset_name, revision, split='test'):
    """Local official export in JSON/JSONL; no network or automatic selection."""
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('dataset revision must be pinned')
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('dataset export exceeds 64 MiB')
    raw = source.read_bytes()
    rows = json.loads(raw) if source.suffix == '.json' else [
        json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    if not isinstance(rows, list) or not rows:
        raise ValueError('nonempty list of tasks required')
    ids = []
    for row in rows:
        validate_record(row)
        ids.append(row['instance_id'])
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate task id')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    tasks = []
    for row in rows:
        case = destination / row['instance_id']
        case.mkdir(mode=0o700)
        write(case / 'public.json', {k: row[k] for k in PUBLIC_FIELDS})
        write(case / 'evaluator.json', row)
        (case / 'evaluator.json').chmod(0o600)
        tasks.append({'instance_id': row['instance_id'],
                      'public_sha256': sha((case / 'public.json').read_bytes()),
                      'evaluator_sha256': sha((case / 'evaluator.json').read_bytes())})
    manifest = {'schema_version': 1, 'dataset_name': dataset_name, 'dataset_revision': revision,
                'split': split, 'source_sha256': sha(raw), 'tasks': tasks,
                'track': 'existing Python source files; no oracle-selected edit paths'}
    write(destination / 'manifest.json', manifest)
    return manifest


def task_record(dataset, instance_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+', instance_id):
        raise ValueError('invalid instance id')
    dataset = Path(dataset).resolve(strict=True)
    manifest = load(dataset / 'manifest.json')
    entries = [t for t in manifest['tasks'] if t['instance_id'] == instance_id]
    if len(entries) != 1:
        raise ValueError('task not registered')
    entry = entries[0]
    case = dataset / instance_id
    if case.is_symlink():
        raise ValueError('symlink task directory')
    for role in ('public', 'evaluator'):
        path = case / (role + '.json')
        if path.is_symlink() or sha(path.read_bytes()) != entry[role + '_sha256']:
            raise ValueError('task evidence changed')
    row = validate_record(load(case / 'evaluator.json'))
    if row['instance_id'] != instance_id:
        raise ValueError('task identity mismatch')
    if load(case / 'public.json') != {k: row[k] for k in PUBLIC_FIELDS}:
        raise ValueError('public projection mismatch')
    return row


def git_blobs(repo, revision):
    raw = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-rz', revision])
    files = {}
    for entry in raw.split(b'\0'):
        if not entry:continue
        header, path = entry.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        if kind != 'blob' or (mode not in ('100644', '100755')
                              and not (mode == '120000' and documentation_link(path.decode()))):
            raise ValueError('unsupported Git source entry')
        files[path.decode()] = oid
    return files


def prepare_workspace(dataset, instance_id, repo, output, source_roots=(), *, source_proof=None,
                      snapshot_profile='bounded-v1'):
    row = task_record(dataset, instance_id)
    output, repo = Path(output).resolve(), Path(repo).resolve(strict=True)
    private = Path(dataset).resolve()
    if output == private or output in private.parents or private in output.parents:
        raise ValueError('Agent workspace must be separate from evaluator dataset')
    for prefix in source_roots:
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_/-]*', prefix) or '..' in prefix.split('/'):
            raise ValueError('invalid source root')
    # Resolve the exact base object, never a caller's dirty worktree or fix commit.
    output.mkdir(parents=True, exist_ok=False)
    base = output / 'baseline'
    revision = row['base_commit']
    if source_proof is not None:
        tree = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}']).decode().strip()
        if source_proof['upstream_commit'] != row['base_commit'] or tree != source_proof['tree']:
            raise ValueError('archive source proof mismatch')
        revision = 'HEAD'
    manifest = create_snapshot(repo, base, revision, profile=snapshot_profile)
    if source_proof is not None:
        manifest['normalized_archive_commit'] = manifest['commit']
        manifest['commit'] = row['base_commit']
        manifest['source_proof'] = source_proof
        write(base / 'manifest.json', manifest)
    permitted = set()
    for item in manifest['files']:
        path = item['path']
        if source_roots and not any(path.startswith(p.rstrip('/') + '/') for p in source_roots):
            continue
        try:
            validate_source_path(path, {path})
        except ValueError:
            continue
        permitted.add(path)
    if not permitted:
        raise ValueError('no supported source files')
    # A lifecycle receipt, explicitly not a passing/failing test result.
    reports = base / 'reports'
    reports.mkdir()
    write(reports / 'result.json', {'status': 'issue_only', 'counts': {},
                                   'snapshot_id': snapshot_id(manifest), 'tests_executed': False})
    (reports / 'pytest.xml').write_text('<testsuites/>\n')
    (reports / 'pytest.log').write_text('No benchmark tests executed or disclosed before submission.\n')
    public = {k: row[k] for k in PUBLIC_FIELDS}
    write(output / 'task.json', public)
    receipt = {'schema_version': 1, 'task': public, 'baseline_id': snapshot_id(manifest),
               'base_git_blobs': git_blobs(repo, manifest.get('normalized_archive_commit', manifest['commit'])),
               'allowed_paths': sorted(permitted), 'source_roots': list(source_roots),
               'edit_scope_origin': 'base tree and operator policy; never gold/test patch',
               'agent_features': 'source navigation and exact-reference edits; no benchmark test feedback'}
    write(output / 'workspace.json', receipt)
    return receipt


def prepare_archive_workspace(dataset, instance_id, archive, commit_metadata, output, source_roots=(), *,
                              snapshot_profile='bounded-v1'):
    """Fallback for Git transport failures; verify the whole upstream Git tree."""
    row = task_record(dataset, instance_id)
    metadata = load(commit_metadata)
    if metadata.get('sha') != row['base_commit'] or not re.fullmatch('[a-f0-9]{40}', metadata.get('tree', {}).get('sha', '')):
        raise ValueError('metadata does not identify the pinned upstream commit/tree')
    archive, output = Path(archive).resolve(strict=True), Path(output).resolve()
    if archive.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('archive exceeds 32 MiB')
    repo = output.with_name(output.name + '-verified-source')
    repo.mkdir(parents=True, exist_ok=False)
    max_files, max_bytes = snapshot_limits(snapshot_profile)
    total, seen, prefix, links = 0, set(), None, []
    from pathlib import PurePosixPath
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            p = PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or '\\' in member.name or '.git' in p.parts:
                raise ValueError('unsafe archive path')
            if not p.parts:
                continue
            if prefix is None:
                prefix = p.parts[0]
            if p.parts[0] != prefix:
                raise ValueError('archive must have one root')
            if member.isdir():
                continue
            if len(p.parts) < 2:
                raise ValueError('links/special archive entries are unsupported')
            relative = PurePosixPath(*p.parts[1:]).as_posix()
            allowed_link = (snapshot_profile == 'python-repository-v1' and member.issym()
                            and documentation_link(relative) and not Path(member.linkname).is_absolute())
            if not member.isfile() and not allowed_link:
                raise ValueError('links/special archive entries are unsupported')
            total += member.size
            if relative in seen or len(seen) >= max_files or total > max_bytes:
                raise ValueError('archive exceeds snapshot limits or duplicates paths')
            seen.add(relative)
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if allowed_link:
                links.append((target, member.linkname))
                continue
            with tar.extractfile(member) as stream:
                target.write_bytes(stream.read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    # Retain these Git entries only in the source proof repository. The Agent
    # snapshot omits them. Resolve after all regular files exist, never follow a
    # link while extracting another archive member.
    for target, linkname in links:
        resolved = (target.parent / linkname).resolve(strict=True)
        if repo not in resolved.parents or not resolved.is_file():
            raise ValueError('documentation image link escapes the verified source')
        target.symlink_to(linkname)
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    for args in [('init', '-q'), ('-c', 'core.autocrlf=false', 'add', '--force', '--all'),
                 ('-c', 'user.name=Archive', '-c', 'user.email=archive@localhost',
                  '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                  'commit', '-qm', 'verified source archive')]:
        subprocess.run(['git', '-C', str(repo), *args], env=env, check=True, capture_output=True, timeout=30)
    tree = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}']).decode().strip()
    if tree != metadata['tree']['sha']:
        raise ValueError('archive files/modes do not match upstream Git tree')
    proof = {'upstream_commit': row['base_commit'], 'tree': tree,
             'archive_sha256': sha(archive.read_bytes()), 'metadata_sha256': sha(Path(commit_metadata).read_bytes()),
             'method': 'whole Git tree verified; local commit normalized; no upstream history exposed'}
    return prepare_workspace(dataset, instance_id, repo, output, source_roots, source_proof=proof,
                             snapshot_profile=snapshot_profile)


class SWEbenchSession(RepairSession):
    swebench_enabled = True
    candidate_evaluation_is_docker = False

    def __init__(self, workspace, output, *, swe_policy='on_demand', enable_reproduction=False,
                 reproduction_image=None, max_repro_programs=3, max_repro_runs=6, **kwargs):
        if not isinstance(swe_policy, str) or swe_policy not in {'on_demand', 'issue_evidence'}:
            raise ValueError('SWE policy must be on_demand or issue_evidence')
        self.swe_policy = swe_policy
        if (type(enable_reproduction) is not bool
                or type(max_repro_programs) is not int or not 1 <= max_repro_programs <= 12
                or type(max_repro_runs) is not int or not 1 <= max_repro_runs <= 12):
            raise ValueError('invalid SWE reproduction switch or limits')
        if enable_reproduction:
            if type(reproduction_image) is not str or not re.fullmatch('sha256:[a-f0-9]{64}', reproduction_image):
                raise ValueError('SWE reproduction requires a qualified immutable image')
        elif reproduction_image is not None:
            raise ValueError('reproduction image requires explicit enablement')
        self.enable_reproduction = enable_reproduction
        self.reproduction_image = reproduction_image
        self.max_repro_programs, self.max_repro_runs = max_repro_programs, max_repro_runs
        self.reproduction_workspace = None
        self.reproduction_stop_reason = None
        self.issue_context_stats = None
        self.submitted_number = None
        self.workspace = Path(workspace).resolve(strict=True)
        receipt = load(self.workspace / 'workspace.json')
        self.public_task = receipt['task']
        super().__init__(self.workspace / 'baseline', Path(output), set(receipt['allowed_paths']),
                         profile='simple_context', **kwargs)
        if snapshot_id(self.manifest) != receipt['baseline_id'] or self.manifest['commit'] != self.public_task['base_commit']:
            raise ValueError('workspace/base commit mismatch')

    def _validate_baseline(self, before, outcomes):
        if (before.get('status') != 'issue_only' or before.get('tests_executed') is not False
                or outcomes or before.get('snapshot_id') != snapshot_id(self.manifest)):
            raise ValueError('issue-only baseline required; benchmark tests must remain private')

    def check_budget(self):
        if self.reproduction_workspace is not None and self.reproduction_workspace.blocked_reason:
            self.reproduction_stop_reason = self.reproduction_workspace.blocked_reason
        if self.submitted_number is not None:
            raise BudgetExceeded('candidate_submitted')
        if self.reproduction_stop_reason is not None:
            raise BudgetExceeded(self.reproduction_stop_reason)
        super().check_budget()

    def initial_context(self):
        self.check_budget()
        packet = {'mode': 'issue_only', 'tests_executed': False,
                'repo': self.public_task['repo'], 'base_commit': self.public_task['base_commit'],
                'navigation': 'Use list_sources, search_source and AST tools to locate code. No gold paths or failing-test names are supplied.'}
        if self.swe_policy == 'issue_evidence':
            from .issue_context import issue_evidence
            packet['issue_evidence'] = issue_evidence(self)
        if self.enable_reproduction:
            packet['reproduction'] = {
                'tools': ['propose_reproducer', 'run_reproducer'],
                'max_programs': self.max_repro_programs, 'max_runs': self.max_repro_runs,
                'target': 'immutable public baseline only', 'network': 'none',
                'runtime_profile': 'swebench-testbed-v1',
                'trust': 'model_program_output; not an independent verdict',
                'contract': 'Python code must define run() and return a JSON-serializable observation. '
                            'Only base sources and existing public tests are available; no official acceptance feedback.',
            }
        return packet

    def _check_reproduction(self):
        self.check_budget()
        if self.execution_control is not None:
            self.execution_control.check()

    def _get_reproduction_workspace(self):
        if not self.enable_reproduction:
            raise EditRejected('reproduction_disabled')
        if self.reproduction_workspace is None:
            from .reproduction import ReproductionWorkspace

            class Control:
                def check(control):
                    self._check_reproduction()

            self.reproduction_workspace = ReproductionWorkspace(
                self.baseline, self.output / 'reproduction', self.reproduction_image,
                max_programs=self.max_repro_programs, max_runs=self.max_repro_runs,
                execution_control=Control(), admit=lambda operation: self._check_reproduction(),
                runtime_profile='swebench-testbed-v1')
        return self.reproduction_workspace

    def propose_reproducer(self, code):
        with self.lock:
            self.reserve_tool('propose_reproducer')
            result = self._get_reproduction_workspace().propose_reproducer(code)
            self.record('reproducer_proposed', **result)
            self.save('running')
            return result

    def run_reproducer(self, reproducer_ref, timeout_seconds=30):
        with self.lock:
            self.reserve_tool('run_reproducer')
            if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 0 < timeout_seconds <= 60):
                raise EditRejected('invalid_reproduction_timeout')
            workspace = self._get_reproduction_workspace()
            remaining = self.deadline_seconds - (time.monotonic() - self.started)
            try:
                result = workspace.run_reproducer(reproducer_ref,
                    timeout_seconds=min(timeout_seconds, remaining), max_output_bytes=4096)
            finally:
                if workspace.cleanup_ok is not True:
                    self.reproduction_stop_reason = 'reproduction_cleanup_unconfirmed'
                elif workspace.blocked_reason is not None:
                    self.reproduction_stop_reason = workspace.blocked_reason
            self.record('reproducer_finished', reproducer_ref=reproducer_ref,
                        status=result['status'], cleanup_ok=result.get('cleanup_ok', False),
                        trust='model_program_output')
            if result.get('cleanup_ok') is not True or result.get('input_integrity_ok') is not True:
                self.reproduction_stop_reason = ('reproduction_cleanup_unconfirmed'
                    if result.get('cleanup_ok') is not True else 'reproduction_inputs_changed')
                self.save('stopped', stop_reason=self.reproduction_stop_reason)
                raise BudgetExceeded(self.reproduction_stop_reason)
            self.save('running')
            self.check_budget()
            # Preserve the executor's outer provenance even when the program
            # claims success or forges a nested trust/status field.
            return result

    def list_sources(self, offset=0, limit=100):
        self.reserve_tool('list_sources')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise EditRejected('invalid_page_range')
        paths = sorted(self.files)
        page = paths[offset:offset + limit]
        while len(json.dumps(page, ensure_ascii=False)) > 8000 and len(page) > 1:
            page.pop()
        return {'files': page, 'total': len(paths),
                'allowed_paths': [path for path in page if path in self.allowed_paths],
                'allowed_path_count': len(self.allowed_paths),
                'allowed_paths_scope': 'Current page only; use pagination to inspect the remaining files.',
                'next_offset': offset + len(page) if offset + len(page) < len(paths) else None}

    def read_test_log(self, attempt=0, offset=0):
        self.reserve_tool('read_test_log')
        return {'status': 'unavailable', 'reason': 'official_acceptance_is_evaluator_only',
                'next_action': 'Diagnose from the issue and public base sources; submit one final patch.'}

    def _verify_candidate(self, candidate, destination, timeout_seconds):
        # Rebuild independently, without running or exposing acceptance tests.
        proposed = verify_snapshot(candidate)
        base = {i['path']: i['sha256'] for i in self.manifest['files']}
        changes = {i['path']: (candidate / 'workspace' / i['path']).read_text()
                   for i in proposed['files'] if i['sha256'] != base[i['path']]}
        create_candidate(self.baseline, destination, changes, set(self.allowed_paths))
        self.submitted_number = len(self.attempts)
        write(destination / 'submission.json', {'status': 'submitted',
            'reason': 'awaiting_official_evaluation', 'instance_id': self.public_task['instance_id']})
        return {'status': 'submitted', 'reason': 'awaiting_official_evaluation', 'phase': 'submission',
                'scope': 'syntax/scope checked and independently reconstructed; not a benchmark pass'}

    def save(self, state, **extra):
        extra.update(benchmark='swebench', evaluation_status='not_run',
                     selected_attempt=self.submitted_number, swe_policy=self.swe_policy,
                     issue_context_stats=self.issue_context_stats)
        if self.submitted_number is not None:
            extra['patch'] = 'attempt-%02d/verification/patch.diff' % self.submitted_number
        if self.enable_reproduction:
            extra['swe_reproduction'] = {
                'enabled': True, 'runtime_profile': 'swebench-testbed-v1',
                'image_id': self.reproduction_image, 'max_programs': self.max_repro_programs,
                'max_runs': self.max_repro_runs, 'stop_reason': self.reproduction_stop_reason,
                'workspace': 'reproduction/workspace.json' if self.reproduction_workspace is not None else None,
                'cleanup_ok': (self.reproduction_workspace is None
                               or self.reproduction_workspace.cleanup_ok is True),
                'trust': 'model_program_output; not official acceptance',
            }
        return super().save(state, **extra)


def validate_qualification(workspace, qualification):
    """Validate both environment controls before any Agent or model is created."""
    workspace, qualification = Path(workspace), Path(qualification)
    task = load(workspace / 'task.json')
    q = load(qualification / 'qualification.json')
    if (q.get('qualified') is not True or q.get('negative', {}).get('status') != 'unresolved'
            or q.get('gold', {}).get('status') != 'resolved'):
        raise ValueError('environment qualification failed; do not start inference')
    images, policies = set(), []
    for role in ('negative', 'gold'):
        result = q[role]
        binding = load(qualification / role / 'source-binding.json')
        if (result.get('instance_id') != task['instance_id'] or result.get('cleanup_ok') is not True
                or binding.get('upstream_commit') != task['base_commit']
                or binding.get('content_match') is not True or binding.get('dirty_returncode') != 0):
            raise ValueError('qualification evidence does not match this clean task')
        images.add(result['image_id'])
        policies.append(result.get('execution_policy'))
    if len(images) != 1 or policies[0] is None or policies[0] != policies[1]:
        raise ValueError('qualification environments differ')
    if policies[0].get('network') == REQUESTS_NETWORK_POLICY:
        if policies[0] != execution_policy(REQUESTS_NETWORK_POLICY, task['instance_id']):
            raise ValueError('qualification network adaptation differs from its frozen policy')
    return {'task': {key: task[key] for key in PUBLIC_FIELDS},
            'image_id': next(iter(images)), 'execution_policy': policies[0],
            'qualification_sha256': sha((qualification / 'qualification.json').read_bytes())}


def run_qualified_agent(workspace, output, model, qualification, *, enable_reproduction=False,
                        max_repro_programs=3, max_repro_runs=6, **limits):
    """Application entry: failed/mismatched environment never reaches the model."""
    checked = validate_qualification(workspace, qualification)
    if 'reproduction_image' in limits:
        raise ValueError('reproduction image is owned by environment qualification')
    from .agent import run_agent
    session = SWEbenchSession(workspace, output, enable_reproduction=enable_reproduction,
        reproduction_image=checked['image_id'] if enable_reproduction else None,
        max_repro_programs=max_repro_programs, max_repro_runs=max_repro_runs, **limits)
    session.record('swebench_qualification_checked',
                   qualification_sha256=checked['qualification_sha256'],
                   image_id=checked['image_id'], execution_policy=checked['execution_policy'])
    return run_agent(session, model, checked['task']['problem_statement'])


def export_prediction(workspace, agent_output, destination, model_name='workbench-swe'):
    workspace, agent_output = Path(workspace), Path(agent_output)
    receipt = load(workspace / 'workspace.json')
    result = load(agent_output / 'result.json')
    number = result.get('selected_attempt')
    if type(number) is not int or not 1 <= number <= 5 or result['status'] != 'submitted':
        raise ValueError('no final submitted candidate')
    candidate = agent_output / ('attempt-%02d' % number) / 'verification'
    manifest = verify_snapshot(candidate)
    base = verify_snapshot(workspace / 'baseline')
    if (result['baseline_id'] != snapshot_id(base) or manifest['base_snapshot_id'] != snapshot_id(base)
            or result['allowed_paths'] != receipt['allowed_paths']):
        raise ValueError('submission/workspace mismatch')
    old = {f['path']: f['sha256'] for f in base['files']}
    new = {f['path']: f['sha256'] for f in manifest['files']}
    if old.keys() != new.keys():
        raise ValueError('file set changed')
    changed = {p for p in old if old[p] != new[p]}
    if not changed or not changed <= set(receipt['allowed_paths']):
        raise ValueError('unauthorized changes')
    # Recompute the patch, rather than trusting a model-edited patch artifact.
    from .candidate import make_patch
    patch = make_patch(workspace / 'baseline/workspace',
                       {p: (candidate / 'workspace' / p).read_text() for p in changed}, agent_output).decode()
    prediction = {'instance_id': receipt['task']['instance_id'],
                  'model_name_or_path': model_name, 'model_patch': patch}
    if not re.fullmatch('[A-Za-z0-9_.-]{1,64}', model_name):
        raise ValueError('safe model label required')
    with Path(destination).open('x') as f:
        f.write(json.dumps(prediction, ensure_ascii=False) + '\n')
    return prediction


def image_identity(image):
    r = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                       capture_output=True, text=True, timeout=15)
    if r.returncode:
        raise ValueError('official image not installed locally; adapter never pulls images')
    if not re.fullmatch('sha256:[a-f0-9]{64}', r.stdout.strip()):
        raise ValueError('invalid immutable image')
    return r.stdout.strip()


def isolated_env():
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'DOCKER_HOST',
                                     'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH') if k in os.environ}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               HF_DATASETS_OFFLINE='1', LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false')
    if 'DOCKER_HOST' not in env:
        r = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                           capture_output=True, text=True, check=True, timeout=15)
        env['DOCKER_HOST'] = r.stdout.strip()
    return env


def validate_candidate_patch(text):
    """External predictions follow the same existing-Python-source policy."""
    if any(marker in text for marker in ('GIT binary patch', 'new file mode ', 'deleted file mode ',
                                        'rename from ', 'rename to ', 'old mode ', 'new mode ', '/dev/null')):
        raise ValueError('unsupported file operation in candidate patch')
    headers = [line for line in text.splitlines() if line.startswith('diff --git ')]
    if not headers:
        raise ValueError('Git source patch required')
    declared = set()
    for line in headers:
        match = re.fullmatch(r'diff --git a/(.+?) b/(.+)', line)
        if not match or match[1] != match[2]:
            raise ValueError('unsupported diff path or rename')
        validate_source_path(match[1], {match[1]})
        declared.add(match[1])
    parsed = subprocess.run(['git', 'apply', '--numstat', '-'], input=text,
        capture_output=True, text=True, timeout=10,
        env={**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
             'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})
    if parsed.returncode != 0:
        raise ValueError('unusable Git patch')
    actual = set()
    for line in parsed.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) != 3 or not all(n.isdigit() for n in fields[:2]):
            raise ValueError('unsupported patch stat')
        validate_source_path(fields[2], {fields[2]})
        actual.add(fields[2])
    if actual != declared:
        raise ValueError('patch headers disagree with Git target paths')
    return actual


def start_worker(command, **kwargs):
    return subprocess.Popen(command, **kwargs)


def wait_worker(process, timeout, execution_control=None):
    if execution_control is None:
        return process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        execution_control.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('official harness', timeout)
        try:
            result = process.wait(timeout=min(0.25, remaining))
        except subprocess.TimeoutExpired:
            continue
        execution_control.check()
        return result


def cleanup_run_network(plan):
    """Remove only this run's labelled network and verify it is absent."""
    receipt = {'name': plan['name'], 'cleanup_ok': False, 'owner_verified': False}
    command = ['docker', 'network', 'inspect', plan['name']]
    try:
        inspect = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if inspect.returncode == 0:
            records = json.loads(inspect.stdout)
            if (not isinstance(records, list) or len(records) != 1
                    or records[0].get('Name') != plan['name']
                    or records[0].get('Labels') != plan['labels']
                    or not re.fullmatch('[a-f0-9]{64}', records[0].get('Id', ''))):
                return {**receipt, 'reason': 'network_owner_mismatch'}
            receipt['owner_verified'] = True
            receipt['network_id'] = records[0]['Id']
            removal = subprocess.run(['docker', 'network', 'rm', records[0]['Id']],
                                     capture_output=True, text=True, timeout=15)
            receipt['remove_returncode'] = removal.returncode
            inspect = subprocess.run(command, capture_output=True, text=True, timeout=15)
        error = inspect.stderr.lower()
        receipt['cleanup_ok'] = inspect.returncode != 0 and (
            'no such network' in error or f'network {plan["name"]} not found' in error)
        receipt['reason'] = 'network_absent' if receipt['cleanup_ok'] else 'network_cleanup_unconfirmed'
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
        receipt['reason'] = 'network_cleanup_unconfirmed'
        receipt['error_type'] = type(exc).__name__
    return receipt


def evaluate(row, prediction, output, harness_python, *, workspace=None, timeout=600, negative=False, gold_control=False, network_policy='none', execution_control=None):
    """Official run_instance/get_eval_report in a separate dependency environment."""
    validate_record(row)
    policy = execution_policy(network_policy, row['instance_id'])
    if not 1 <= timeout <= 3600:
        raise ValueError('test timeout must be 1–3600 seconds')
    if (prediction.get('instance_id') != row['instance_id'] or
            not re.fullmatch('[A-Za-z0-9_.-]{1,64}', prediction.get('model_name_or_path', ''))
            or not isinstance(prediction.get('model_patch'), str)
            or len(prediction['model_patch'].encode()) > 4 * 1024 * 1024
            or (not negative and not prediction['model_patch'].strip())):
        raise ValueError('invalid or empty prediction')
    candidate_paths = (validate_candidate_patch(prediction['model_patch'])
                       if not negative and not gold_control else None)
    if workspace is None:
        raise ValueError('verified base workspace is required for image/source binding')
    receipt = load(Path(workspace) / 'workspace.json')
    if candidate_paths is not None and not candidate_paths <= set(receipt['allowed_paths']):
        raise ValueError('prediction exceeds operator-approved source scope')
    baseline = verify_snapshot(Path(workspace) / 'baseline')
    if (receipt['task'] != {k: row[k] for k in PUBLIC_FIELDS}
            or receipt['baseline_id'] != snapshot_id(baseline)):
        raise ValueError('evaluation workspace does not match task')
    expected_blobs = git_blobs(Path(baseline['source_repo']),
        baseline.get('normalized_archive_commit', baseline['commit']))
    if receipt['base_git_blobs'] != expected_blobs:
        raise ValueError('workspace source binding changed')
    image = image_identity(row['image'])  # Preflight before creating any run artifacts.
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    run_id = 'repair-' + uuid4().hex
    plan = network_plan(network_policy, row['instance_id'], run_id)
    name = 'sweb.eval.%s.%s' % (row['instance_id'].lower(), run_id)
    payload = {'record': row, 'prediction': prediction, 'image_id': image,
               'base_git_blobs': expected_blobs,
               'network_policy': network_policy,
               'run_id': run_id, 'timeout': timeout, 'negative': negative}
    write(output / 'evaluator-input.json', payload)
    (output / 'evaluator-input.json').chmod(0o600)
    # Resolving a venv's Python symlink loses its pyvenv.cfg and dependencies.
    command = [str(Path(harness_python).absolute()), '-m', 'repair_workbench.swebench_worker',
               str(output / 'evaluator-input.json')]
    started = time.monotonic()
    result = {'status': 'inconclusive', 'reason': 'official_report_missing', 'run_id': run_id,
              'instance_id': row['instance_id'], 'image_id': image, 'cleanup_ok': False,
              'negative_control': negative, 'harness_commit': HARNESS_COMMIT,
              'execution_policy': policy}
    process = None
    try:
        if execution_control is not None:
            execution_control.check()
        with (output / 'harness.log').open('wb') as log:
            process = start_worker(command, cwd=output, env=isolated_env(), start_new_session=True,
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_worker(process, timeout + 60, execution_control)
            except subprocess.TimeoutExpired:
                result['reason'] = 'harness_wall_timeout'
            response = output / 'worker-result.json'
            if response.is_file():
                result.update(load(response))
    except KeyboardInterrupt:
        result['reason'] = 'cancel_requested'
        raise
    except BudgetExceeded as exc:
        result['reason'] = str(exc)
        raise
    except Exception as exc:
        result.update(reason='harness_execution_error', error_type=type(exc).__name__)
        raise
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        try:
            inspect = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
            if inspect.returncode == 0:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
                inspect = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=15)
            result['cleanup_ok'] = inspect.returncode != 0 and 'no such' in inspect.stderr.lower()
        except (OSError, subprocess.SubprocessError):
            result['cleanup_ok'] = False
        result['container_cleanup_ok'] = result['cleanup_ok']
        if not result['container_cleanup_ok']:
            result.update(status='inconclusive', reason='container_cleanup_unconfirmed')
        if plan is not None:
            network_cleanup = (cleanup_run_network(plan) if result['container_cleanup_ok'] else
                {'name': plan['name'], 'cleanup_ok': False, 'owner_verified': False,
                 'reason': 'container_cleanup_unconfirmed'})
            write(output / 'network-cleanup.json', network_cleanup)
            result['network_cleanup_ok'] = network_cleanup['cleanup_ok']
            result['cleanup_ok'] = result['container_cleanup_ok'] and result['network_cleanup_ok']
            if result['container_cleanup_ok'] and not result['network_cleanup_ok']:
                result.update(status='inconclusive', reason='network_cleanup_unconfirmed')
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        write(output / 'result.json', result)
    return result


def qualify(row, output, harness_python, timeout=600, *, workspace=None, network_policy='none'):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    negative = evaluate(row, {'instance_id': row['instance_id'], 'model_name_or_path': 'negative-control',
        'model_patch': ''}, output / 'negative', harness_python, workspace=workspace, timeout=timeout, negative=True, network_policy=network_policy)
    gold = evaluate(row, {'instance_id': row['instance_id'], 'model_name_or_path': 'gold-control',
        'model_patch': row['patch']}, output / 'gold', harness_python, workspace=workspace, timeout=timeout, gold_control=True, network_policy=network_policy)
    result = {'qualified': negative['status'] == 'unresolved' and gold['status'] == 'resolved'
              and negative['cleanup_ok'] and gold['cleanup_ok'], 'negative': negative, 'gold': gold,
              'scope': 'environment/control qualification, not Agent performance'}
    result['reason'] = ('qualified' if result['qualified'] else
        'container_cleanup_unconfirmed' if not negative['cleanup_ok'] or not gold['cleanup_ok'] else
        'negative_control_not_unresolved' if negative['status'] != 'unresolved' else
        'gold_control_not_resolved')
    write(output / 'qualification.json', result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import')
    imp.add_argument('--input', type=Path, required=True)
    imp.add_argument('--output', type=Path, required=True)
    imp.add_argument('--dataset-name', required=True)
    imp.add_argument('--revision', required=True)
    imp.add_argument('--split', default='test')
    prep = sub.add_parser('prepare')
    prep.add_argument('--dataset', type=Path, required=True)
    prep.add_argument('--instance', required=True)
    prep.add_argument('--repo', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--source-root', action='append', default=[])
    prep.add_argument('--snapshot-profile', choices=['bounded-v1', 'python-repository-v1'], default='bounded-v1')
    arc = sub.add_parser('prepare-archive')
    arc.add_argument('--dataset', type=Path, required=True)
    arc.add_argument('--instance', required=True)
    arc.add_argument('--archive', type=Path, required=True)
    arc.add_argument('--commit-metadata', type=Path, required=True)
    arc.add_argument('--output', type=Path, required=True)
    arc.add_argument('--source-root', action='append', default=[])
    arc.add_argument('--snapshot-profile', choices=['bounded-v1', 'python-repository-v1'], default='bounded-v1')
    exp = sub.add_parser('export')
    exp.add_argument('--workspace', type=Path, required=True)
    exp.add_argument('--agent-output', type=Path, required=True)
    exp.add_argument('--output', type=Path, required=True)
    exp.add_argument('--model-label', default='workbench-swe')
    for name in ('evaluate', 'qualify'):
        ev = sub.add_parser(name)
        ev.add_argument('--dataset', type=Path, required=True)
        ev.add_argument('--instance', required=True)
        ev.add_argument('--output', type=Path, required=True)
        ev.add_argument('--harness-python', type=Path, required=True)
        ev.add_argument('--workspace', type=Path, required=True)
        ev.add_argument('--network-policy', choices=NETWORK_POLICIES, default='none')
        ev.add_argument('--timeout', type=int, default=600)
        if name == 'evaluate':
            ev.add_argument('--predictions', type=Path, required=True)
    args = p.parse_args(argv)
    if args.command == 'import':
        result = import_dataset(args.input, args.output, dataset_name=args.dataset_name,
                                revision=args.revision, split=args.split)
    elif args.command == 'prepare':
        result = prepare_workspace(args.dataset, args.instance, args.repo, args.output, args.source_root,
                                   snapshot_profile=args.snapshot_profile)
    elif args.command == 'prepare-archive':
        result = prepare_archive_workspace(args.dataset, args.instance, args.archive,
            args.commit_metadata, args.output, args.source_root, snapshot_profile=args.snapshot_profile)
    elif args.command == 'export':
        result = export_prediction(args.workspace, args.agent_output, args.output, args.model_label)
        result = {k: v for k, v in result.items() if k != 'model_patch'}
    else:
        row = task_record(args.dataset, args.instance)
        if args.command == 'qualify':
            result = qualify(row, args.output, args.harness_python, args.timeout, workspace=args.workspace, network_policy=args.network_policy)
        else:
            predictions = [json.loads(s) for s in args.predictions.read_text().splitlines() if s.strip()]
            if len(predictions) != 1:
                raise ValueError('one prediction per isolated evaluation required')
            result = evaluate(row, predictions[0], args.output, args.harness_python, workspace=args.workspace, timeout=args.timeout, network_policy=args.network_policy)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
