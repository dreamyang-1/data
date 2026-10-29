"""Registered execution adapters and bounded outcome/provenance records."""
from __future__ import annotations

import math
import hashlib,json
from .registry import CATALOG_VERSION, TOOLS


def capability_errors(intent, selection, facts):
    """Full profiles allow declared non-computable results, never invented facts."""
    requested=set(selection.get('capabilities') or [])
    actual=set(facts.get('tool_selection') or [])
    errors=[]
    if actual!=requested:errors.append('实际工具集合与模型确认计划不一致')
    for record in facts.get('tool_executions') or []:
        if record['status']=='FAILED':errors.append(record['capability_id']+'执行失败')
    candidates=[key.split(':',1)[1] for key in requested if key.startswith('model:')]
    scores=facts.get('candidate_scores') or {}
    if candidates and not set(candidates).issubset(scores):
        errors.append('候选预测模型缺少回测证据')
    if len(candidates)==1 and facts.get('selected_model')!=candidates[0]:
        errors.append('未使用用户指定预测模型')
    if requested=={'forecast_backtest'} and facts.get('forecast') is not None:
        errors.append('仅回测任务出现未来预测')
    return errors


def attach_execution_records(request, facts):
    contract=getattr(request,'_analysis_contract',None) or {}
    selection=contract.get('tool_selection') or {}
    if selection.get('source')!='MODEL_CATALOG':return
    from app.analysis.tool_selection import report_capability
    reports=facts.get('capability_results') or []
    records=[]
    for capability in selection.get('capabilities') or []:
        matching=[r for r in reports if report_capability(request.primary_intent,r.get('method'))==capability]
        if capability in {'multidimensional_attribution','multidimensional_ratio_attribution'}:
            matching=[r for r in reports if r.get('method') in {'multidimensional_root_cause',capability}]
        model_id=capability.split(':',1)[1] if capability.startswith('model:') else None
        if model_id:
            score=(facts.get('candidate_scores') or {}).get(model_id)
            status='COMPLETED' if score is not None else 'NOT_COMPUTABLE'
            outputs={'candidate':model_id,'score':score,'selected':facts.get('selected_model')==model_id}
            methods=['expanding_window_one_step_backtest']
            reason=None if score is not None else 'NO_BACKTEST_EVIDENCE'
        else:
            statuses={r.get('status') for r in matching}
            status=('FAILED' if not matching else
                'PARTIAL' if 'PARTIAL' in statuses or 'COMPLETED' in statuses and len(statuses)>1 else
                'COMPLETED' if statuses=={'COMPLETED'} else
                'NOT_APPLICABLE' if statuses=={'NOT_APPLICABLE'} else 'NOT_COMPUTABLE')
            outputs={'reports':matching}
            methods=list(dict.fromkeys(r.get('method') for r in matching))
            reason=None if status=='COMPLETED' else ('REGISTERED_CAPABILITY_RESULT_MISSING' if not matching
                else '; '.join(r.get('reason') or '' for r in matching) or 'INPUT_OR_METHOD_NOT_APPLICABLE')
        record={'capability_id':capability,'catalog_version':CATALOG_VERSION,'executor_id':TOOLS[capability]['executor_id'],
            'status':status,'reason':reason,'methods':methods,'effective_parameters':selection.get('parameters',{}).get(capability,{}),
            'scope':{'target_window':contract.get('target_window'),'grain':contract.get('grain')},
            'input_task_ids':contract.get('source_task_ids') or [],'outputs':outputs}
        fingerprint=hashlib.sha256(json.dumps({'conversation':request.conversation_id,'question':request.original_question,
            'record':record},sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()[:24]
        record['result_ref']='analysis:'+capability+':'+fingerprint
        from .results import ToolExecutionRecord
        records.append(ToolExecutionRecord.model_validate(record).model_dump(mode='json'))
    facts['tool_executions']=records
    facts['tool_catalog_version']=CATALOG_VERSION
    facts['tool_selection_source']='MODEL_CATALOG'


def validate_finite_results(value):
    if isinstance(value,float) and not math.isfinite(value):raise ValueError('NON_FINITE_TOOL_OUTPUT')
    if isinstance(value,dict):
        for item in value.values():validate_finite_results(item)
    elif isinstance(value,(list,tuple)):
        for item in value:validate_finite_results(item)
