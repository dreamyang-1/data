"""Diagnostic contract controls; no live models or production routing changes."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from pydantic import ValidationError

from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from app.semantic_v2.authorized_contract import contract_digest
from tools.cutover.context_proposal_schema import ContextProposal, ContextAwareParse, proposal_schema
from tools.cutover.context_proposal_round4 import discover_context, validate_proposal, propose
from test_v2_raw_turn_recognition import catalog, planner, metric_step, request, IDENTITY, turns
from test_v2_pending_recognition import catalog as pending_catalog, ask


def proposed(context, relation='CONTINUE', task=None, **updates):
    target = task or context.model_context['candidate_tasks'][0]
    data = dict(status='ACCEPTED', relation=relation, target_task_id=target['task_id'],
        state_version=context.model_context['state_version'], task_version=target['task_version'], pending_id=None)
    if relation == 'NEW_TASK':data.update(target_task_id=None, task_version=None)
    data.update(updates)
    return ContextProposal(**data)


async def setup(catalog, count=1):
    steps=[metric_step('销售额') for _ in range(count)]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    session=ScopedPlanSession(request(message_id='next'),IDENTITY,catalog[0])
    state=results[-1].next_state;plans=tuple(r.plan_state for r in results)
    return session,state,plans,discover_context(session,state=state,plans=plans)


@pytest.mark.asyncio
async def test_candidate_discovery_is_bounded_and_relation_independent(catalog):
    session,state,plans,context=await setup(catalog,7)
    rows=context.model_context['candidate_tasks']
    assert len(rows)==4 and rows[0]['task_id']=='task:turn6'
    assert context.model_context['omitted_task_count']==3
    assert len({r['task_id'] for r in rows})==4
    assert all(r['candidate_scope']['business_domain_ids']==[205] for r in rows)
    # No parse/relation parameter can suppress history before model invocation.
    import inspect
    assert set(inspect.signature(discover_context).parameters)=={'session','state','plans','pending'}
    assert all(k not in context.model_context for k in ('history','question','conversation_history'))


@pytest.mark.asyncio
async def test_legal_model_followup_is_not_vetoed_by_current_surface_or_missing_slots(catalog):
    session,state,_,context=await setup(catalog)
    for text in ('嗯','今年','查询新的销售额', ''):
        trace=validate_proposal(proposed(context),context,session=session,state=state,question=text)
        assert trace['FINAL_STATUS']=='ACCEPTED' and trace['FINAL_TARGET']=='task:turn0'
        assert trace['SOFT_EVIDENCE']==[] and trace['OVERRIDE_DIRECTION']=='NONE'


@pytest.mark.asyncio
@pytest.mark.parametrize('change,reason',[
    ({'target_task_id':'task:forged'},'CANDIDATE_MEMBERSHIP'),
    ({'task_version':999},'TASK_VERSION_MATCH'),
    ({'state_version':999},'STATE_VERSION_MATCH'),
    ({'relation':'RETURN_TO_TOPIC'},'HISTORICAL_TARGET_IS_NON_ACTIVE')])
async def test_hard_veto_reasons_are_observable(catalog,change,reason):
    session,state,_,context=await setup(catalog)
    trace=validate_proposal(proposed(context,**change),context,session=session,state=state)
    assert trace['FINAL_STATUS']=='REJECTED' and reason in trace['OVERRIDE_REASON']
    assert trace['FINAL_TARGET'] is None


@pytest.mark.asyncio
async def test_candidate_context_tamper_and_scope_change_fail_closed(catalog):
    session,state,_,context=await setup(catalog)
    bad=replace(context,authority_fingerprint='different-current-scope')
    assert 'CURRENT_SCOPE_MATCH' in validate_proposal(proposed(context),bad,session=session,state=state)['OVERRIDE_REASON']
    changed=deepcopy(context.model_context);changed['candidate_tasks'][0]['task_version']=2
    bad=replace(context,model_context=changed)
    assert 'CANDIDATE_SNAPSHOT_UNCHANGED' in validate_proposal(proposed(context),bad,session=session,state=state)['OVERRIDE_REASON']


@pytest.mark.asyncio
async def test_native_cross_scope_restore_rejects_before_model_exposure(catalog):
    session,state,_,context=await setup(catalog)
    scope=state.context.authorized_scope.model_copy(update={'semantic_model_id':82})
    crossed=state.model_copy(update={'context':state.context.model_copy(update={'authorized_scope':scope})})
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        discover_context(session,state=crossed)


@pytest.mark.asyncio
async def test_historical_target_must_be_offered_and_non_active(catalog):
    session,state,_,context=await setup(catalog,3)
    historical=next(t for t in context.model_context['candidate_tasks'] if t['task_id']=='task:turn0')
    p=proposed(context,'RETURN_TO_TOPIC',historical)
    assert validate_proposal(p,context,session=session,state=state)['FINAL_TARGET']=='task:turn0'
    trace=validate_proposal(proposed(context,'CONTINUE',historical),context,session=session,state=state)
    assert trace['OVERRIDE_REASON']==['CURRENT_RELATION_TARGET_IS_ACTIVE']


@pytest.mark.parametrize('updates',[
    {'relation':'NEW_TASK','target_task_id':'task:old','task_version':1},
    {'relation':'CONTINUE','target_task_id':None,'task_version':None},
    {'status':'AMBIGUOUS','target_task_id':'task:old'},
    {'state_version':True}, {'state_version':'1'}, {'task_version':True},
    {'relation':'ANSWER_CLARIFICATION','pending_id':None},
    {'relation':'CONTINUE','pending_id':'pending:x'}])
def test_schema_rejects_inconsistent_or_non_strict_proposals(updates):
    values=dict(status='ACCEPTED',relation='CONTINUE',target_task_id='task:x',state_version=1,task_version=1,pending_id=None)
    values.update(updates)
    with pytest.raises(ValidationError):ContextProposal(**values)


def test_ambiguity_has_no_target_and_no_made_up_relation_enum():
    p=ContextProposal(status='AMBIGUOUS',relation=None,target_task_id=None,state_version=0,task_version=None,pending_id=None)
    assert p.target_task_id is None
    from app.semantic_v2.enums import DialogueAct
    from tools.cutover.context_proposal_schema import RELATIONS
    assert set(RELATIONS)<=set(DialogueAct)


@pytest.mark.asyncio
async def test_pending_context_and_answer_are_version_bound(pending_catalog):
    result,_=await ask(pending_catalog)
    session=ScopedPlanSession(request(message_id='answer'),IDENTITY,pending_catalog[0])
    context=discover_context(session,state=result.next_state,pending=result.pending_state)
    p=proposed(context,'ANSWER_CLARIFICATION',pending_id=context.model_context['pending']['pending_id'])
    trace=validate_proposal(p,context,session=session,state=result.next_state,question='第一个')
    assert trace['FINAL_STATUS']=='ACCEPTED'
    trace=validate_proposal(p,context,session=session,state=result.next_state,question='江苏有哪些医院')
    assert trace['FINAL_STATUS']=='REJECTED' and 'PENDING_ANSWER_ADMISSIBLE' in trace['OVERRIDE_REASON']
    assert validate_proposal(proposed(context,'NEW_TASK'),context,session=session,state=result.next_state,
        question='江苏有哪些医院')['FINAL_STATUS']=='ACCEPTED'
    with pytest.raises(ValueError,match='PENDING_RECEIPT_REQUIRED'):discover_context(session,state=result.next_state)
    bad=p.model_copy(update={'pending_id':'not-the-pending'})
    assert 'PENDING_VERSION_MATCH' in validate_proposal(bad,context,session=session,state=result.next_state,question='第一个')['OVERRIDE_REASON']


@pytest.mark.asyncio
async def test_schema_reuses_parse_and_one_call_preserves_current_explicit_evidence(catalog):
    session,state,_,context=await setup(catalog)
    source=metric_step('再加订单笔数','订单笔数','ADD',True)[1]
    original=deepcopy(source)
    class Model:
        calls=0
        async def complete(self,**kw):
            self.calls+=1
            assert kw['stage']=='v2_current_turn'
            assert kw['context']['task_context']['candidate_count']==1
            schema=kw['schema'];assert schema['properties']['explicit_slot_mentions']['additionalProperties'] is False
            return kw['output_model'].model_validate({**source,'context_proposal':proposed(context,'ADD').model_dump()})
    client=Model()
    parsed,proposal=await propose(client,question='再加订单笔数',turn_id='next',clock='2026-09-09T09:00:00+08:00',discovered=context)
    assert client.calls==1 and source==original
    assert parsed.model_dump()==CurrentTurnSemanticParse.model_validate(source).model_dump()
    assert 'context_proposal' not in parsed.model_dump()
    schema=proposal_schema(context.model_context)
    assert schema['$defs']['ContextProposal']['properties']['target_task_id']['anyOf'][0]['enum']==['task:turn0']


@pytest.mark.asyncio
async def test_model_unavailable_does_not_default_to_old_task(catalog):
    session,state,_,context=await setup(catalog)
    before=contract_digest(state.model_dump(mode='json'))
    class Unavailable:
        async def complete(self,**kw):raise TimeoutError('controlled')
    with pytest.raises(TimeoutError):
        await propose(Unavailable(),question='继续',turn_id='next',clock='2026-09-09T09:00:00+08:00',discovered=context)
    assert before==contract_digest(state.model_dump(mode='json'))


def test_json_schema_exposes_status_target_consistency_without_new_semantic_rules():
    schema=proposal_schema({'candidate_tasks':[],'pending':None})
    constraints=schema['$defs']['ContextProposal']['allOf']
    assert constraints[0]['then']['properties']=={key:{'type':'null'} for key in
        ('relation','target_task_id','task_version','pending_id')}
    assert set(constraints[0]['if']['properties']['status']['enum'])=={'UNRESOLVED','AMBIGUOUS'}


def test_signal_compatibility_projection_cannot_modify_explicit_current_evidence():
    from tools.cutover.context_proposal_native_diagnostic import relation_signal_adapter
    source=CurrentTurnSemanticParse.model_validate(metric_step('再加订单笔数','订单笔数','ADD',True)[1]).model_dump(mode='json')
    before=deepcopy(source)
    for act in ('NEW_TASK','MODIFY','RETURN_TO_TOPIC','ANSWER_CLARIFICATION'):
        projected=relation_signal_adapter(source,{'FINAL_STATUS':'ACCEPTED','FINAL_RELATION':act}).model_dump(mode='json')
        changed={k for k in source if source[k]!=projected[k]}
        assert changed<={'dialogue_act_candidates','reference_signals','followup_signals','topic_shift_signals'}
        assert projected['mentions']==source['mentions'] and projected['operation_markers']==source['operation_markers']
    assert source==before
    with pytest.raises(ValueError,match='NOT_ACCEPTED'):
        relation_signal_adapter(source,{'FINAL_STATUS':'REJECTED'})


@pytest.mark.parametrize('error',['SCHEMA_INVALID','MODEL_TIMEOUT'])
def test_null_target_after_model_failure_is_not_correct_ambiguity(error):
    from tools.cutover.summarize_context_proposal_round4 import metrics
    arms={arm:{'relation':'UNRESOLVED','target':None,'error':error} for arm in
        ('A_CURRENT_HYBRID','B_MODEL_PROPOSAL_PLUS_HARD_CONSTRAINTS','C_RULE_ONLY_DIAGNOSTIC')}
    result=metrics([{'expected_relation':'UNRESOLVED','expected_target':None,'arms':arms}])
    for observed in result.values():
        assert observed['valid_decision_outputs']==0
        assert observed['schema_or_runtime_rejected']==1
        assert observed['relation_semantic_match']=={'n':0,'N':0}
