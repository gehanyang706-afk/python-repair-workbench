"""Deterministic, metadata-only candidate selection before model inference.

Only instance_id/id, repo and base_commit are read from official rows. Gold
patches, test patches, test identities and model outcomes never influence this
module. A candidate pool is not an environment qualification or model score.
The caller must bind the original dataset/revision and qualification receipts
with its experiment manifest; the hashes here are consistency checks, not
signatures or proof of truthful operator declarations.
"""
from __future__ import annotations

import hashlib
import json
import re


_SPLITS = ('dev', 'validation', 'test')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
_REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
_COMMIT = re.compile(r'[a-f0-9]{40}\Z')
_REVISION = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _copy(value):
    return json.loads(_bytes(value))


def _text(value, pattern, field):
    if type(value) is not str or not pattern.fullmatch(value):
        raise ValueError('invalid_' + field)
    return value


def _counts(value, field):
    if type(value) is not dict or set(value) != set(_SPLITS):
        raise ValueError(field + '_requires_dev_validation_test')
    if any(type(count) is not int or not 0 <= count <= 10000 for count in value.values()):
        raise ValueError('invalid_' + field)
    return {split: value[split] for split in _SPLITS}


def _unique_strings(values, field):
    if type(values) is not list:
        raise ValueError(field + '_must_be_list')
    result = [_text(item, _ID, field) for item in values]
    if len(set(result)) != len(result):
        raise ValueError('duplicate_' + field)
    return result


def _public_row(row):
    if type(row) is not dict:
        raise ValueError('invalid_source_row')
    identifier = row.get('instance_id', row.get('id'))
    identifier = _text(identifier, _ID, 'task_id')
    if 'id' in row and 'instance_id' in row and row['id'] != row['instance_id']:
        raise ValueError('conflicting_task_ids')
    return {'id': identifier, 'repo': _text(row.get('repo'), _REPO, 'repo'),
            'base_commit': _text(row.get('base_commit'), _COMMIT, 'base_commit')}


def _interleave(candidates, repositories, seed):
    queues = {repo: sorted((c['id'] for c in candidates if c['repo'] == repo),
                          key=lambda identifier: _hash([seed, 'task', identifier]))
              for repo in repositories}
    result = []
    while any(queues.values()):
        for repo in repositories:
            if queues[repo]:
                result.append(queues[repo].pop(0))
    return result


