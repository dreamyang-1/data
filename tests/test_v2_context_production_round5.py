"""Production context contract tests; authored proposals, never model accuracy."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.semantic_v2.context_contract import ContextProposal, ContextAwareParse, proposal_schema
from app.semantic_v2.recognition import current_turn_schema
from app.semantic_v2.context_proposal import ContextProposalFailure, discover_context
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.authorized_contract import contract_digest
from test_v2_raw_turn_recognition import catalog, planner, metric_step, turns, request, IDENTITY, parse


def chosen(context, relation='CONTINUE', *, identifier=None):
    ctx=context['task_context']
    target=next((r for r in ctx['candidate_tasks'] if r['task_id']==(identifier or ctx['active_task_id'])),None)
    return proposal(relation=relation,state_version=ctx['state_version'],
        target_task_id=target['task_id'] if target else identifier,
        task_version=target['task_version'] if target else None)


def proposal(status='ACCEPTED', relation='NEW_TASK', **updates):
    data=dict(status=status, relation=relation, target_task_id=None, state_version=0,
        task_version=None, pending_id=None)
    data.update(updates)
    return data


@pytest.mark.parametrize('change',[
    {'state_version':True}, {'state_version':'1'},
    {'status':'AMBIGUOUS','relation':'RETURN_TO_TOPIC'},
    {'relation':'CONTINUE'}, {'target_task_id':'forged'},
    {'relation':'ANSWER_CLARIFICATION','target_task_id':'task:x','task_version':1},
])
def test_schema_rejects_inconsistent_proposals(change):
    with pytest.raises(ValidationError):ContextProposal(**proposal(**change))


@pytest.mark.parametrize('status',['AMBIGUOUS','UNRESOLVED'])
def test_schema_explicit_nonselection(status):
    assert ContextProposal(**proposal(status, None)).target_task_id is None


def test_schema_exports_round4_reject_constraint_and_requires_joint_proposal():
    schema=proposal_schema(dict(candidate_tasks=[],pending=None),current_turn_schema())
    assert 'context_proposal' in schema['required']
    assert schema['$defs']['ContextProposal']['allOf'][0]['then']['properties']['relation']=={'type':'null'}
    assert schema['$defs']['ContextProposal']['properties']['target_task_id']=={'type':'null'}
    assert schema['properties']['explicit_slot_mentions']['additionalProperties'] is False
    with pytest.raises(ValidationError):ContextAwareParse.model_validate({})


def test_schema_candidate_ids_are_bounded_enum():
    schema=proposal_schema(dict(candidate_tasks=[{'task_id':'task:a'}],pending={'pending_id':'p:a'}),current_turn_schema())
    props=schema['$defs']['ContextProposal']['properties']
    assert props['target_task_id']['anyOf'][0]['enum']==['task:a']
    assert props['pending_id']['anyOf'][0]['const']=='p:a'


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['AMBIGUOUS','UNRESOLVED'])
async def test_runtime_nonselection_is_terminal_before_draft_and_does_not_mutate(catalog,status):
    steps=[metric_step('销售额'),metric_step('订单笔数','订单笔数')]
    engine,_=planner(catalog,steps);bases=await turns(engine,steps)
    before=deepcopy(bases[-1].next_state.model_dump(mode='json'))
    last=('继续',parse('继续'),{});last[1]['context_proposal']=lambda c:proposal(status,None,state_version=c['task_context']['state_version'])
    engine,transport=planner(catalog,[last])
    with pytest.raises(ContextProposalFailure) as error:
        await engine.run(request(question=last[0],message_id='uncertain'),IDENTITY,
            state=bases[-1].next_state,plans=tuple(b.plan_state for b in bases))
    assert error.value.context_status==status
    assert error.value.decision.decision_type=='TERMINAL' and not error.value.decision.create_pending
    assert str(error.value.decision.reason_type)==('USER_AMBIGUITY' if status=='AMBIGUOUS' else 'SEMANTIC_RESOLUTION_FAILURE')
    assert error.value.context_trace['FINAL_TARGET'] is None
    assert len(error.value.context_trace['CONTEXT_CANDIDATES'])==2
    assert len(transport.calls)==1
    assert before==bases[-1].next_state.model_dump(mode='json')


@pytest.mark.asyncio
@pytest.mark.parametrize('soft',[{}, {'topic_shift_signals':['EXPLICIT_NEW_TASK']},
    {'dialogue_act_candidates':['NEW_TASK']}, {'reference_signals':['HISTORICAL']}])
async def test_joint_proposal_drives_runtime_without_soft_signal_veto(catalog,soft):
    first=metric_step('销售额');engine,_=planner(catalog,[first]);base=(await turns(engine,[first]))[0]
    last=metric_step('再加订单笔数','订单笔数','ADD',True)
    last[1].update({'reference_signals':[],'followup_signals':[],**soft})
    last[1]['context_proposal']=lambda c:chosen(c,'ADD')
    engine,transport=planner(catalog,[last])
    result=await engine.run(request(question=last[0],message_id='follow'),IDENTITY,state=base.next_state,plans=(base.plan_state,))
    assert result.plan['logical_plan']['task_id']==base.plan['logical_plan']['task_id']
    assert {x['canonical_code'] for x in result.plan['logical_plan']['payload']['measures']}=={'amount','orders'}
    assert result.resolution['dialogue_act']=='ADD' and result.context_trace['OVERRIDE_DIRECTION']=='NONE'
    assert len(transport.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,reason',[('target','CANDIDATE_MEMBERSHIP'),('version','TASK_VERSION_MATCH'),('state','STATE_VERSION_MATCH')])
async def test_runtime_hard_veto_is_traced_and_never_falls_back(catalog,fault,reason):
    first=metric_step('销售额');engine,_=planner(catalog,[first]);base=(await turns(engine,[first]))[0]
    last=metric_step('再加订单笔数','订单笔数','ADD',True)
    def bad(c):
        p=chosen(c)
        p.update({'target_task_id':'task:forged'} if fault=='target' else {'task_version':99} if fault=='version' else {'state_version':99})
        return p
    last[1]['context_proposal']=bad;engine,transport=planner(catalog,[last])
    with pytest.raises(ContextProposalFailure) as error:
        await engine.run(request(question=last[0],message_id='bad'),IDENTITY,state=base.next_state,plans=(base.plan_state,))
    assert reason in error.value.context_trace['OVERRIDE_REASON'] and len(transport.calls)==1


@pytest.mark.asyncio
async def test_discovery_cap_and_scope_before_recognition(catalog):
    steps=[metric_step('销售额') for _ in range(7)];engine,_=planner(catalog,steps);bases=await turns(engine,steps)
    session=ScopedPlanSession(request(message_id='next'),IDENTITY,catalog[0])
    discovered=discover_context(session,state=bases[-1].next_state,plans=tuple(b.plan_state for b in bases))
    assert len(discovered.model_context['candidate_tasks'])==4
    assert discovered.model_context['candidate_tasks'][0]['task_id']=='task:turn6'
    crossed=bases[-1].next_state
    scope=crossed.context.authorized_scope.model_copy(update={'semantic_model_id':82})
    crossed=crossed.model_copy(update={'context':crossed.context.model_copy(update={'authorized_scope':scope})})
    last=('继续',parse('继续'),{});engine,transport=planner(catalog,[last])
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        await engine.run(request(question=last[0],message_id='next'),IDENTITY,state=crossed)
    assert transport.calls==[]


@pytest.mark.asyncio
async def test_existing_but_unoffered_task_cannot_be_selected(catalog):
    steps=[metric_step('销售额') for _ in range(7)];engine,_=planner(catalog,steps);bases=await turns(engine,steps)
    last=('继续',parse('继续'),{})
    def omitted(c):
        offered={r['task_id'] for r in c['task_context']['candidate_tasks']}
        identifier=next(f'task:turn{i}' for i in range(7) if f'task:turn{i}' not in offered)
        return proposal(relation='RETURN_TO_TOPIC',target_task_id=identifier,task_version=1,
            state_version=c['task_context']['state_version'])
    last[1]['context_proposal']=omitted;engine,transport=planner(catalog,[last])
    with pytest.raises(ContextProposalFailure) as error:
        await engine.run(request(question=last[0],message_id='omitted'),IDENTITY,state=bases[-1].next_state,
            plans=tuple(b.plan_state for b in bases))
    assert 'CANDIDATE_MEMBERSHIP' in error.value.context_trace['OVERRIDE_REASON']
    assert len(transport.calls)==1


@pytest.mark.asyncio
async def test_state_changes_during_model_call_are_not_applied(catalog):
    first=metric_step('销售额');engine,_=planner(catalog,[first]);base=(await turns(engine,[first]))[0]
    state=deepcopy(base.next_state)
    last=metric_step('再加订单笔数','订单笔数','ADD',True)
    def stale(c):
        p=chosen(c,'ADD')
        raw=state.model_dump(mode='json')['payload'];raw['state_version']+=1
        # Deliberate fault injection bypasses the immutable artifact wrapper.
        # A different valid snapshot still must not match the old exposure.
        object.__setattr__(state,'payload',raw)
        object.__setattr__(state,'payload_digest',contract_digest(raw))
        return p
    last[1]['context_proposal']=stale;engine,transport=planner(catalog,[last])
    with pytest.raises(ContextProposalFailure) as error:
        await engine.run(request(question=last[0],message_id='stale'),IDENTITY,state=state,plans=(base.plan_state,))
    assert 'STATE_SNAPSHOT_UNCHANGED' in error.value.context_trace['OVERRIDE_REASON']
    assert 'STATE_VERSION_MATCH' in error.value.context_trace['OVERRIDE_REASON']
    assert len(transport.calls)==1


@pytest.mark.asyncio
async def test_pending_answer_proposal_can_override_old_new_topic_flag(pending_catalog):
    from test_v2_pending_recognition import ask
    pending,_=await ask(pending_catalog)
    last=('第一个',parse('第一个'),{})
    last[1]['topic_shift_signals']=['EXPLICIT_NEW_TASK']
    def answer(c):
        ctx=c['task_context'];p=ctx['pending']
        return proposal(relation='ANSWER_CLARIFICATION',state_version=ctx['state_version'],
            target_task_id=p['task_id'],task_version=p['task_version'],pending_id=p['pending_id'])
    last[1]['context_proposal']=answer;engine,transport=planner(pending_catalog,[last])
    result=await engine.run(request(question=last[0],message_id='answer'),IDENTITY,
        state=pending.next_state,pending=pending.pending_state)
    assert result.resolution['dialogue_act']=='ANSWER_CLARIFICATION'
    assert len(transport.calls)==1
    assert result.context_trace['FINAL_TARGET']==pending.pending_state.payload['task_id']


@pytest.mark.asyncio
async def test_pending_answer_without_resume_is_rejected_while_new_task_is_allowed(pending_catalog):
    from test_v2_pending_recognition import ask
    pending,_=await ask(pending_catalog)
    last=('第一个',parse('第一个',follow=True),{})
    engine,transport=planner(pending_catalog,[last])
    with pytest.raises(ContextProposalFailure) as error:
        await engine.run(request(question=last[0],message_id='answer'),IDENTITY,state=pending.next_state)
    assert 'PENDING_VERSION_MATCH' in error.value.context_trace['OVERRIDE_REASON']
    last=metric_step('销售额');last[1]['dialogue_act_candidates']=['NEW_TASK']
    engine,transport=planner(pending_catalog,[last])
    result=await engine.run(request(question=last[0],message_id='new'),IDENTITY,state=pending.next_state)
    assert result.resolution['dialogue_act']=='NEW_TASK'
    assert result.plan['logical_plan']['task_id']!=pending.pending_state.payload['task_id']
    assert len(transport.calls)==2


from test_v2_pending_recognition import catalog as pending_catalog


@pytest.mark.asyncio
async def test_registered_proposal_is_detached_and_immutable(catalog):
    from app.semantic_v2.context_proposal import accept_proposal,validated_session_trace
    first=metric_step('销售额');engine,_=planner(catalog,[first]);base=(await turns(engine,[first]))[0]
    session=ScopedPlanSession(request(message_id='next'),IDENTITY,catalog[0])
    discovered=discover_context(session,state=base.next_state,plans=(base.plan_state,))
    row=discovered.model_context['candidate_tasks'][0]
    p=ContextProposal(**proposal(relation='CONTINUE',target_task_id=row['task_id'],task_version=row['task_version'],
        state_version=discovered.model_context['state_version']))
    trace=accept_proposal(session,p,discovered,state=base.next_state)
    p.relation='NEW_TASK';discovered.model_context['candidate_tasks'].clear()
    stored,ctx,saved=session._context_proposal_proof
    assert stored.relation=='CONTINUE' and len(ctx.model_context['candidate_tasks'])==1
    with pytest.raises(TypeError):stored.relation='NEW_TASK'
    assert validated_session_trace(session,base.next_state)['FINAL_TARGET']==trace['FINAL_TARGET']
