"""Extract observations from existing service outputs, never from Gold labels."""
from __future__ import annotations
from datetime import date,datetime
from unittest.mock import patch

from tools.cutover.transition_evaluator import MODES


def _base(case,component,mode):
    return {'case_id':case['case_id'],'scope':case['scope'],'catalog_ref':case['catalog_ref'],
        'component':component,'mode':mode,'status':'OK','axes':{},'safety':{}}


def observe_legacy_rules(case):
    """Actual classifier/admission/merge service seam; no rewriter/model/SQL.

    No intended target is injected for historical recall, no name is promoted
    to a canonical ID, and required slots are not equated with public questions.
    This sequential offline harness fixes the existing classifier's date clock.
    """
    from app.domain.models import TrustedIdentity,TurnRelation
    from app.intent import RuleBasedIntentClassifier
    from app.services.turn_admission import TurnAdmissionGate
    from app.services.dataset_followup import plan_dataset_followup
    out=_base(case,'V1_RULE_AND_DATASET_SERVICES','RULE_SERVICE_OFFLINE')
    dataset=case.get('dataset')
    if dataset is not None:
        result=plan_dataset_followup(case['current_utterance'],dataset['columns'],dataset['rows'],source_complete=dataset['source_complete'])
        if result and result.get('type')=='limit':route='DISPLAY_LIMIT'
        elif result and result.get('type')=='sort_limit':route='LOCAL_RANK_COMPLETE_DATASET' if dataset['source_complete'] else 'UNSAFE_LOCAL_RANK'
        else:route='GLOBAL_RANK_REPLAN_OR_REJECT' if result is None else 'OTHER'
        out['axes']['dataset_route']=route
        out['safety']['unsafe_truncated_ranking']=route=='UNSAFE_LOCAL_RANK'
        out['evidence']={'boundary':'plan_dataset_followup','source_complete':dataset['source_complete'],'production_orchestrator_executed':False}
        return out
    clock=datetime.fromisoformat(case['clock'])
    class FixedDate(date):
        @classmethod
        def today(cls):return cls(clock.year,clock.month,clock.day)
    classifier=RuleBasedIntentClassifier();gate=TurnAdmissionGate()
    identity=TrustedIdentity(tenant_id='evaluation',user_id='evaluation')
    previous=None;previous_time=None;thread_count=0;decision=None
    with patch('app.intent.classifier.date',FixedDate):
        utterances=[*case['history'],case['current_utterance']]
        for i,text in enumerate(utterances):
            current=classifier.classify(text,identity,case['case_id'])
            pending=bool(case.get('initial_pending') and i==len(utterances)-1)
            decision=gate.evaluate(question=text,current=current,previous=previous,message_id=f'turn-{i}',pending=pending)
            previous_time=previous.time_range.model_dump(mode='json') if previous and previous.time_range else None
            merged=classifier.merge_clarification(previous.model_copy(deep=True),text) if previous and decision.inherit_business_context else current.model_copy(deep=True)
            result=gate.apply_explicit_slot_protection(merged,current,decision)
            if decision.inherit_business_context and previous:
                result.analysis_thread_id=previous.analysis_thread_id
            else:
                result.analysis_thread_id=f'observed-task-{thread_count}';thread_count+=1
            previous=result
        actual_time=result.time_range.model_dump(mode='json') if result.time_range else None
        relation=('NEW_TASK' if decision.relation==TurnRelation.STANDALONE_NEW_TOPIC else
            'RETURN_TO_TOPIC' if decision.relation==TurnRelation.HISTORICAL_TOPIC_RETURN else
            'ANSWER_PENDING' if decision.relation==TurnRelation.CLARIFICATION_RESPONSE else
            'UNRESOLVED' if decision.relation==TurnRelation.AMBIGUOUS_RELATION else 'FOLLOW_UP')
        target='UNRESOLVED' if decision.historical_recall_required else 'PREVIOUS' if decision.inherit_business_context else 'NEW'
        regions=[]
        for item in result.filters:
            if item.get('field') in {'地区','业务省份','业务城市'}:
                value=item.get('value');regions.extend(value if isinstance(value,list) else [value])
        grain=decision.current_turn_facts.explicit_slots.get('time_grain')
        operations=sorted({op.operation.value for op in decision.slot_operations if op.operation.value in {'ADD','REPLACE','REMOVE','CLEAR'}})
        out['axes'].update(turn_relation=relation,target_task=target,metric_surfaces=[m.input for m in result.metrics],
            dimension_surfaces=list(result.dimensions),region_values=regions,
            time_relation='ABSENT' if actual_time is None else 'SAME_AS_PREVIOUS' if actual_time==previous_time else 'CHANGED',
            metric_clarification_required='metric' in classifier.required_missing_slots(result),slot_operations=operations)
        for slot,axis in [('metrics','metric_operations'),('dimensions','dimension_operations'),('region','region_operations')]:
            out['axes'][axis]=sorted({op.operation.value for op in decision.slot_operations if op.slot==slot and op.operation.value in {'ADD','REPLACE','REMOVE','CLEAR'}})
        if grain:out['axes']['time_grain']=str(grain.value).upper()
        if case.get('initial_pending'):
            out['axes']['pending_action']='ANSWER' if relation=='ANSWER_PENDING' else 'DETACHED' if relation=='NEW_TASK' and not decision.inherit_business_context else 'UNRESOLVED'
            out['safety']['pending_hijack']=relation=='ANSWER_PENDING'
        # Do not invent a zero wrong-inheritance/scope rate from this partial seam.
        out['evidence']={'boundary':'RuleBasedIntentClassifier -> TurnAdmissionGate -> merge_clarification -> explicit_slot_protection',
            'operation_slots':[{'slot':o.slot,'operation':o.operation.value} for o in decision.slot_operations],
            'reason_codes':decision.reason_codes,'historical_selector_executed':False,'canonical_grounding_executed':False,
            'production_orchestrator_executed':False,'public_clarification_observed':False}
    return out