def create_candidate_pool(rows, *, dataset_name, revision, source_split, seed,
                          targets, metadata=None, developer_exposed=None,
                          repository_order=None, minimum_repositories=None):
    """Freeze candidate order and screening declarations without reading answers.

    ``metadata`` maps IDs to optional cluster_id/eligible/reason fields. Default
    clusters conservatively group the same repo/base commit. Developer exposure
    makes the entire cluster dev-only. ``repository_order`` must contain every
    source repository exactly once; otherwise seeded hashes order repositories.
    All source candidates retain their position and screening record, including
    excluded candidates. Environment selection happens separately before runs.
    """
    _text(dataset_name, _REPO, 'dataset_name')
    _text(revision, _REVISION, 'dataset_revision')
    _text(source_split, _ID, 'source_split')
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise ValueError('invalid_seed')
    targets = _counts(targets, 'targets')
    if not sum(targets.values()):
        raise ValueError('at_least_one_target_required')
    minima = _counts(dict.fromkeys(_SPLITS, 0) if minimum_repositories is None
                     else minimum_repositories, 'minimum_repositories')
    if any(minima[s] > targets[s] for s in _SPLITS):
        raise ValueError('repository_minimum_exceeds_target')
    if type(rows) is not list or not rows:
        raise ValueError('nonempty_source_rows_required')
    candidates = [_public_row(row) for row in rows]
    ids = {row['id'] for row in candidates}
    if len(ids) != len(candidates):
        raise ValueError('duplicate_task_id')
    metadata = {} if metadata is None else metadata
    if type(metadata) is not dict or not set(metadata) <= ids:
        raise ValueError('metadata_references_unknown_tasks')
    exposed = sorted(_unique_strings([] if developer_exposed is None else developer_exposed,
                                     'developer_exposed'))
    if not set(exposed) <= ids:
        raise ValueError('exposure_references_unknown_tasks')
    for candidate in candidates:
        item = metadata.get(candidate['id'], {})
        if type(item) is not dict or not set(item) <= {'cluster_id', 'eligible', 'reason'}:
            raise ValueError('invalid_selection_metadata')
        eligible = item.get('eligible', True)
        if type(eligible) is not bool:
            raise ValueError('metadata_eligible_must_be_bool')
        candidate.update(
            cluster_id=_text(item.get('cluster_id', 'base:' + _hash(
                [candidate['repo'], candidate['base_commit']])), _ID, 'cluster_id'),
            eligible=eligible,
            reason=_text(item.get('reason', 'eligible' if eligible else 'metadata_excluded'),
                         _ID, 'screening_reason'))
    exposed_clusters = {row['cluster_id'] for row in candidates if row['id'] in exposed}
    for candidate in candidates:
        candidate['dev_only'] = candidate['cluster_id'] in exposed_clusters
    candidates.sort(key=lambda row: row['id'])
    repos = {row['repo'] for row in candidates}
    if repository_order is None:
        repository_order = sorted(repos, key=lambda repo: _hash([seed, 'repo', repo]))
    elif (type(repository_order) is not list
          or any(type(repo) is not str for repo in repository_order)
          or len(set(repository_order)) != len(repository_order) or set(repository_order) != repos):
        raise ValueError('repository_order_must_list_all_repositories_once')
    material = {
        'schema_version': 1, 'kind': 'candidate_pool',
        'dataset': {'name': dataset_name, 'revision': revision, 'source_split': source_split},
        'rule': {'seed': seed, 'targets': targets, 'minimum_repositories': minima,
                 'split_order': list(_SPLITS), 'repository_order': list(repository_order),
                 'default_cluster_rule': 'repo_base_commit_v1',
                 'cluster_policy': 'one_task_per_cluster; exposed_cluster_dev_only',
                 'developer_exposed': exposed},
        'candidates': candidates,
        'candidate_order': _interleave(candidates, repository_order, seed),
        'screening': [{'id': row['id'], 'status': 'eligible' if row['eligible'] else 'excluded',
                       'reason': row['reason']} for row in candidates],
        'counts': {'source_rows': len(candidates),
                   'metadata_eligible': sum(row['eligible'] for row in candidates)}}
    return {**material, 'pool_sha256': _hash(material)}


def validate_candidate_pool(pool, *, expected_sha256=None):
    """Rebuild every derived field; optionally pin an externally retained hash."""
    if type(pool) is not dict:
        raise ValueError('invalid_candidate_pool')
    if type(pool.get('schema_version')) is not int or pool['schema_version'] != 1:
        raise ValueError('invalid_candidate_pool_schema')
    digest = _text(pool.get('pool_sha256'), _DIGEST, 'pool_digest')
    if expected_sha256 is not None:
        _text(expected_sha256, _DIGEST, 'expected_pool_digest')
        if digest != expected_sha256:
            raise ValueError('unexpected_pool_digest')
    material = {key: value for key, value in pool.items() if key != 'pool_sha256'}
    if _hash(material) != digest:
        raise ValueError('candidate_pool_hash_mismatch')
    try:
        dataset, rule, candidates = pool['dataset'], pool['rule'], pool['candidates']
        rebuilt = create_candidate_pool(candidates, dataset_name=dataset['name'],
            revision=dataset['revision'], source_split=dataset['source_split'], seed=rule['seed'],
            targets=rule['targets'], minimum_repositories=rule['minimum_repositories'],
            developer_exposed=rule['developer_exposed'], repository_order=rule['repository_order'],
            metadata={row['id']: {key: row[key] for key in ('cluster_id', 'eligible', 'reason')}
                      for row in candidates})
    except (KeyError, TypeError) as exc:
        raise ValueError('invalid_candidate_pool_structure') from exc
    if _bytes(pool) != _bytes(rebuilt):
        raise ValueError('candidate_pool_derived_fields_changed')
    return _copy(pool)


