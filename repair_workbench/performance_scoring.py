"""Score operator-owned final measurements without executing or consulting an LLM.

This module consumes receipts from a trusted independent runner. Provenance
strings and hashes detect mismatches; they do not authenticate arbitrary JSON.
Model-authored reproduction observations are explicitly not acceptable inputs.
The runner, container execution and final-data access boundary are separate.
"""
from __future__ import annotations

import math
import re
import statistics

from .workload_contract import WorkloadSpec, canonical_json


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(value):
    return type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _number(value, *, memory=False):
    return ((type(value) is int if memory else type(value) in (int, float))
            and 0 <= value <= 2**63 - 1 and math.isfinite(value))


def _ratio(after, before):
    if before == 0:
        return 1.0 if after == 0 else None
    return after / before


def score_final_measurements(spec: WorkloadSpec, *, baseline_ref, candidate_ref,
                             original_test_receipt, measurements):
    """Score exact final cases, both resources and all ABBA blocks.

    Currently supports confirmed baseline-equivalence with deterministic output
    hashes. A trusted-reference oracle or additional metamorphic checks require
    their own verified receipts; this version returns inconclusive for them.
    Invalid/missing/foreign receipts raise ValueError for the batch to record as
    an evaluation error, rather than counting them as a model repair failure.
    Valid original-test or semantic regressions are failed candidates.
    """
    _require(type(spec) is WorkloadSpec, 'frozen_workload_spec_required')
    _require(_digest(baseline_ref) and _digest(candidate_ref), 'invalid_snapshot_reference')
    data, contract_hash = spec.as_dict(), spec.sha256
    execution = {key: value for key, value in data['measurement'].items() if key != 'order'}
    execution['adapter_sha256'] = data['adapter_sha256']
    expected_cases = {case['case_id']: case for case in data['final_inputs']}
    test = original_test_receipt
    _require(type(test) is dict and set(test) == {'contract_sha256', 'snapshot_ref', 'image_id',
        'suite_sha256', 'cleanup_ok', 'producer', 'outcomes'}, 'invalid_original_test_receipt')
    _require(test['producer'] == 'independent_original_test_runner'
             and test['contract_sha256'] == contract_hash and test['snapshot_ref'] == candidate_ref
             and test['image_id'] == execution['image_id']
             and test['suite_sha256'] == data['original_tests']['suite_sha256'],
             'original_test_receipt_binding_mismatch')
    verdict = {'contract_sha256': contract_hash, 'baseline_ref': baseline_ref,
               'candidate_ref': candidate_ref, 'status': 'inconclusive', 'reason': None,
               'cases': {}, 'scope': 'Independent final workload acceptance; not an LLM ability estimate.'}
    if test['cleanup_ok'] is not True:
        return {**verdict, 'reason': 'original_test_cleanup_unconfirmed'}
    test_result = spec.original_test_result(test['outcomes'])
    verdict['original_tests'] = test_result
    if not test_result['accepted']:
        return {**verdict, 'status': 'failed', 'reason': 'original_test_regression'}
    semantics = data['semantics']
    if semantics['strategy'] != 'baseline_equivalence' or semantics['metamorphic_checks']:
        return {**verdict, 'reason': 'additional_semantic_oracle_receipts_required'}

    _require(type(measurements) is list and len(measurements) == 8 * len(expected_cases),
             'incomplete_final_measurements')
    rows, environment = {}, None
    fields = {'contract_sha256', 'snapshot_ref', 'role', 'probe', 'position', 'case_id',
              'input_sha256', 'execution', 'environment', 'cleanup_ok', 'producer',
              'samples', 'output_sha256', 'output_length'}
    cleanup_ok = True
    for row in measurements:
        _require(type(row) is dict and set(row) == fields, 'invalid_final_measurement_fields')
        _require(row['producer'] == 'independent_registered_workload_runner',
                 'untrusted_measurement_producer')
        _require(row['contract_sha256'] == contract_hash
                 and canonical_json(row['execution']) == canonical_json(execution),
                 'measurement_execution_binding_mismatch')
        case_id, position, probe = row['case_id'], row['position'], row['probe']
        _require(type(case_id) is str and case_id in expected_cases, 'nonfinal_or_unknown_input')
        _require(row['input_sha256'] == expected_cases[case_id]['sha256'], 'final_input_hash_mismatch')
        _require(type(position) is int and 0 <= position <= 3 and probe in ('benchmark', 'memory'),
                 'invalid_ABBA_position_or_probe')
        expected_role = data['measurement']['order'][position]
        _require(row['role'] == expected_role
                 and row['snapshot_ref'] == (baseline_ref if expected_role == 'baseline' else candidate_ref),
                 'measurement_snapshot_or_order_mismatch')
        key = (case_id, probe, position)
        _require(key not in rows, 'duplicate_measurement_block')
        rows[key] = row
        cleanup_ok = cleanup_ok and row['cleanup_ok'] is True
        samples = row['samples']
        _require(type(samples) is list and len(samples) == execution['repeats']
                 and all(_number(v, memory=probe == 'memory') for v in samples),
                 'invalid_final_measurement_samples')
        _require(_digest(row['output_sha256']) and type(row['output_length']) is int
                 and row['output_length'] >= 0, 'invalid_output_identity')
        observed = row['environment']
        _require(type(observed) is dict and set(observed) == {'python', 'implementation', 'system', 'machine', 'clock'}
                 and all(type(v) is str and 1 <= len(v) <= 120 for v in observed.values())
                 and observed['clock'] == 'perf_counter_ns', 'invalid_measurement_environment')
        environment = observed if environment is None else environment
        _require(observed == environment, 'measurement_environment_changed')
    _require(set(rows) == {(case_id, probe, position) for case_id in expected_cases
                           for probe in ('benchmark', 'memory') for position in range(4)},
             'missing_measurement_block')
    if not cleanup_ok:
        return {**verdict, 'reason': 'measurement_cleanup_unconfirmed'}

    failed, noisy, baseline_semantic_unstable = [], [], []
    thresholds = data['thresholds']
    for case_id, case in expected_cases.items():
        case_rows = [rows[(case_id, probe, position)] for probe in ('benchmark', 'memory') for position in range(4)]
        outputs = {(r['output_sha256'], r['output_length']) for r in case_rows}
        if len({(r['output_sha256'], r['output_length']) for r in case_rows if r['role'] == 'baseline'}) != 1:
            baseline_semantic_unstable.append(case_id)
        if len(outputs) != 1:
            failed.append((case_id, 'output_behavior_changed_or_nondeterministic'))
        metrics = {}
        for probe, metric in (('benchmark', 'time_ratio'), ('memory', 'memory_ratio')):
            grouped = {role: [v for r in case_rows if r['probe'] == probe and r['role'] == role
                              for v in r['samples']] for role in ('baseline', 'candidate')}
            medians = {role: statistics.median(values) for role, values in grouped.items()}
            spreads = {role: ((max(values) - min(values)) / medians[role] if medians[role] else
                              (0.0 if max(values) == 0 else None)) for role, values in grouped.items()}
            ratio = _ratio(medians['candidate'], medians['baseline'])
            metrics[metric] = ratio
            metrics[probe + '_medians'] = medians
            metrics[probe + '_relative_spread'] = spreads
            if any(v is None or v > thresholds['max_relative_noise'] for v in spreads.values()):
                noisy.append((case_id, probe))
        verdict['cases'][case_id] = {'role': case['role'], **metrics}
        if metrics['time_ratio'] is None or metrics['time_ratio'] > thresholds['max_time_ratio']:
            failed.append((case_id, 'time_guardrail_exceeded'))
        if metrics['memory_ratio'] is None or metrics['memory_ratio'] > thresholds['max_memory_ratio']:
            failed.append((case_id, 'memory_guardrail_exceeded'))
        if case['role'] == 'target':
            if data['goal'] == 'speed':
                if metrics['benchmark_medians']['baseline'] <= 0:
                    noisy.append((case_id, 'zero_baseline_time'))
                elif metrics['time_ratio'] is None or metrics['time_ratio'] > 1 - thresholds['min_speedup']:
                    failed.append((case_id, 'speed_target_not_met'))
            else:
                if metrics['memory_medians']['baseline'] <= 0:
                    noisy.append((case_id, 'zero_baseline_memory'))
                elif metrics['memory_ratio'] is None or metrics['memory_ratio'] > 1 - thresholds['min_memory_reduction']:
                    failed.append((case_id, 'memory_target_not_met'))
    verdict.update(failures=failed, unstable_measurements=noisy,
                   baseline_semantic_unstable=baseline_semantic_unstable)
    if baseline_semantic_unstable:
        verdict['reason'] = 'baseline_output_not_deterministic'
    elif any(reason.startswith('output_') for _, reason in failed):
        verdict.update(status='failed', reason='semantic_regression')
    elif noisy:
        verdict['reason'] = 'measurement_unstable_or_not_measurable'
    elif failed:
        verdict.update(status='failed', reason='joint_resource_targets_not_met')
    else:
        verdict.update(status='passed', reason='original_tests_semantics_and_resource_targets_met')
    return verdict