def observe_v2_plan(result,case,*,catalog,mode,previous=None,history=()):
    """Extract a plan or clarification from RawTurnPlanner, scope/version checked.

    The caller must preserve the real model provenance. This adapter performs no
    generation and cannot upgrade scripted output to a live-model observation.
    """
    if mode not in MODES or mode=='RULE_SERVICE_OFFLINE':raise ValueError('V2_OBSERVATION_MODE_INVALID')
    out=_base(case,'V2_RAW_TURN_PLANNER',mode)
    context=result.next_state.context;scope=context.authorized_scope
    from tools.cutover.evaluation_contract import digest
    if catalog.get('artifact_hash')!=digest({k:v for k,v in catalog.items() if k!='artifact_hash'}):
        raise ValueError('V2_OBSERVATION_CATALOG_HASH_INVALID')
    actual_scope={'semantic_model_id':scope.semantic_model_id,'business_domain_ids':list(scope.business_domain_ids),'scope_mode':scope.scope_mode}
    if (actual_scope!=case['scope'] or context.catalog_pin.catalog_version!=catalog['catalog_version'] or case['catalog_ref']!=catalog['artifact_hash']
            or scope.database_id!=case.get('database_id') or list(scope.knowledge_base_names)!=case.get('knowledge_base_names',[])):
        raise ValueError('V2_OBSERVATION_SCOPE_OR_CATALOG_MISMATCH')
    from app.semantic_v2.authorized_contract import contract_digest, scoped_artifact_material
    if contract_digest(scoped_artifact_material(result.next_state.payload,result.next_state.source_value_bindings))!=result.next_state.payload_digest:
        raise ValueError('V2_OBSERVATION_STATE_DIGEST_INVALID')
    def task_id_of(value):
        return value.plan['logical_plan']['task_id'] if value.plan is not None else value.pending_state.payload['task_id']
    task_id=task_id_of(result)
    last=task_id_of(previous) if previous else None
    history_ids=[task_id_of(r) for r in history]
    target='PREVIOUS' if task_id==last else 'HISTORY:'+str(history_ids.index(task_id)) if task_id in history_ids else 'NEW'
    if result.plan is None:
        out['axes'].update(target_task=target,clarification_decision=result.trace.decision,pending_action='ASK',
            metric_clarification_required=result.trace.blocking_slot=='metrics')
        out['evidence']={'boundary':'RawTurnPlanner RecognizedClarification','reason_type':result.trace.reason_type,
            'blocking_slot':result.trace.blocking_slot,'already_asked':result.trace.already_asked,
            'is_user_ambiguity':result.trace.is_user_ambiguity,'system_repair_possible':result.trace.system_repair_possible,
            'safe_default_available':result.trace.safe_default_available,'full_pipeline_executed':False}
        return out
    task=result.next_state.payload['tasks'][task_id]
    state=next(v['semantics'] for v in task['versions'] if v['version']==task['active_version'])
    fact_ids={f['fact_id'] for f in catalog['facts']}
    def refs(slot,kind):
        values=state.get(slot,[]);canonical=[]
        for value in values:
            key=kind+':'+value['canonical_code']
            if key not in fact_ids:raise ValueError('V2_BINDING_NOT_IN_EVALUATION_CATALOG')
            canonical.append(key)
        return values,canonical
    metrics,metric_ids=refs('metrics','METRIC');dimensions,dimension_ids=refs('dimensions','DIMENSION')
    act=result.resolution['dialogue_act']
    patch=result.resolution['task_patch']
    edits=[op for key in ('adds','replacements','removes','clears') for op in patch.get(key,[])]
    out['axes'].update(metric_surfaces=[m['display_name'] for m in metrics],dimension_surfaces=[d['display_name'] for d in dimensions],
        canonical_metrics=metric_ids,canonical_dimensions=dimension_ids,target_task=target,
        turn_relation='NEW_TASK' if act=='NEW_TASK' else 'RETURN_TO_TOPIC' if act=='RETURN_TO_TOPIC' else 'ANSWER_PENDING' if act=='ANSWER_CLARIFICATION' else 'FOLLOW_UP',
        clarification_decision='NO_ASK',
        slot_operations=sorted({o['operation'] for o in edits}))
    for slot,axis in [('metrics','metric_operations'),('dimensions','dimension_operations')]:
        out['axes'][axis]=sorted({o['operation'] for o in edits if o['slot_path']==slot})
    if act=='ANSWER_CLARIFICATION':out['axes']['pending_action']='ANSWER'
    if previous is not None and previous.plan is None and act=='NEW_TASK':
        pending_id=previous.pending_state.payload['pending_id']
        record=result.next_state.payload['pending_records'].get(pending_id,{})
        out['axes']['pending_action']='DETACHED' if record.get('status')=='SUSPENDED' else 'UNKNOWN'
    out['evidence']={'boundary':'RawTurnPlanner typed next_state and bound canonical refs','backend_mode':result.plan['backend_contract']['mode'],
        'catalog_version':context.catalog_pin.catalog_version,'state_version':result.next_state.payload['state_version'],
        'full_pipeline_executed':False}
    return out
