"""Actual persisted results prove operation order, operands and source identity."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.domain.models import AtomicTask, PrimaryIntent
from app.planning.compound_question import CompoundJudgment, CompoundQuestionRouter
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner, TaskInputBindingError, materialize_computation_binding
from test_complex_execution_correctness import service, context, persist, IDENTITY


def monthly_mean():
    return {'type':'pipeline','operations':[
        {'type':'derive','operator':'divide','left_field':'金额','right_field':'单数','result_field':'月均单额'},
        {'type':'aggregate','group_by':[], 'aggregations':[{'field':'月均单额','function':'avg','alias':'各月均额平均'}]}]}


@pytest.mark.asyncio
async def test_query_declared_result_operation_is_planned_before_execution():
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    planner._model_decompose=AsyncMock(return_value=QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY,
        tasks=(DecomposedTask(question='先计算每月单均金额再平均',intent=PrimaryIntent.METRIC_QUERY,
            structured={'指标':[{'name':'金额'},{'name':'单数'}],'是否查询数据库':'是',
                        '结果操作':{'operation':monthly_mean()}}),)))
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    routed=await router.plan_with_judgment(CompoundJudgment(is_compound=False),'先计算每月单均金额再平均')
    assert routed.plan is not None
    assert len(routed.plan.tasks)==2
    query,compute=routed.plan.tasks
    assert '结果操作' not in query.extraction
    assert compute.extraction['是否查询数据库']=='否'
    assert compute.extraction['结果操作']['source_task_id']==query.task_id
    assert compute.depends_on==[query.task_id]


def test_single_query_self_result_index_becomes_a_real_predecessor():
    from app.planning.calculation_bindings import materialize_declared_result_operation
    value=QuestionDecomposition(tasks=(DecomposedTask(question='计算均额',structured={
        '是否查询数据库':'是','结果操作':{'source_task_index':0,'operation':monthly_mean()}}),))
    planned=materialize_declared_result_operation(value)
    assert len(planned.tasks)==2
    assert planned.tasks[1].depends_on==(0,)
    assert '结果操作' not in planned.tasks[0].structured


@pytest.mark.asyncio
async def test_model_can_preserve_a_tentative_operation_while_requesting_business_clarification(monkeypatch):
    import httpx,json
    from pydantic import SecretStr
    import app.domain.semantic_description as description
    monkeypatch.setattr(description,'load_semantic_description',AsyncMock(return_value={'source':'','content':'','available':False}))
    agent=service()
    settings=agent.settings.model_copy(update={'intent_model_enabled':True,'multi_question_model_enabled':True,
        'intent_model_api_key':SecretStr('test')})
    reply={'intent':'METRIC_QUERY','tasks':[{'question':'统计平均月度增长率','extraction':{
        '指标':[{'name':'数量'}],'是否查询数据库':'是'},'postprocess':{'operation':monthly_mean()},
        'clarification':{'question':'采用哪种算法？','candidates':['算术平均','复合平均'],'affected_slots':['analysis_method']}}]}
    async def respond(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(reply,ensure_ascii=False)}}]})
    planner=SingleQuestionPlanner(settings,transport=httpx.MockTransport(respond))
    result=await planner._model_decompose('统计平均月度增长率',semantic_model_id=777)
    assert result.tasks[0].structured['计算待确认']['candidates']==['算术平均','复合平均']
    assert '结果操作' not in result.tasks[0].structured


def test_operation_source_is_materialized_in_the_local_task_namespace():
    extraction={'是否查询数据库':'否','结果操作':{'source_task_index':0,'operation':monthly_mean()}}
    result=materialize_computation_binding(extraction,task_index=1,task_offset=3,dependencies=['task-4'])
    assert result['结果操作']['source_task_id']=='task-4'
    assert 'source_task_id' not in extraction['结果操作']
    with pytest.raises(TaskInputBindingError):
        materialize_computation_binding(extraction,task_index=1,task_offset=3,dependencies=[])


@pytest.mark.asyncio
async def test_monthly_ratio_then_mean_is_200_and_not_overall_ratio_250():
    agent=service();chat,_=context()
    source=await persist(agent,chat,['月份','金额','单数'],[
        {'月份':'2025-01','金额':100,'单数':1},{'月份':'2025-02','金额':900,'单数':3}],conversation='source')
    task=AtomicTask(task_id='mean',question='先计算每月单均金额再平均',depends_on=['source'],extraction={
        '是否查询数据库':'否','结果操作':{'source_task_id':'source','operation':monthly_mean()}})
    agent.adapters=replace(agent.adapters,semantic_query=AsyncMock(side_effect=AssertionError('local calculation must not call ASL')))
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    assert result.status=='COMPLETED'
    assert result.evidence[0].payload['preview']==[{'各月均额平均':200}]
    assert result.evidence[0].payload['operation']['input_binding']['dataset_id']==source.dataset_id


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_value',[0,None,True,'NaN'])
async def test_declared_pipeline_does_not_average_away_an_invalid_operand(bad_value):
    agent=service();chat,_=context()
    await persist(agent,chat,['金额','单数'],[{'金额':100,'单数':1},{'金额':900,'单数':bad_value}],conversation='source')
    task=AtomicTask(task_id='mean',question='先计算每月单均金额再平均',depends_on=['source'],extraction={
        '是否查询数据库':'否','结果操作':{'source_task_id':'source','operation':monthly_mean()}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    assert result.status=='FAILED'
    assert not result.dataset_id


@pytest.mark.asyncio
async def test_result_operation_cannot_select_a_later_unrelated_dataset():
    agent=service();chat,_=context()
    await persist(agent,chat,['金额','单数'],[{'金额':100,'单数':1}],conversation='source')
    task=AtomicTask(task_id='mean',question='先计算每月单均金额再平均',depends_on=['source'],extraction={
        '是否查询数据库':'否','结果操作':{'source_task_id':'source','dataset_id':'unrelated', 'operation':monthly_mean()}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    assert result.status=='FAILED'
    assert not result.dataset_id


@pytest.mark.asyncio
async def test_same_source_percentage_binds_each_rows_denominator():
    agent=service();chat,_=context()
    await persist(agent,chat,['分组','已覆盖数','总数'],[
        {'分组':'A','已覆盖数':5,'总数':100},{'分组':'B','已覆盖数':10,'总数':50}],conversation='source')
    task=AtomicTask(task_id='ratio',question='按组计算占比',depends_on=['source'],extraction={
        '是否查询数据库':'否','计算绑定':{'operation':'divide',
            'numerator':{'source_task_id':'source','column':'已覆盖数'},
            'denominator':{'source_task_id':'source','column':'总数'},
            'result_field':'占比','format':'percent'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    assert result.status=='COMPLETED'
    assert [row['占比'] for row in result.evidence[0].payload['preview']]==['5.00%','20.00%']
