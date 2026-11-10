"""Frozen operator-owned workload data, not an executable evaluator.

The application supplies reviewed adapter/checker registries out of band. A
model proposal must never be promoted to this configuration. Hashes bind data
and implementations; they do not authenticate an operator, validate a business
oracle, or establish that the existing probe runner supports this contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_FIELDS = {'schema_version', 'task_id', 'repo', 'base_commit', 'workload_id',
           'workload_version', 'adapter_sha256', 'goal', 'thresholds', 'measurement',
           'semantics', 'original_tests', 'development_inputs', 'final_inputs'}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _fields(value, expected, name):
    _require(type(value) is dict and set(value) == set(expected), f'invalid {name} fields')


def _identifier(value, name):
    _require(type(value) is str and _ID.fullmatch(value) is not None, f'invalid {name}')


def _sha(value, name):
    _require(type(value) is str and _SHA.fullmatch(value) is not None, f'invalid {name}')


def _number(value, low, high, name, *, low_open=False):
    _require(type(value) in (int, float)
             and (low < value if low_open else low <= value) and value <= high and math.isfinite(value),
             f'invalid {name}')


def _json_data(value, depth=0, counter=None):
    counter = [0] if counter is None else counter
    counter[0] += 1
    _require(depth <= 16 and counter[0] <= 50000, 'JSON data exceeds structural limits')
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        _require(math.isfinite(value), 'nonfinite JSON value')
        return
    if type(value) is dict:
        _require(all(type(key) is str for key in value), 'JSON keys must be strings')
        for item in value.values():
            _json_data(item, depth + 1, counter)
        return
    if type(value) is list:
        for item in value:
            _json_data(item, depth + 1, counter)
        return
    raise ValueError('only JSON data is allowed; no executable objects')


def canonical_json(value):
    _json_data(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        _require(len(encoded.encode('utf-8')) <= MAX_DOCUMENT_BYTES, 'document too large')
        return encoded
    except (UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError('invalid JSON data') from exc


def input_sha256(value):
    """Identity of an input's canonical JSON, excluding its name/scoring role."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _load_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'duplicate JSON field')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite JSON value')
    _require(type(text) is str and len(text.encode()) <= MAX_DOCUMENT_BYTES, 'document too large')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('invalid JSON document') from exc


def _registered(identity, digest, registry, name):
    _identifier(identity, name + ' id')
    _sha(digest, name + ' hash')
    _require(type(registry) is dict and registry.get(identity) == digest,
             name + ' is not in the application trusted registry')


def _input_set(cases, name):
    _require(type(cases) is list and 1 <= len(cases) <= 128, f'invalid {name} count')
    ids, hashes, roles = set(), set(), set()
    for case in cases:
        _fields(case, {'case_id', 'role', 'input', 'sha256'}, name)
        _identifier(case['case_id'], 'case id')
        _sha(case['sha256'], 'input hash')
        _require(case['role'] in ('target', 'guardrail'), 'invalid input scoring role')
        _require(len(canonical_json(case['input']).encode()) <= 65536, 'input too large')
        _require(case['sha256'] == input_sha256(case['input']), 'input hash mismatch')
        _require(case['case_id'] not in ids and case['sha256'] not in hashes, 'duplicate input identity')
        ids.add(case['case_id']); hashes.add(case['sha256']); roles.add(case['role'])
    _require('target' in roles, f'{name} requires a target input')
    return ids, hashes


def _test_ids(values, name, *, empty=False):
    _require(type(values) is list and (empty or bool(values)) and len(values) <= 20000,
             f'invalid {name}')
    seen = set()
    for value in values:
        _require(type(value) is str and 1 <= len(value) <= 1024 and value not in seen
                 and not any(c in value for c in ('\n', '\r', '\x00')), f'invalid {name}')
        filename, separator, identity = value.partition('::')
        path = PurePosixPath(filename)
        qualified, parameter, suffix = identity.partition('[')
        # Parameter IDs are exact literal keys and can contain backslashes,
        # SQL wildcard characters or '::'. They never grant path permissions.
        _require(bool(separator) and not path.is_absolute() and '..' not in path.parts
                 and not any(c in filename for c in ('\\', '*', '?'))
                 and path.as_posix() == filename and filename.endswith('.py')
                 and re.fullmatch(r'[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*', qualified)
                 and (not parameter or identity.endswith(']')),
                 f'{name} requires exact pytest node ids')
        seen.add(value)
    return seen


