"""Gold metadata with explicit truth provenance and execution-unit boundaries."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_fixtures import request_for
from tools.cutover.harness_contract import validate_labels

GROUPS=('TRUE_SINGLE_TURN','CONTEXT_DEPENDENT_TURN','HISTORICAL_RETURN','PENDING_RESPONSE','DATASET_FOLLOWUP')


def enrich(rows, *, corpus, split='PUBLIC_DEV'):
    cases=[]
    for original in rows:
        case=deepcopy(original);case['corpus']=corpus;case['split']=split
        case['source_case_hash']=digest(original)
        labels=case['labels']
        # These are reviewed corpus metadata, never runtime keyword branches.
        pending=bool(case.get('initial_pending')) or case['case_id']=='G81-075'
        dataset=case.get('dataset') is not None or case['case_id']=='G81-091'
        group=('PENDING_RESPONSE' if pending else 'DATASET_FOLLOWUP' if dataset else
               'HISTORICAL_RETURN' if labels.get('turn_relation')=='RETURN_TO_TOPIC' else
               'CONTEXT_DEPENDENT_TURN' if case.get('history') else 'TRUE_SINGLE_TURN')
        case['entry_group']=group;case['initial_pending']=pending
        case['history_kind']='DECLARED_STATE_PRECONDITION' if pending or dataset else 'EXECUTABLE_HISTORY'
        case['turn_count']=1 if pending or dataset else len(case['history'])+1
        case['precondition_description_count']=len(case['history']) if pending or dataset else 0
        case['business_timezone']='Asia/Shanghai';case['as_of_datetime']=case['clock']
        case['calendar_version']='gregorian-business-calendar-v1'
        case['scope_fingerprint']=request_for(case).authorized_semantic_scope.fingerprint()
        case['label_coverage']='PARTIAL_SEMANTIC' if set(labels)-{'turn_relation','target_task','operation'} else 'TURN_ONLY'
        case['label_provenance']={}
        for axis in labels:
            catalog_axis=axis in {'mentions','canonical_metrics','canonical_dimensions','metric_surfaces','dimension_surfaces'}
            case['label_provenance'][axis]={
                'label_source':'CATALOG_PROVEN' if catalog_axis else 'BUSINESS_CONTRACT',
                'evidence':case.get('catalog_evidence',[]) if catalog_axis else
                    ['USER_CRITICAL_MULTITURN_CONTRACT','REVIEWED_ORIGINAL_GOLD:'+case['source_case_hash']],
                'limits':'Selected mention recall/allowed roles only; no precision or complete plan claim' if axis=='mentions' else 'Only this labeled axis'}
        # These historical-return fixtures do not contain the referenced task.
        # Preserve the label and expose its precondition defect instead of
        # inventing an earlier user turn to make the runtime satisfy it.
        if case['case_id'] in {'G81-076','G81-088'}:
            case['fixture_gap']='REFERENCED_HISTORICAL_TASK_NOT_IN_GOLD_HISTORY'
        if case['case_id']=='G81-091':
            case['fixture_gap']='DATASET_PROOFS_ABSENT_FROM_ORIGINAL_GOLD'
        validate_labels(case);cases.append(case)
    return cases


def metadata(cases):
    """Public IDs/provenance only. Raw questions and private outputs stay private."""
    keys=('case_id','corpus','split','source_case_hash','entry_group','history_kind','turn_count',
          'precondition_description_count','label_coverage','label_provenance','scope','scope_fingerprint',
          'business_timezone','as_of_datetime','calendar_version','fixture_gap')
    return [{k:c[k] for k in keys if k in c} for c in cases]