def freeze_qualified_subset(pool, qualifications, *, inference_started,
                            expected_pool_sha256):
    """Close selection before inference using only predeclared qualification facts.

    Qualifications map IDs to exactly qualified/evidence_sha256/reason, where
    qualified is a strict bool and reason is a bounded public reason code. This
    interface deliberately rejects model scores and success/failure outcomes.
    Missing qualification remains not_checked, never implicitly qualified.
    A caller must enforce its own durable inference-start fence; the required
    boolean here is an operator declaration, not proof that no model has run.
    """
    if type(inference_started) is not bool or inference_started:
        raise ValueError('selection_must_close_before_inference')
    frozen = validate_candidate_pool(pool, expected_sha256=expected_pool_sha256)
    candidates = {row['id']: row for row in frozen['candidates']}
    if type(qualifications) is not dict or not set(qualifications) <= set(candidates):
        raise ValueError('qualification_references_unknown_task')
    receipts = {}
    for identifier, receipt in qualifications.items():
        if (type(receipt) is not dict
                or set(receipt) != {'qualified', 'evidence_sha256', 'reason'}
                or type(receipt['qualified']) is not bool):
            raise ValueError('invalid_qualification_summary')
        receipts[identifier] = {'qualified': receipt['qualified'],
            'evidence_sha256': _text(receipt['evidence_sha256'], _DIGEST, 'qualification_digest'),
            'reason': _text(receipt['reason'], _ID, 'qualification_reason')}
    order = frozen['candidate_order']
    qualified = [identifier for identifier in order if candidates[identifier]['eligible']
                 and receipts.get(identifier, {}).get('qualified') is True]
    selected, used_clusters = [], set()
    for split in _SPLITS:
        # Qualified exposed clusters get the dev slots first; they can never
        # displace independent tasks into validation/test through exposure.
        ranked = ([i for i in qualified if candidates[i]['dev_only']]
                  + [i for i in qualified if not candidates[i]['dev_only']]) if split == 'dev' else qualified
        count = 0
        for identifier in ranked:
            row = candidates[identifier]
            if (count >= frozen['rule']['targets'][split] or row['cluster_id'] in used_clusters
                    or (split != 'dev' and row['dev_only'])):
                continue
            selected.append({key: row[key] for key in ('id', 'repo', 'base_commit', 'cluster_id')}
                            | {'split': split, 'qualification': receipts[identifier]})
            used_clusters.add(row['cluster_id'])
            count += 1
    selected_ids = {row['id'] for row in selected}
    screening = []
    for identifier in order:
        row = candidates[identifier]
        if not row['eligible']:
            status, reason = 'excluded', row['reason']
        elif identifier not in receipts:
            status, reason = 'not_checked', 'qualification_missing'
        elif not receipts[identifier]['qualified']:
            status, reason = 'qualification_failed', receipts[identifier]['reason']
        elif identifier in selected_ids:
            status, reason = 'selected', 'frozen_before_inference'
        elif row['cluster_id'] in used_clusters:
            status, reason = 'excluded', 'cluster_already_selected'
        elif row['dev_only']:
            status, reason = 'excluded', 'developer_exposure_dev_capacity'
        else:
            status, reason = 'not_selected', 'target_capacity'
        screening.append({'id': identifier, 'status': status, 'reason': reason})
    achieved = {split: sum(row['split'] == split for row in selected) for split in _SPLITS}
    diversity = {split: len({row['repo'] for row in selected if row['split'] == split})
                 for split in _SPLITS}
    material = {'schema_version': 1, 'kind': 'qualified_subset',
        'pool_sha256': frozen['pool_sha256'], 'dataset': frozen['dataset'], 'rule': frozen['rule'],
        'inference_started': False, 'qualification_summaries': dict(sorted(receipts.items())),
        'selected': selected, 'screening': screening,
        'counts': {**frozen['counts'], 'qualification_checked': len(receipts),
                   'qualified_metadata_eligible': len(qualified), 'selected': len(selected)},
        'achieved': achieved, 'repository_counts': diversity,
        'shortfall': {split: frozen['rule']['targets'][split] - achieved[split] for split in _SPLITS},
        'repository_shortfall': {split: max(0, frozen['rule']['minimum_repositories'][split] - diversity[split])
                                 for split in _SPLITS}}
    return {**material, 'selection_sha256': _hash(material)}


def candidate_pool_from_json(text, **kwargs):
    """Parse a JSON row list with duplicate-key rejection, then project metadata."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    return create_candidate_pool(json.loads(text, object_pairs_hook=unique), **kwargs)
