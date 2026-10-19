"""Trusted known semantic versions constrain automatic reuse and recovery."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.models import AgentResponse, AtomicTask, TaskPlan
from app.planning import CompoundQuestionRouter
from test_complex_execution_correctness import service, context, IDENTITY


def version(chat,value):
    chat._semantic_decision = SimpleNamespace(scope_proof=SimpleNamespace(
        catalog_version=value,semantic_model_version=value))


def response(chat):
    return AgentResponse(request_id=uuid4(),conversation_id=chat.conversation_id,
        status='COMPLETED',intent='METRIC_QUERY',answer='合成已验证结果')


@pytest.mark.asyncio
async def test_automatic_repeat_response_does_not_cross_a_known_catalog_change():
    agent=service();chat,_=context();chat.question='查询2025年销售额';version(chat,'old')
    agent._handle=AsyncMock(return_value=response(chat))
    await agent.handle(chat,IDENTITY)
    changed=chat.model_copy(deep=True,update={'message_id':'second-message'});version(changed,'new')
    await agent.handle(changed,IDENTITY)
    assert agent._handle.await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize('stored_version',['old','new'])
async def test_checkpoint_reuse_requires_matching_known_catalog_version(stored_version):
    agent=service();chat,_=context();version(chat,'new')
    agent.task_planner=CompoundQuestionRouter(agent.settings)
    plan=TaskPlan(planner='DETERMINISTIC_RULE',tasks=[
        AtomicTask(task_id='a',question='查询甲项'),AtomicTask(task_id='b',question='查询乙项')])
    agent._handle=AsyncMock(return_value=response(chat))
    await agent.sessions.put_dag_checkpoint(IDENTITY.tenant_id,IDENTITY.user_id,chat.application_id,
        chat.conversation_id,chat.message_id,{'authorized_scope':chat.authorized_semantic_scope.fingerprint(),
        'plan_fingerprint':hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
        'semantic_versions':{'catalog_version':stored_version,'semantic_model_version':stored_version},
        'completed':{'a':response(chat).model_dump(mode='json')},'conversations':{'a':chat.conversation_id}})
    await agent._handle_task_plan(chat,IDENTITY,plan)
    assert agent._handle.await_count==(1 if stored_version=='new' else 2)


@pytest.mark.asyncio
async def test_resume_preserves_pending_when_catalog_changed_instead_of_running_old_bindings():
    agent=service();chat,_=context();version(chat,'new')
    plan=TaskPlan(planner='DETERMINISTIC_RULE',tasks=[
        AtomicTask(task_id='a',question='查询甲项'),AtomicTask(task_id='b',question='查询乙项')])
    state={'resume_token_hash':hashlib.sha256(b'token').hexdigest(),'root_message_id':'original',
        'task_plan':plan.model_dump(mode='json'),'awaiting_task_ids':['b'],'state_version':1,
        'scope_fingerprint':agent._dag_scope_fingerprint(chat),
        'semantic_versions':{'catalog_version':'old','semantic_model_version':'old'}}
    await agent.sessions.put_dag_pending(IDENTITY.tenant_id,IDENTITY.user_id,chat.application_id,
        chat.conversation_id,state,expected_version=0)
    agent._handle_task_plan=AsyncMock(return_value=response(chat))
    result=await agent._resume_task_plan(chat,IDENTITY,state)
    assert result.status!='COMPLETED' and '版本' in result.answer
    agent._handle_task_plan.assert_not_called()
    assert await agent.sessions.get_dag_pending(IDENTITY.tenant_id,IDENTITY.user_id,
        chat.application_id,chat.conversation_id) is not None


def test_refresh_requests_are_not_automatic_repeat_cache_candidates():
    agent=service();chat,_=context();chat.question='查询最新数据并刷新结果'
    assert not agent._is_repeat_cache_candidate(chat)
