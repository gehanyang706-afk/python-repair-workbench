"""Evidence-bound requirements and deterministic scope review, not a semantic oracle."""
import hashlib
import json

from .edits import EditRejected
from .source_context import units, change_review
from .snapshot import verify_snapshot


def bounded_text(value, limit=2000):
    return isinstance(value,str) and 0 < len(value.strip()) <= limit


def validate_plan(session, requirements, scope):
    verify_snapshot(session.baseline)
    if not isinstance(requirements,list) or not 2 <= len(requirements) <= 16:
        raise EditRejected('invalid_requirements')
    hashes={f['path']:f['sha256'] for f in session.manifest['files']}
    ids=set(); kinds=set()
    for item in requirements:
        if not isinstance(item,dict) or set(item) != {'id','kind','task_quote','claim','evidence'}:
            raise EditRejected('invalid_requirement_fields')
        identity=item['id']
        if not bounded_text(identity,40) or identity in ids:
            raise EditRejected('invalid_requirement_id')
        ids.add(identity)
        if not isinstance(item['kind'],str):
            raise EditRejected('invalid_requirement_kind_or_claim')
        kinds.add(item['kind'])
        if item['kind'] not in {'change','preserve'} or not bounded_text(item['claim']):
            raise EditRejected('invalid_requirement_kind_or_claim')
        if not bounded_text(item['task_quote']) or item['task_quote'] not in session.task:
            raise EditRejected('requirement_not_grounded_in_task')
        if not isinstance(item['evidence'],list) or not 1 <= len(item['evidence']) <= 6:
            raise EditRejected('missing_requirement_evidence')
        for ref in item['evidence']:
            if not isinstance(ref,dict) or set(ref) != {'path','sha256','start_line','end_line'}:
                raise EditRejected('invalid_evidence_fields')
            path=ref['path'];start=ref['start_line'];end=ref['end_line']
            if not isinstance(path,str) or path not in hashes or ref['sha256'] != hashes[path]:
                raise EditRejected('evidence_version_or_path_mismatch')
            count=len((session.baseline/'workspace'/path).read_bytes().splitlines())
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= count or end-start >= 200:
                raise EditRejected('invalid_evidence_range')
    if kinds != {'change','preserve'}:
        raise EditRejected('change_and_preserve_requirements_required')
    if not isinstance(scope,list) or not 1 <= len(scope) <= 32:
        raise EditRejected('invalid_repair_scope')
    seen=set()
    for item in scope:
        if not isinstance(item,dict) or set(item) != {'path','symbol'}:
            raise EditRejected('invalid_scope_fields')
        path,symbol=item['path'],item['symbol']
        if not isinstance(path,str) or not isinstance(symbol,str) or path not in session.allowed_paths:
            raise EditRejected('scope_path_not_allowed')
        key=(path,symbol)
        if key in seen or symbol not in units((session.baseline/'workspace'/path).read_bytes().decode()):
            raise EditRejected('scope_symbol_missing_or_duplicate')
        seen.add(key)
    plan={'task_sha256':hashlib.sha256(session.task.encode()).hexdigest(),
          'requirements':requirements,'scope':scope}
    plan['plan_id']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    return plan


def review_changes(session, changes, assessment):
    plan=session.repair_plan
    if not plan:
        raise EditRejected('repair_plan_required')
    if not isinstance(assessment,dict) or assessment.get('plan_id') != plan['plan_id']:
        raise EditRejected('assessment_plan_mismatch', expected_plan_id=plan['plan_id'])
    coverage=assessment.get('coverage')
    required={r['id'] for r in plan['requirements']}
    if (not isinstance(coverage,dict) or set(coverage) != required
            or not all(bounded_text(v) for v in coverage.values())):
        raise EditRejected('incomplete_requirement_coverage', required_ids=sorted(required))
    touched=[];risks=[]
    for path,new in changes.items():
        old=(session.baseline/'workspace'/path).read_bytes().decode()
        c,r=change_review(old,new,path);touched.extend(c);risks.extend(r)
    scope={(x['path'],x['symbol']) for x in plan['scope']}
    outside=[x for x in touched if (x['path'],x['symbol']) not in scope]
    if outside:
        raise EditRejected('change_outside_declared_scope', outside=outside)
    acknowledgements=assessment.get('risk_acknowledgements',[])
    if not isinstance(acknowledgements,list):
        raise EditRejected('invalid_risk_acknowledgements')
    by_kind={}
    for item in acknowledgements:
        if not isinstance(item,dict) or set(item) != {'kind','requirement_ids','explanation'}:
            raise EditRejected('invalid_risk_acknowledgement')
        refs=item['requirement_ids']
        if (not bounded_text(item['kind'],80) or item['kind'] in by_kind or not bounded_text(item['explanation'])
                or not isinstance(refs,list) or not refs or not all(isinstance(x,str) for x in refs) or not set(refs) <= required):
            raise EditRejected('invalid_risk_acknowledgement')
        by_kind[item['kind']]=item
    missing=[r for r in risks if r['kind'] not in by_kind]
    preserve={r['id'] for r in plan['requirements'] if r['kind']=='preserve'}
    ungrounded=[r for r in risks if r['kind'] in by_kind and not preserve.intersection(by_kind[r['kind']]['requirement_ids'])]
    if missing or ungrounded:
        raise EditRejected('risk_review_required', risks=risks[:20],
                           hint='Explain every risk kind and link preserved-constraint IDs; this does not prove semantic correctness.')
    return {'plan_id':plan['plan_id'],'coverage':coverage,'touched':touched,
            'risks':risks,'risk_acknowledgements':acknowledgements,
            'assurance':'Scope/evidence checks passed; model-authored coverage is not a semantic proof.'}
