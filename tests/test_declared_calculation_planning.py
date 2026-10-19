"""Derived requests require executable operands, not prose after raw metrics."""
from unittest.mock import AsyncMock

import pytest

from app.domain.models import AtomicTask, PrimaryIntent
from app.planning.compound_question import CompoundJudgment, CompoundQuestionRouter
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner
from test_complex_execution_correctness import service, context, persist, IDENTITY


def raw_extraction():
    return {'意图':'统计查询','指标':[{'name':'含税销售总额'},{'name':'订单笔数'}],
            '是否查询数据库':'是','输出要求':'计算每单均额=含税销售总额/订单笔数'}


@pytest.mark.asyncio
async def test_unregistered_division_is_planned_as_query_then_bound_computation():
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    planner._model_decompose=AsyncMock(return_value=QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY,
        tasks=(DecomposedTask(question='统计每单均额',intent=PrimaryIntent.METRIC_QUERY,structured=raw_extraction()),)))
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    routed=await router.plan_with_judgment(CompoundJudgment(is_compound=False),'统计每单均额')
    assert routed.plan is not None
    assert len(routed.plan.tasks)==2
    compute=routed.plan.tasks[-1]
    assert compute.depends_on==[routed.plan.tasks[0].task_id]
    assert compute.extraction['计算绑定']['numerator']['column']=='含税销售总额'
    assert compute.extraction['计算绑定']['denominator']['column']=='订单笔数'


@pytest.mark.asyncio
async def test_declared_row_division_uses_fields_with_an_unseen_output_label():
    agent=service();chat,_=context()
    await persist(agent,chat,['业务员编码','含税销售总额','订单笔数'],[
        {'业务员编码':'A','含税销售总额':1000,'订单笔数':4},
        {'业务员编码':'B','含税销售总额':300,'订单笔数':2}],conversation='source')
    task=AtomicTask(task_id='result',question='计算每单均额',depends_on=['source'],extraction={
        '是否查询数据库':'否','计算绑定':{'operation':'divide',
            'numerator':{'source_task_id':'source','column':'含税销售总额'},
            'denominator':{'source_task_id':'source','column':'订单笔数'},
            'result_field':'每单均额','format':'number'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    assert result.status=='COMPLETED'
    assert {row['业务员编码']:row['每单均额'] for row in result.evidence[0].payload['preview']}=={'A':250,'B':150}


@pytest.mark.asyncio
@pytest.mark.parametrize('question,data_request', [
    ('最近哪几家合作组织的订阅增长最快？',True),
    ('写一首关于订阅增长的诗',False),
])
async def test_unknown_binding_does_not_turn_catalog_data_request_into_greeting(question,data_request):
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    outside=QuestionDecomposition(intent=PrimaryIntent.OUT_OF_SCOPE,
        tasks=(DecomposedTask(question=question,intent=PrimaryIntent.OUT_OF_SCOPE),),
        metric_catalog=(frozenset({'续费笔数'}),{}))
    planner._model_decompose=AsyncMock(return_value=outside)
    result=await planner.plan_question(question,semantic_context='| 实体 | 同义词 |\n| --- | --- |\n| 订阅合同 | 订阅 |')
    assert (result.intent==PrimaryIntent.METRIC_QUERY) is data_request
    assert result.tasks[0].question==question
    assert result.tasks[0].structured is None  # no invented replacement object/field
