"""Private, opt-in observations of existing runtime calls; no decision overrides."""
from __future__ import annotations

from contextlib import ExitStack
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from unittest.mock import patch

from pydantic import BaseModel
from tools.cutover.evaluation_contract import digest

ACTIVE = ContextVar('evaluation_runtime_observer', default=None)
ARTIFACTS = ('AuthorizedSemanticScope','CurrentTurnParseInput','PreviousTask',
             'RecentTasks','HistoricalTasks','PendingState','DatasetReference','CatalogSnapshot',
             'CandidateSet','CurrentClock','TurnResolutionInput','SemanticResolutionInput',
             'TaskPatchInput','TaskSemanticState','SemanticQueryIR','DryPlanInput','LogicalPlan')


def json_value(value):
    if isinstance(value, BaseModel):return value.model_dump(mode='json')
    if isinstance(value, dict):return {str(k):json_value(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):return [json_value(v) for v in value]
    if value is None or type(value) in (bool,str,int,float):return value
    return {'runtime_type':type(value).__name__}  # Never introspect credentials or clients.


class RuntimeObserver:
    def __init__(self):
        self.turns=[];self.active=None;self.stack=ExitStack()

    def begin_turn(self, case_id, index):
        self.active={'case_id':case_id,'turn_index':index,'events':[]}
        self.turns.append(self.active)

    def record(self, stage, inputs, output=None, error=None):
        if self.active is not None:
            self.active['events'].append({'stage':stage,'inputs':deepcopy(json_value(inputs)),
                'output':deepcopy(json_value(output)),'error_type':type(error).__name__ if error else None})

    def __enter__(self):
        from app.semantic_v2 import recognition as runtime
        from app.semantic_v2.catalog_bridge import ScopedPlanSession
        from app.semantic_v2.pipeline import TurnResolver, CurrentTurnParser
        from tools.cutover.run_raw_transition_benchmark import ModelRecorder
        self.token=ACTIVE.set(self)
        original_init=ScopedPlanSession.__init__
        @wraps(original_init)
        def session_init(session,*args,**kwargs):
            original_init(session,*args,**kwargs)
            active=ACTIVE.get()
            if active is not None:
                active.record('AuthorizedSemanticScope',{},session.context.authorized_scope)
                active.record('CatalogSnapshot',{},session.context.catalog_pin)
        self.stack.enter_context(patch.object(ScopedPlanSession,'__init__',session_init))

        def observe(obj,name,stage,*,static=False,select=None):
            original=getattr(obj,name)
            @wraps(original)
            def wrapped(*args,**kwargs):
                selected=select(args,kwargs) if select else kwargs
                try:
                    result=original(*args,**kwargs)
                except Exception as exc:
                    active=ACTIVE.get()
                    if active is not None:active.record(stage,selected,error=exc)
                    raise
                active=ACTIVE.get()
                if active is not None:active.record(stage,selected,result)
                return result
            self.stack.enter_context(patch.object(obj,name,staticmethod(wrapped) if static else wrapped))

        observe(TurnResolver,'resolve','TurnResolutionInput',static=True,
                select=lambda a,k:{'parse':a[0],**k})
        # Versioned Round 5 runtime seam: observe the actual validated proposal,
        # without replacing it with the previous signal-derived resolver.
        if hasattr(runtime, 'proposal_resolution'):
            observe(runtime,'proposal_resolution','TurnResolutionInput',
                select=lambda a,k:{'context_trace':a[0],'parse':a[1],'state':a[2],
                    'task_patch':a[3],'semantic_resolution':a[4]})
        observe(CurrentTurnParser,'parse','CurrentTurnParseInput',static=True)
        observe(runtime.RawTurnPlanner,'_candidates','CandidateSet',static=True,
                select=lambda a,k:{'parse':a[1]})
        original_context=runtime.RawTurnPlanner._task_context
        @classmethod
        def task_context(cls,*args,**kwargs):
            result=original_context(*args,**kwargs)
            active=ACTIVE.get()
            if active is not None:active.record('HistoricalTasks',{'parse':args[0]},result)
            return result
        self.stack.enter_context(patch.object(runtime.RawTurnPlanner,'_task_context',task_context))
        observe(runtime.RawTurnPlanner,'_patch','TaskPatchInput',static=True,
                select=lambda a,k:{'parse':a[1],'draft':a[2],'base':a[4],**k})
        observe(runtime,'apply_task_patch','TaskSemanticState',
                select=lambda a,k:{'previous':a[0],'patch':a[1],**k})
        observe(runtime,'materialize_payload','SemanticQueryIR',
                select=lambda a,k:{'payload_type':a[0],'state':a[1]})
        observe(ScopedPlanSession,'resolve_turn','SemanticResolutionInput',
                select=lambda a,k:k)
        observe(ScopedPlanSession,'_compile_logical','LogicalPlan',
                select=lambda a,k:{**k,'authorized_scope':a[0].context.authorized_scope})
        observe(ScopedPlanSession,'bind','CanonicalBinding',
                select=lambda a,k:{'candidate_id':a[1],'role':a[2],
                    'authorized_scope':a[0].context.authorized_scope})
        original=ModelRecorder.complete
        @wraps(original)
        async def complete(recorder,**kwargs):
            active=ACTIVE.get()
            if active is not None:
                active.record('ModelInput',{'stage':kwargs['stage'],'context':kwargs['context']})
                active.record('CurrentClock',{},kwargs['context'].get('clock'))
            return await original(recorder,**kwargs)
        self.stack.enter_context(patch.object(ModelRecorder,'complete',complete))
        return self

    def __exit__(self,*exc):
        self.stack.close();ACTIVE.reset(self.token)


def successful_axes(result, *, before, prior_results, catalog):
    """Read accepted contracts. Never fill observations from expected labels."""
    from app.semantic_v2.pipeline import collect_bound_refs
    from app.semantic_v2.authorized_contract import AuthorizedSemanticScope
    axes={};scopes={};plan=result.get('plan');state=result['next_state']
    current_scope=state['context']['authorized_scope']
    fp=AuthorizedSemanticScope.model_validate(current_scope).fingerprint()
    scopes.update(current=fp,stored_state=fp)
    if before.get('state'):
        scopes['previous_state']=AuthorizedSemanticScope.model_validate(before['state']['context']['authorized_scope']).fingerprint()
    if before.get('pending'):
        scopes['pending']=AuthorizedSemanticScope.model_validate(before['pending']['context']['authorized_scope']).fingerprint()
    parse=result.get('parse',{})
    axes['scope']=current_scope
    axes['mentions']=[{'surface':v['surface'],'start':v['start_char'],'end':v['end_char'],
        'roles':v['candidate_roles']} for v in parse.get('mentions',[])]
    axes['roles']=[{'mention_id':v['mention_id'],'roles':v['candidate_roles']}
                  for v in parse.get('mentions',[])]
    if plan is None:
        axes.update(clarification_decision=result['trace']['decision'],pending_action='ASK',
                    metric_clarification_required=result['trace']['blocking_slot']=='metrics')
        return axes,scopes
    logical=plan['logical_plan'];task_id=logical['task_id'];resolution=result['resolution']
    tasks=state['payload']['tasks'];task=tasks[task_id]
    semantic=next(v['semantics'] for v in task['versions'] if v['version']==task['active_version'])
    previous_ids=[]
    for prior in prior_results:
        previous_ids.append(prior['plan']['logical_plan']['task_id'] if prior['plan'] else prior['pending_state']['payload']['task_id'])
    target=('PREVIOUS' if previous_ids and task_id==previous_ids[-1] else
            'HISTORY:'+str(previous_ids.index(task_id)) if task_id in previous_ids else 'NEW')
    act=resolution['dialogue_act'];payload=logical['payload']
    from app.semantic_v2.models import PlanPayload
    from pydantic import TypeAdapter
    bound=[v.model_dump(mode='json') for v in collect_bound_refs(TypeAdapter(PlanPayload).validate_python(payload))]
    for binding in bound:
        # Catalog IDs must be authorized by the actual plan's permission proofs;
        # scope is not inferred from a display name or copied Gold fact.
        if binding['catalog_version']!=catalog['catalog_version']:
            raise ValueError('HARNESS_BINDING_VERSION_MISMATCH')
    scopes['binding_context']=AuthorizedSemanticScope.model_validate(logical['permission_requirement']['authorized_scope']).fingerprint()
    from app.semantic_v2.registries import PayloadContractRegistry
    axes.update(target_task=target,dialogue_act=act,
        turn_relation='NEW_TASK' if act=='NEW_TASK' else 'RETURN_TO_TOPIC' if act=='RETURN_TO_TOPIC' else 'ANSWER_PENDING' if act=='ANSWER_CLARIFICATION' else 'FOLLOW_UP',
        bindings=bound,metric_surfaces=[r['display_name'] for r in semantic['metrics']],
        dimension_surfaces=[r['display_name'] for r in semantic['dimensions']],
        canonical_metrics=['METRIC:'+r['canonical_code'] for r in semantic['metrics']],
        canonical_dimensions=['DIMENSION:'+r['canonical_code'] for r in semantic['dimensions']],
        entity=semantic.get('subject'),time=semantic.get('time_spec'),filters=semantic.get('filter_expression'),
        task_patch=resolution['task_patch'],task_state=semantic,
        query_shape=str(PayloadContractRegistry.get(payload['payload_type']).resolved_query_shape),
        semantic_query_ir=payload,clarification_decision='NO_ASK',metric_clarification_required=False)
    edits=[v for k in ('sets','adds','replacements','removes','clears') for v in resolution['task_patch'].get(k,[])]
    for slot,axis in [('metrics','metric_operations'),('dimensions','dimension_operations')]:
        axes[axis]=sorted({v['operation'] for v in edits if v['slot_path']==slot})
    operations=sorted({v['operation'] for v in edits if v['operation'] in {'ADD','REPLACE','REMOVE','CLEAR'}})
    axes['slot_operations']=operations
    if len(operations)==1:axes['operation']=operations[0]
    if act=='ANSWER_CLARIFICATION':axes['pending_action']='ANSWER'
    if before.get('pending'):
        pending_id=before['pending']['payload']['pending_id']
        record=state['payload']['pending_records'].get(pending_id)
        if record and record['status']=='SUSPENDED':axes['pending_action']='DETACHED'
    # Region is a reviewed evaluation-axis mapping to the frozen catalog's
    # province/city entity attributes. It neither chooses nor rewrites a field.
    region_codes={f['code'] for f in catalog['facts'] if f['catalog_type']=='ATTRIBUTE'
                  and f.get('owner') in {'province','city'}}
    def predicates(value):
        if not value:return []
        if value['node_type'] in {'PREDICATE','ALIASED_PREDICATE'}:return [value]
        if value.get('operator')=='NOT':raise ValueError('NEGATED_REGION_HAS_NO_POSITIVE_VALUE_LIST')
        return [p for child in value['children'] for p in predicates(child)]
    def values(value):
        if value['value_type']=='LIST':return [x for child in value['values'] for x in values(child)]
        if value['value_type']=='ENTITY_REF':return [value['ref']['display_name']]
        if value['value_type']=='STRING':return [value['value']]
        raise ValueError('REGION_VALUE_AXIS_NOT_A_NAME_LIST')
    try:
        selected=[p for p in predicates(semantic.get('filter_expression')) if p['field_ref']['canonical_code'] in region_codes]
        if all(p['operator'] in {'EQ','IN'} for p in selected):
            axes['region_values']=sorted({v for p in selected for v in values(p['value'])})
        old_fields=[]
        if before.get('state'):
            old_tasks=before['state']['payload']['tasks']
            if task_id in old_tasks:
                old_task=old_tasks[task_id]
                old_semantic=next(v['semantics'] for v in old_task['versions'] if v['version']==old_task['active_version'])
                old_fields=predicates(old_semantic.get('filter_expression'))
        region_edits=[]
        for edit in edits:
            if edit['slot_path']!='filter_expression':continue
            touched=old_fields if edit['operation'] in {'CLEAR','REMOVE'} else predicates(edit.get('new_value'))
            if any(p['field_ref']['canonical_code'] in region_codes for p in touched):region_edits.append(edit['operation'])
        axes['region_operations']=sorted(set(region_edits))
    except (ValueError,KeyError):
        # Unsupported projection stays absent; full typed filters remain observed.
        pass
    return axes,scopes


def public_observation(private):
    """Only identifiers, hashes and bounded diagnostic types leave private storage."""
    return {'case_id':private['case_id'],'turn_index':private['turn_index'],
            'artifact_hash':digest(private),
            'stages':[{'stage':e['stage'],'input_hash':digest(e['inputs']),
                       'output_hash':digest(e['output']),'error_type':e['error_type']}
                      for e in private['events']]}