def _validate(data, adapters, checkers):
    _fields(data, _FIELDS, 'WorkloadSpec')
    canonical_json(data)
    _require(type(data['schema_version']) is int and data['schema_version'] == SCHEMA_VERSION,
             'unsupported workload schema version')
    for name in ('task_id', 'workload_version'):
        _identifier(data[name], name)
    _require(type(data['repo']) is str and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', data['repo']),
             'invalid repository')
    _require(type(data['base_commit']) is str and re.fullmatch(r'[a-f0-9]{40}', data['base_commit']),
             'base commit must be immutable')
    _registered(data['workload_id'], data['adapter_sha256'], adapters, 'workload adapter')
    _require(data['goal'] in ('speed', 'memory'), 'goal must be speed or memory')
    thresholds = data['thresholds']
    _fields(thresholds, {'min_speedup', 'min_memory_reduction', 'max_time_ratio',
                         'max_memory_ratio', 'max_relative_noise'}, 'thresholds')
    for key in ('min_speedup', 'min_memory_reduction'):
        _number(thresholds[key], 0, 0.99, key)
    primary, other = (('min_speedup', 'min_memory_reduction') if data['goal'] == 'speed'
                      else ('min_memory_reduction', 'min_speedup'))
    _require(thresholds[primary] > 0 and thresholds[other] == 0, 'thresholds must match the declared primary goal')
    for key in ('max_time_ratio', 'max_memory_ratio'):
        _number(thresholds[key], 1, 4, key)
    _number(thresholds['max_relative_noise'], 0, 1, 'max_relative_noise', low_open=True)

    measurement = data['measurement']
    required_measurement = {'order', 'warmups', 'repeats', 'timeout_seconds', 'image_id',
                            'cpus', 'memory_limit_bytes', 'time_metric', 'memory_metric'}
    _require(type(measurement) is dict and required_measurement <= set(measurement)
             <= required_measurement | {'batch_iterations', 'gc_policy', 'warmup_seconds', 'cpu_affinity',
                                         'batch_iterations_by_case'}, 'invalid measurement fields')
    _require(type(measurement.get('batch_iterations', 1)) is int
             and 1 <= measurement.get('batch_iterations', 1) <= 100,
             'invalid timing batch iterations')
    _require(measurement.get('gc_policy', 'unmodified') in ('unmodified', 'collect_before_sample'),
             'invalid GC policy')
    _number(measurement.get('warmup_seconds', 0), 0, 5, 'minimum warmup duration')
    affinity = measurement.get('cpu_affinity')
    _require(affinity is None or type(affinity) is str and re.fullmatch(r'[0-9]{1,2}', affinity),
             'invalid single CPU affinity')
    _require(measurement['order'] == ['baseline', 'candidate', 'candidate', 'baseline'], 'ABBA order required')
    for key, low, high in (('warmups', 1, 10), ('repeats', 3, 100), ('memory_limit_bytes', 64 * 1024**2, 64 * 1024**3)):
        _require(type(measurement[key]) is int and low <= measurement[key] <= high, f'invalid {key}')
    _number(measurement['timeout_seconds'], 0, 3600, 'timeout_seconds', low_open=True)
    _number(measurement['cpus'], 0, 32, 'cpus', low_open=True)
    _require(type(measurement['image_id']) is str and re.fullmatch(r'sha256:[a-f0-9]{64}', measurement['image_id']),
             'immutable image id required')
    _require(measurement['time_metric'] == 'unprofiled_wall_seconds', 'explicit unprofiled timing required')
    _require(measurement['memory_metric'] in ('python_traced_peak_bytes', 'process_peak_rss_bytes'),
             'explicit supported memory metric required')

    semantics = data['semantics']
    _fields(semantics, {'strategy', 'baseline_behavior_confirmed', 'output_comparison', 'contract_reference',
                        'checker_id', 'checker_sha256', 'reference_sha256', 'metamorphic_checks'}, 'semantics')
    _registered(semantics['checker_id'], semantics['checker_sha256'], checkers, 'semantic checker')
    _require(semantics['strategy'] in ('baseline_equivalence', 'trusted_reference'), 'unknown semantic strategy')
    _require(type(semantics['baseline_behavior_confirmed']) is bool, 'baseline confirmation must be explicit')
    _require(semantics['output_comparison'] in ('exact_bytes', 'canonical_json'), 'unknown output comparison')
    reference = semantics['contract_reference']
    _require(type(reference) is str and 1 <= len(reference) <= 512 and not any(c in reference for c in '\n\r\x00'),
             'explicit business contract reference required')
    if semantics['strategy'] == 'baseline_equivalence':
        _require(semantics['baseline_behavior_confirmed'] is True and semantics['reference_sha256'] is None,
                 'baseline equivalence needs confirmed behavior, not assumed correctness')
    else:
        _sha(semantics['reference_sha256'], 'trusted reference hash')
    relations = semantics['metamorphic_checks']
    _require(type(relations) is list and len(relations) <= 16, 'invalid metamorphic checks')
    seen = set()
    for relation in relations:
        _fields(relation, {'checker_id', 'checker_sha256', 'relation', 'version'}, 'metamorphic check')
        _registered(relation['checker_id'], relation['checker_sha256'], checkers, 'metamorphic checker')
        _identifier(relation['version'], 'metamorphic version')
        _require(relation['relation'] in ('repeat_determinism', 'idempotence', 'partition_concat', 'prefix_consistency'),
                 'unknown metamorphic relation')
        identity = relation['checker_id']
        _require(identity not in seen, 'duplicate metamorphic checker')
        seen.add(identity)

    tests = data['original_tests']
    _require(type(tests) is dict and set(tests) in (
        {'suite_sha256', 'nodeids', 'expected_xfail'},
        {'suite_sha256', 'nodeids', 'expected_xfail', 'expected_xpass'}), 'invalid original_tests fields')
    _sha(tests['suite_sha256'], 'original suite hash')
    nodeids = _test_ids(tests['nodeids'], 'original test list')
    xfails = _test_ids(tests['expected_xfail'], 'expected xfail list', empty=True)
    _require(xfails <= nodeids, 'expected xfail must name an original test')
    xpasses = _test_ids(tests.get('expected_xpass', []), 'expected xpass list', empty=True)
    _require(xpasses <= nodeids and not xpasses & xfails,
             'expected xpass must name a separate original test')
    development = _input_set(data['development_inputs'], 'development inputs')
    final = _input_set(data['final_inputs'], 'final inputs')
    _require(not development[0] & final[0] and not development[1] & final[1],
             'exploration and final input identities overlap')
    iterations = measurement.get('batch_iterations_by_case', {})
    _require(type(iterations) is dict and set(iterations) <= final[0]
             and all(type(v) is int and 1 <= v <= 10000 for v in iterations.values()),
             'invalid final-case timing iterations')


