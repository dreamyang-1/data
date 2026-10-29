"""Compare observed multi-turn state without treating unobserved safety as zero.

This is an offline evidence format, never a public request/response contract.
Observation modes stay separate: a rule-service or scripted-transport replay
cannot be reported as a live-model/full-orchestrator accuracy measurement.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from tools.cutover.evaluation_contract import digest
from tools.cutover.semantic_evaluator import read_jsonl


AXES = {
    'turn_relation', 'target_task', 'metric_surfaces', 'dimension_surfaces',
    'region_values', 'time_relation', 'time_grain', 'metric_clarification_required',
    'canonical_metrics', 'canonical_dimensions', 'dataset_route', 'pending_action',
    'slot_operations', 'metric_operations', 'dimension_operations', 'region_operations',
    'semantic_plan', 'clarification_decision',
}
UNORDERED = {'metric_surfaces', 'dimension_surfaces', 'region_values',
    'canonical_metrics', 'canonical_dimensions', 'slot_operations',
    'metric_operations', 'dimension_operations', 'region_operations'}
SAFETY = {'scope_expansion', 'cross_scope_pending_reuse', 'cross_scope_dataset_reuse',
    'pending_hijack', 'wrong_inheritance', 'wrong_silent_auto_accept', 'unsafe_truncated_ranking'}
MODES = {'RULE_SERVICE_OFFLINE', 'SCRIPTED_MODEL_PIPELINE', 'RECORDED_MODEL_PIPELINE',
    'LIVE_MODEL_PLAN_ONLY', 'FULL_ORCHESTRATOR_OFFLINE'}


def _same(expected, actual, *, unordered=False):
    # JSON types are meaningful: bool True must not pass an expected integer 1.
    if type(expected) is not type(actual):
        return False
    if unordered:
        return isinstance(actual, list) and Counter(digest(v) for v in expected) == Counter(digest(v) for v in actual)
    return digest(expected) == digest(actual)


def validate_transitions(rows, catalog):
    if catalog.get('artifact_hash') != digest({k:v for k,v in catalog.items() if k != 'artifact_hash'}):
        raise ValueError('EVALUATION_CATALOG_HASH_INVALID')
    facts = {f['fact_id'] for f in catalog['facts']}; ids = set()
    for row in rows:
        if row['case_id'] in ids:
            raise ValueError('DUPLICATE_CASE_ID')
        ids.add(row['case_id'])
        if row['scope'] != catalog['scope'] or row['catalog_ref'] != catalog['artifact_hash']:
            raise ValueError('GOLD_SCOPE_OR_CATALOG_MISMATCH')
        if row.get('label_status') != 'REVIEWED_FOR_LISTED_AXES' or not row.get('history') or not row.get('current_utterance'):
            raise ValueError('TRANSITION_LABEL_EVIDENCE_REQUIRED')
        if not row.get('business_evidence') or not row.get('catalog_evidence') or not set(row['catalog_evidence']) <= facts:
            raise ValueError('TRANSITION_EVIDENCE_MISSING')
        if not row.get('labels') or not set(row['labels']) <= AXES:
            raise ValueError('UNKNOWN_OR_EMPTY_TRANSITION_AXIS')
        if not set(row.get('safety_checks', [])) <= SAFETY or len(set(row.get('safety_checks', []))) != len(row.get('safety_checks', [])):
            raise ValueError('UNKNOWN_OR_DUPLICATE_SAFETY_CHECK')
        for key,value in row['labels'].items():
            if key in UNORDERED and not isinstance(value,list):
                raise ValueError('COLLECTION_LABEL_REQUIRED')
            if key.startswith('canonical_') and not set(value) <= facts:
                raise ValueError('CANONICAL_LABEL_NOT_IN_FROZEN_CATALOG')
            if key in {'canonical_metrics','canonical_dimensions'}:
                prefix='METRIC:' if key=='canonical_metrics' else 'DIMENSION:'
                if any(not ref.startswith(prefix) for ref in value):
                    raise ValueError('CANONICAL_LABEL_ROLE_MISMATCH')
    return ids


def evaluate_transitions(rows, predictions, catalog, *, component, mode):
    if mode not in MODES or not component:
        raise ValueError('OBSERVATION_PROVENANCE_REQUIRED')
    ids = validate_transitions(rows,catalog); supplied = {}
    for p in predictions:
        if p['case_id'] not in ids or p['case_id'] in supplied:
            raise ValueError('UNKNOWN_OR_DUPLICATE_PREDICTION')
        if p['scope'] != catalog['scope'] or p['catalog_ref'] != catalog['artifact_hash']:
            raise ValueError('PREDICTION_SCOPE_OR_CATALOG_MISMATCH')
        if p.get('component') != component or p.get('mode') != mode:
            raise ValueError('MIXED_OBSERVATION_PROVENANCE')
        if p.get('status') not in {'OK','FAILED','NOT_RUN'}:
            raise ValueError('UNKNOWN_OBSERVATION_STATUS')
        if not set(p.get('axes',{})) <= AXES or not set(p.get('safety',{})) <= SAFETY:
            raise ValueError('UNKNOWN_OBSERVATION_AXIS')
        if any(type(v) is not bool for v in p.get('safety',{}).values()):
            raise ValueError('SAFETY_OBSERVATION_MUST_BE_BOOLEAN')
        supplied[p['case_id']] = p
    totals=Counter();passed=Counter();unobserved=Counter();safety_totals=Counter();safety_unknown=Counter();violations=Counter();details=[]
    for row in rows:
        p=supplied.get(row['case_id']);valid=bool(p and p['status']=='OK')
        actual=p.get('axes',{}) if valid else {}; observed=p.get('safety',{}) if valid else {}
        detail={'case_id':row['case_id'],'axes':{},'safety':{},'observation_status':p['status'] if p else 'MISSING'}
        for axis,expected in row['labels'].items():
            totals[axis]+=1
            if axis not in actual:
                unobserved[axis]+=1;result='NOT_OBSERVED'
            else:
                ok=_same(expected,actual[axis],unordered=axis in UNORDERED)
                passed[axis]+=ok;result='PASS' if ok else 'FAIL'
            detail['axes'][axis]=result
        # A supplied safety violation cannot disappear merely because this
        # case did not originally label that axis. Unobserved axes stay open.
        for check in set(row.get('safety_checks',[])) | set(observed):
            safety_totals[check]+=1
            if check not in observed:
                safety_unknown[check]+=1;result='NOT_OBSERVED'
            else:
                violations[check]+=observed[check];result='VIOLATION' if observed[check] else 'NO_VIOLATION_OBSERVED'
            detail['safety'][check]=result
        details.append(detail)
    return {'contract':'multiturn-transition-evaluation-v1','component':component,'mode':mode,
        'cases':len(rows),'gold_hash':digest(rows),'catalog_ref':catalog['artifact_hash'],'catalog_version':catalog['catalog_version'],'scope':catalog['scope'],
        'metrics':{k:{'passed':passed[k],'denominator':n,'unobserved':unobserved[k],'value':passed[k]/n} for k,n in sorted(totals.items())},
        'safety':{k:{'required_or_supplied_observations':safety_totals[k],'violations':violations[k],'unobserved':safety_unknown[k],
            'gate':'FAIL' if violations[k] else 'NOT_EVALUATED' if safety_unknown[k] or not safety_totals[k] else 'PASS_FOR_OBSERVED_COMPONENT'} for k in sorted(SAFETY)},
        'unlabeled_safety_checks':sorted(SAFETY-{k for row in rows for k in row.get('safety_checks',[])}),
        'full_pipeline_accuracy':'NOT_ESTABLISHED_BY_COMPONENT_RESULTS','production_cutover_pass':False,'details':details}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for arg in ('gold','catalog','predictions','output'):p.add_argument('--'+arg,type=Path,required=True)
    p.add_argument('--component',required=True);p.add_argument('--mode',choices=sorted(MODES),required=True)
    a=p.parse_args()
    result=evaluate_transitions(read_jsonl(a.gold),read_jsonl(a.predictions),json.loads(a.catalog.read_text(encoding='utf-8')),component=a.component,mode=a.mode)
    with a.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='details'},ensure_ascii=False))
