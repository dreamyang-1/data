"""An unresolved calculation is a resumable business choice, not raw data success."""
from unittest.mock import AsyncMock

import pytest

from app.domain.models import PlannerExtraction,PrimaryIntent
from test_complex_execution_correctness import service,context,IDENTITY


@pytest.mark.asyncio
async def test_unresolved_calculation_stores_original_pending_before_any_asl_or_sql():
    agent=service();chat,_=context();chat.question='统计各组织近12月的平均月度续费增长率'
    chat._planner_extraction=PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY,structured={
        '指标':[{'name':'续费笔数'}],'计算待确认':{'question':'平均增长率采用哪种算法？1. 算术平均；2. 复合平均。',
        'candidates':['算术平均','复合平均'],'affected_slots':['analysis_method']}})
    agent._handle_surface_query=AsyncMock(side_effect=AssertionError('no ASL before clarification'))
    result=await agent._handle(chat,IDENTITY)
    assert result.status=='NEEDS_CLARIFICATION'
    pending=await agent.sessions.get_pending(IDENTITY.tenant_id,IDENTITY.user_id,chat.application_id,chat.conversation_id)
    assert pending.request.original_question==chat.question
    assert pending.request.semantic_ambiguities[0].candidates==['算术平均','复合平均']
    choice=agent._semantic_clarification_choice(pending.request,'2')
    resumed=agent._apply_semantic_clarification_choice(pending.request,pending.request.model_copy(deep=True),choice)
    assert resumed.original_question==chat.question
    assert '复合平均' in resumed.rewritten_question
    assert '近12月' in resumed.rewritten_question
    assert 'SEMANTIC_CHOICE_TARGET_UNRESOLVED' not in resumed.assumptions
    assert not resumed.semantic_ambiguities
    assert resumed.authorized_semantic_scope==pending.request.authorized_semantic_scope


def test_planner_sanitizer_keeps_the_unresolved_calculation_as_an_internal_contract():
    from app.planning.task_dag import SingleQuestionPlanner
    spec={'question':'请确认统计月份起止与平均算法','affected_slots':['analysis_method','analysis_window']}
    value=SingleQuestionPlanner._sanitize_extraction({'意图':'统计查询','指标':[{'name':'续费笔数'}],
        '计算待确认':spec},intent=PrimaryIntent.METRIC_QUERY)
    assert value['计算待确认']==spec


@pytest.mark.asyncio
async def test_a_new_calculation_question_is_not_hijacked_by_the_old_pending():
    agent=service();chat,_=context();chat.question='统计各组织近12月的平均月度续费增长率'
    chat._planner_extraction=PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY,structured={
        '计算待确认':{'question':'采用哪种平均算法？','candidates':['算术平均','复合平均'],
        'affected_slots':['analysis_method']}})
    await agent._handle(chat,IDENTITY)
    next_chat=chat.model_copy(deep=True,update={'question':'新问题，统计各经销商2025年的平均销售增长率','message_id':'next'})
    next_chat._planner_extraction=PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY,structured={
        '计算待确认':{'question':'新销售增长率采用哪种平均算法？','candidates':['算术平均','复合平均'],
        'affected_slots':['analysis_method']}})
    result=await agent._handle(next_chat,IDENTITY)
    pending=await agent.sessions.get_pending(IDENTITY.tenant_id,IDENTITY.user_id,chat.application_id,chat.conversation_id)
    assert result.status=='NEEDS_CLARIFICATION'
    assert pending.request.original_question==next_chat.question