@dataclass(frozen=True, init=False)
class WorkloadSpec:
    """An immutable canonical contract; registries come from trusted application code."""
    _canonical: str

    def __init__(self, data, *, adapters, checkers):
        _validate(data, adapters, checkers)
        object.__setattr__(self, '_canonical', canonical_json(data))

    @property
    def sha256(self):
        return hashlib.sha256(self._canonical.encode()).hexdigest()

    def as_dict(self):
        return json.loads(self._canonical)

    def development_projection(self):
        data = self.as_dict()
        # No final inputs, hidden reference identities or checker implementation
        # metadata are handed to the model by this explicitly named projection.
        return {key: data[key] for key in ('schema_version', 'task_id', 'repo', 'base_commit',
            'workload_id', 'workload_version', 'goal', 'thresholds', 'development_inputs')} | {
            'semantic_contract': {key: data['semantics'][key] for key in (
                'strategy', 'baseline_behavior_confirmed', 'output_comparison', 'contract_reference')}}

    def original_test_result(self, outcomes):
        _require(type(outcomes) is dict and all(type(k) is str and type(v) is str for k, v in outcomes.items()),
                 'test outcomes must map exact node ids to explicit statuses')
        tests = self.as_dict()['original_tests']
        declared, xfails = set(tests['nodeids']), set(tests['expected_xfail'])
        xpasses = set(tests.get('expected_xpass', []))
        missing, unexpected = sorted(declared - outcomes.keys()), sorted(outcomes.keys() - declared)
        expected = lambda key: 'xfailed' if key in xfails else 'xpassed' if key in xpasses else 'passed'
        mismatched = {key: {'expected': expected(key), 'actual': outcomes[key]}
                      for key in sorted(declared & outcomes.keys())
                      if outcomes[key] != expected(key)}
        return {'accepted': not (missing or unexpected or mismatched), 'missing': missing,
                'unexpected': unexpected, 'mismatched': mismatched}

    def write_frozen(self, path):
        envelope = {'format': 'workload-contract-v1', 'sha256': self.sha256, 'contract': self.as_dict()}
        encoded = canonical_json(envelope) + '\n'
        with Path(path).open('x', encoding='utf-8') as stream:
            stream.write(encoded)
        return self.sha256

    @classmethod
    def read_frozen(cls, path, *, expected_sha256, adapters, checkers):
        _sha(expected_sha256, 'expected contract hash')
        path = Path(path)
        _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_DOCUMENT_BYTES,
                 'frozen contract must be a bounded regular file')
        envelope = _load_json(path.read_text(encoding='utf-8'))
        _fields(envelope, {'format', 'sha256', 'contract'}, 'frozen envelope')
        _require(envelope['format'] == 'workload-contract-v1', 'unknown frozen format')
        spec = cls(envelope['contract'], adapters=adapters, checkers=checkers)
        _require(envelope['sha256'] == expected_sha256 == spec.sha256, 'frozen contract hash mismatch')
        return spec
