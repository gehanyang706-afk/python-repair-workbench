"""Frozen, portable diagnostic evidence and bounded report queries; no execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from .diagnostics import _validated_report, get_workload
from .tool_runtime import digest


MAX_BUNDLE_BYTES = 2 * 1024 * 1024
REPORT_ID = re.compile(r'r-[a-f0-9]{32}\Z')


def visible_evidence(evidence):
    """Keep full observations on disk, return only three CPU hotspots per scale."""
    visible = json.loads(json.dumps(evidence, ensure_ascii=False))
    if visible.get('kind') == 'diagnostic_probe':
        omitted = 0
        for row in visible['observation'].get('summary', {}).get('scales', {}).values():
            if 'top_functions' in row:
                omitted += max(0, len(row['top_functions']) - 3)
                row['top_functions'] = row['top_functions'][:3]
        if omitted:
            visible['detail_navigation'] = {'tool': 'read_probe_report', 'omitted_hotspots': omitted,
                                            'hash_scope': 'Original evidence artifact, not this bounded view.'}
    return visible


def read_json(path, limit=256 * 1024):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('report_symlinks_forbidden')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('report_unavailable_or_oversized')
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError('report_unavailable_or_oversized')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate_report_field')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('nonfinite_report_number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant), hashlib.sha256(raw).hexdigest()


def freeze_probe(root, evidence):
    """Validate one recorded probe without executing/importing repository code."""
    if (evidence.get('kind') != 'diagnostic_probe'
            or not re.fullmatch(r'E\d{4}', str(evidence.get('evidence_ref')))):
        raise ValueError('not_a_diagnostic_probe_reference')
    if evidence.get('sha256') != hashlib.sha256(json.dumps(
            {k: v for k, v in evidence.items() if k != 'sha256'},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest():
        raise ValueError('evidence_digest_mismatch')
    observation = evidence['observation']
    relative = observation.get('report')
    if not isinstance(relative, str) or not re.fullmatch(r'diagnostics/probe-\d{2}', relative):
        raise ValueError('unregistered_probe_directory')
    directory = Path(root).absolute() / relative
    result, result_hash = read_json(directory / 'result.json')
    # New evidence binds the complete result, including samples outside summary.
    if result_hash != observation.get('report_sha256'):
        raise ValueError('unbound_or_changed_probe_report')
    if (result.get('snapshot_id') != evidence.get('snapshot_id')
            or not re.fullmatch(r'[a-f0-9]{64}', str(result.get('snapshot_id')))
            or result.get('status') != 'ok' or result.get('cleanup_ok') is not True
            or any(observation.get(k) != result.get(k) for k in ('probe', 'summary', 'status', 'cleanup_ok'))):
        raise ValueError('probe_identity_or_status_mismatch')
    read_json(directory / 'probe.json', 128 * 1024)  # Reject symlinks before the shared validator.
    validated = _validated_report(directory / 'probe.json', result['probe'], get_workload(result['workload_id']))
    if any(result.get(k) != v for k, v in validated.items()):
        raise ValueError('probe_measurement_mismatch')
    from . import probe_worker
    if (result.get('harness_sha256') != hashlib.sha256(Path(probe_worker.__file__).read_bytes()).hexdigest()
            or result.get('workload_fingerprint') != get_workload(result['workload_id']).fingerprint
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(result.get('image_id')))):
        raise ValueError('probe_execution_identity_mismatch')
    # Export measured data and provenance only: no host paths, commands, logs,
    # source excerpts, model messages, test answers or environment credentials.
    record = {k: result[k] for k in ('snapshot_id', 'image_id', 'workload_id',
                                    'workload_fingerprint', 'harness_sha256', 'probe')}
    record.update(validated, evidence_ref=evidence['evidence_ref'], report_sha256=result_hash,
                  trust='tool_observation_not_independent_acceptance')
    record['report_id'] = 'r-' + digest(record)[:32]
    return record


def write_bundle(records, destination):
    if not 1 <= len(records) <= 12:
        raise ValueError('report_bundle_requires_1_to_12_probes')
    bundle = {'schema_version': 1, 'reports': sorted(records, key=lambda r: r['report_id'])}
    raw = (json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError('report_bundle_too_large')
    destination = Path(destination).absolute()
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('report_symlinks_forbidden')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


class ReportStore:
    """One immutable in-memory catalog, identified by a trusted bundle digest."""
    def __init__(self, path, expected_sha256):
        bundle, observed = read_json(path, MAX_BUNDLE_BYTES)
        if observed != expected_sha256:
            raise ValueError('report_bundle_digest_mismatch')
        if (not isinstance(bundle, dict) or set(bundle) != {'schema_version', 'reports'}
                or type(bundle['schema_version']) is not int or bundle['schema_version'] != 1
                or not isinstance(bundle['reports'], list) or not 1 <= len(bundle['reports']) <= 12):
            raise ValueError('invalid_report_bundle')
        self.sha256 = observed
        self.reports = {}
        for record in bundle['reports']:
            identity = record.get('report_id') if isinstance(record, dict) else None
            if (not isinstance(identity, str) or not REPORT_ID.fullmatch(identity)
                    or identity in self.reports
                    or identity != 'r-' + digest({k: v for k, v in record.items() if k != 'report_id'})[:32]):
                raise ValueError('invalid_or_duplicate_report_identity')
            self.reports[identity] = record

    def catalog(self):
        return {'bundle_sha256': self.sha256, 'read_only': True, 'reports': [
            {k: record[k] for k in ('report_id', 'evidence_ref', 'snapshot_id', 'probe')}
            | {'uri': f'diagnostic://reports/{identity}'}
            for identity, record in self.reports.items()]}

    def query(self, report_id, section='summary', scale='large', offset=0, limit=5):
        if not isinstance(report_id, str) or report_id not in self.reports:
            raise ValueError('unknown_report_id')
        if (section not in {'summary', 'hotspots', 'samples', 'environment'}
                or scale not in {'small', 'large'} or type(offset) is not int or not 0 <= offset <= 100
                or type(limit) is not int or not 1 <= limit <= 10):
            raise ValueError('invalid_report_query')
        record = self.reports[report_id]
        row = record['summary']['scales'][scale]
        if section == 'hotspots':
            if record['probe'] != 'cpu':
                raise ValueError('hotspots_require_cpu_probe')
            items = row['top_functions']
        elif section == 'samples':
            if record['probe'] == 'cpu':
                raise ValueError('cpu_probe_has_no_timing_samples')
            items = record['samples'][scale]
        else:
            items = [record['environment'] if section == 'environment' else
                     {k: v for k, v in row.items() if k != 'top_functions'}]
        page = items[offset:offset + limit]
        result = {k: record[k] for k in ('report_id', 'evidence_ref', 'snapshot_id', 'probe', 'trust')}
        result.update(bundle_sha256=self.sha256, section=section, scale=scale,
                      offset=offset, total=len(items), data=page,
                      next_offset=offset + len(page) if offset + len(page) < len(items) else None)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 16 * 1024:
            raise ValueError('report_page_too_large')
        return json.loads(json.dumps(result, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Export bound diagnostic measurements for read-only MCP review.')
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.run.absolute()
    records = []
    paths = sorted((root / 'evidence').glob('E[0-9][0-9][0-9][0-9].json'))
    if len(paths) > 512:
        raise ValueError('too_many_evidence_records')
    for path in paths:
        item, _ = read_json(path)
        if item.get('kind') == 'diagnostic_probe' and item.get('observation', {}).get('status') == 'ok':
            records.append(freeze_probe(root, item))
    sha = write_bundle(records, args.output)
    print(json.dumps({'bundle': str(args.output.absolute()), 'sha256': sha, 'reports': len(records),
                      'real_model_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
