"""Diagnostic counterfactuals; observed deficiencies are not repaired here."""
from copy import deepcopy
import pytest
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.pipeline import CurrentTurnParser, CurrentTurnSemanticParse
from app.semantic_v2.state_machine import ConversationState
from tools.cutover.context_round3 import model_evidence, resolver_arm, referential_slot_oracle
from test_v2_raw_turn_recognition import catalog, planner, metric_step, request, IDENTITY, turns, parse
from test_v2_context_followup_critical_slice import context_catalog,initial
from test_v2_pending_recognition import catalog as pending_catalog,ask


@pytest.mark.asyncio
@pytest.mark.parametrize('act',['CONTINUE','MODIFY','CORRECT','ADD','REPLACE'])
async def test_act_only_ablation_exposes_unconsumed_model_signal_without_changing_production(catalog,act):
    first=metric_step('销售额');next_step=metric_step('继续增加订单笔数','订单笔数','ADD',True)
    parsed=next_step[1];parsed['reference_signals']=[];parsed['followup_signals']=[]
    parsed['dialogue_act_candidates']=[act]
    engine,_=planner(catalog,[first,next_step]);base=(await turns(engine,[first]))[0]
    before=deepcopy(base.next_state.model_dump(mode='json'))
    kwargs=dict(state=base.next_state,plans=(base.plan_state,))
    with pytest.raises(RecognitionFailure,match='V2_PRIOR_PAYLOAD_IDENTITY_REQUIRED'):
        await engine.run(request(question=next_step[0],message_id='turn1'),IDENTITY,**kwargs)
    assert base.next_state.model_dump(mode='json')==before
    # Round 3's monkeypatch observes the legacy resolver seam. Round 5 Raw
    # runtime consumes a joint proposal directly; do not pretend this old arm
    # still intercepts production. Keep its original causal assertion here.
    from app.semantic_v2.pipeline import TurnResolver
    from app.semantic_v2.slot_reducer import TaskPatch
    from app.semantic_v2.models import SemanticResolutionContract
    surface=deepcopy(parsed)
    for mention in surface['mentions']:mention['source_turn_id']='turn1'
    p=CurrentTurnParser.parse(text=next_step[0],turn_id='turn1',text_ref='turn1',
        parsed=CurrentTurnSemanticParse.model_validate(surface))
    with resolver_arm('A_MODEL_HARD') as decisions:
        result=TurnResolver.resolve(p,state=ConversationState.model_validate(base.next_state.payload),
            task_patch=TaskPatch(base_task_version=1),semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'))
    assert result.target_task_id==base.plan['logical_plan']['task_id']
    assert decisions[0]['CURRENT_HYBRID_TARGET_TASK']!=decisions[0]['MODEL_TARGET_TASK']
    assert base.next_state.model_dump(mode='json')==before
    # The patch is scoped to this context manager; production defaults persist.
    again,_=planner(catalog,[next_step])
    with pytest.raises(RecognitionFailure,match='V2_PRIOR_PAYLOAD_IDENTITY_REQUIRED'):
        await again.run(request(question=next_step[0],message_id='turn1'),IDENTITY,**kwargs)


@pytest.mark.asyncio
async def test_historical_exposure_is_gated_before_context_aware_model(catalog):
    from app.semantic_v2.recognition import RawTurnPlanner,semantic_task_schema
    from app.semantic_v2.authorized_contract import contract_digest
    steps=[metric_step('销售额'),metric_step('销售数量','销售数量'),metric_step('订单笔数','订单笔数')]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    state=ConversationState.model_validate(results[-1].next_state.payload)
    tasks={'task:'+contract_digest({'task':t.task_id})[:24]:t for t in state.tasks.values()}
    plans={r.plan_state.payload['task_id']:__import__('app.semantic_v2.pipeline',fromlist=['AuthorizedLogicalPlan']).AuthorizedLogicalPlan.model_validate(r.plan_state.payload) for r in results}
    for refs,count in [([],0),(['ELLIPSIS'],1),(['HISTORICAL'],3)]:
        p=CurrentTurnParser.parse(text='input',turn_id='turn3',text_ref='turn3',
            parsed=CurrentTurnSemanticParse(reference_signals=refs))
        labels=RawTurnPlanner._task_context(p,state,tasks,plans)
        assert len(labels)==count
        schema=semantic_task_schema(p,tasks)['properties']['historical_task_handle']
        if refs!=['HISTORICAL']:
            assert schema['type']=='null' and all('task_handle' not in item for item in labels)
        else:assert all('task_handle' in item for item in labels)


@pytest.mark.parametrize('variant',['empty','missing_target','conflict'])
def test_model_arm_abstains_without_inventing_target(variant):
    state=ConversationState(conversation_id='c',tenant_id='t',user_id='u',application_id='app',state_version=0)
    p=CurrentTurnSemanticParse(dialogue_act_candidates=[] if variant=='empty' else ['RETURN_TO_TOPIC'] if variant=='missing_target' else ['NEW_TASK','CONTINUE'])
    parsed=CurrentTurnParser.parse(text='input',turn_id='turn',text_ref='turn',parsed=p)
    result=model_evidence(parsed,state,'not-offered')
    assert result.relation=='UNRESOLVED' and result.target is None
    assert result.reason=={'empty':'NO_MODEL_RELATION_PROPOSITION','missing_target':'TARGET_NOT_OFFERED_OR_MISSING','conflict':'CONFLICTING_MODEL_PROPOSITIONS'}[variant]


def test_reference_oracle_cannot_erase_current_explicit_operation():
    outputs=[{'stage':'v2_current_turn','output':{'operation_markers':[{'mention_id':'m','slot_name':'metrics','operation_hint':'ADD'}],
        'explicit_slot_mentions':{'metrics':['m']}}},{'stage':'v2_semantic_edits','output':{'edits':[]}}]
    before=deepcopy(outputs)
    with pytest.raises(ValueError,match='CANNOT_DROP_EXPLICIT_OPERATION'):referential_slot_oracle(outputs,mention_id='m')
    assert outputs==before


def test_reference_oracle_changes_only_reviewed_declaration_in_one_artifact():
    outputs=[{'stage':'v2_current_turn','output':{'operation_markers':[],
        'explicit_slot_mentions':{'metrics':['reference','new']},'reference_signals':['HISTORICAL']}},
        {'stage':'v2_semantic_edits','output':{'edits':[{'evidence_mention_ids':['new']}]}}]
    before=deepcopy(outputs);changed=referential_slot_oracle(outputs,mention_id='reference')
    assert outputs==before and changed[1:]==outputs[1:]
    assert changed[0]['output']['explicit_slot_mentions']=={'metrics':['new']}
    assert changed[0]['output']['reference_signals']==outputs[0]['output']['reference_signals']


@pytest.mark.asyncio
async def test_model_arm_new_task_does_not_inherit(catalog):
    first=metric_step('销售额');last=metric_step('订单笔数','订单笔数')
    last[1]['dialogue_act_candidates']=['NEW_TASK']
    engine,_=planner(catalog,[first]);base=(await turns(engine,[first]))[0]
    engine,_=planner(catalog,[last])
    with resolver_arm('A_MODEL_HARD'):
        result=await engine.run(request(question=last[0],message_id='turn1'),IDENTITY,state=base.next_state,plans=(base.plan_state,))
    assert result.plan['logical_plan']['task_id']!=base.plan['logical_plan']['task_id']
    assert [m['canonical_code'] for m in result.plan['logical_plan']['payload']['measures']]==['orders']


@pytest.mark.asyncio
async def test_complete_metric_and_time_still_follow_model_reference(context_catalog):
    from test_v2_raw_turn_recognition import binding,edit
    first=initial('去年上海销售额')
    text='继续上一任务，统计今年销售额'
    p=parse(text,[('今年','TIME_FIELD','time_spec','REPLACE'),('销售额','MEASURE','metrics','SET')],follow=True)
    def draft(c):return dict(payload_type='INHERIT',edits=[edit('metrics',[binding(c,'销售额','MEASURE','m1')],ids=('m1',))],
        temporal_edits=[dict(component='RANGE',operation='REPLACE',evidence_mention_ids=['m0'],
            value=dict(start='2026-01-01T00:00:00+08:00',end_exclusive='2027-01-01T00:00:00+08:00'))])
    engine,_=planner(context_catalog,[first,(text,p,draft)]);result=await turns(engine,[first,(text,p,draft)])
    final=result[-1].plan['logical_plan']
    assert final['task_id']==result[0].plan['logical_plan']['task_id']
    assert final['payload']['filters']['value']['value']=='上海'
    assert final['payload']['time']['range']['start'].startswith('2025-12-31T16:00:00')


@pytest.mark.asyncio
async def test_complete_new_metric_time_request_keeps_old_region_out(context_catalog):
    steps=[initial('去年上海销售额'),initial('去年销售额',regions=())]
    steps[-1][1]['dialogue_act_candidates']=['NEW_TASK']
    engine,_=planner(context_catalog,steps);result=await turns(engine,steps)
    assert result[-1].plan['logical_plan']['task_id']!=result[0].plan['logical_plan']['task_id']
    assert result[-1].plan['logical_plan']['payload']['filters'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('shift',[False,True])
async def test_pending_new_act_conflict_is_visible_and_parse_only_oracle_is_bounded(pending_catalog,shift):
    pending,_=await ask(pending_catalog);step=metric_step('销售额')
    step[1]['dialogue_act_candidates']=['NEW_TASK']
    if shift:step[1]['topic_shift_signals']=['EXPLICIT_NEW_TASK']
    before=pending.next_state.model_dump(mode='json');engine,calls=planner(pending_catalog,[step])
    result=await engine.run(request(question=step[0],message_id='answer'),IDENTITY,
        state=pending.next_state,pending=pending.pending_state)
    assert result.resolution['dialogue_act']=='NEW_TASK'
    assert result.plan['logical_plan']['task_id']!=pending.pending_state.payload['task_id']
    assert len(calls.calls)==2
    assert pending.next_state.model_dump(mode='json')==before
    # Authored joint NEW_TASK is independent of old shift flags. Exact option
    # matching cannot veto it. This is a contract control, not model accuracy.


@pytest.mark.asyncio
async def test_context_with_missing_active_task_fails_closed(catalog):
    step=metric_step('继续销售额','销售额','ADD',True)
    step[1]['dialogue_act_candidates']=['CONTINUE']
    engine,_=planner(catalog,[step])
    with pytest.raises(RecognitionFailure,match='V2_CONTEXT_UNRESOLVED'):
        await engine.run(request(question=step[0],message_id='orphan'),IDENTITY)
