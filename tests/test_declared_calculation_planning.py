"""Derived requests require executable operands, not prose after raw metrics."""
from unittest.mock import AsyncMock

import pytest

from app.domain.models import AtomicTask, PrimaryIntent
from app.planning.compound_question import CompoundJudgment, CompoundQuestionRouter
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner, TaskInputBindingError
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
    ('最近订阅数增速靠前的是哪些合作组织？',True),
    ('最近订阅数增速最高的前三家合作组织是谁？',True),
    ('续费笔数最多的是哪家合作组织？',True),
    ('订阅数的口径是什么？',False),
    ('写一首关于订阅增长的诗',False),
])
async def test_unknown_binding_does_not_turn_catalog_data_request_into_greeting(question,data_request):
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    outside=QuestionDecomposition(intent=PrimaryIntent.OUT_OF_SCOPE,
        tasks=(DecomposedTask(question=question,intent=PrimaryIntent.OUT_OF_SCOPE),),
        metric_catalog=(frozenset({'续费笔数'}),{'订阅数':'续费笔数'}))
    planner._model_decompose=AsyncMock(return_value=outside)
    if data_request:
        with pytest.raises(TaskInputBindingError,match=question):
            await planner.plan_question(question,semantic_context='| 实体 | 同义词 |\n| --- | --- |\n| 订阅合同 | 订阅 |')
        assert planner._model_decompose.await_count==2
    else:
        result=await planner.plan_question(question,semantic_context='| 实体 | 同义词 |\n| --- | --- |\n| 订阅合同 | 订阅 |')
        assert result.intent==PrimaryIntent.OUT_OF_SCOPE
        assert result.tasks[0].question==question


@pytest.mark.asyncio
async def test_cross_entity_details_cannot_hide_a_required_metric_ranking_in_prose():
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    question='找出续费金额最高的组织，列出它的合同'
    invalid=QuestionDecomposition(intent=PrimaryIntent.DETAIL_QUERY,
        metric_catalog=(frozenset({'续费金额'}),{}),tasks=(DecomposedTask(question=question,
            intent=PrimaryIntent.DETAIL_QUERY,structured={'意图':'明细查询','指标':[],
            '展示字段':[{'entity':'组织','field':'组织名称'},{'entity':'合同','field':'合同名称'}],
            '排序':[{'field':'金额','order':'desc'}], '输出要求':'先取第一名组织，再列出其合同'}),))
    valid=QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY,tasks=(
        DecomposedTask(question='统计各组织续费金额并降序取第一名',intent=PrimaryIntent.METRIC_QUERY,
            structured={'指标':[{'name':'续费金额'}],'维度':['组织'],
                '排序':[{'field':'续费金额','order':'desc'}],'限制':1}),
        DecomposedTask(question='根据上述组织查询合同',depends_on=(0,),intent=PrimaryIntent.DETAIL_QUERY,
            structured={'展示字段':[{'entity':'合同','field':'合同名称'}],'是否查询数据库':'是'})))
    planner._model_decompose=AsyncMock(side_effect=[invalid,valid])
    result=await planner.plan_question(question)
    assert len(result.tasks)==2
    assert planner._model_decompose.await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize('repair',['valid','invalid','transport_failure'])
async def test_rejected_complete_plan_gets_one_correction_before_any_execution(repair):
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    def proposed(year):
        return QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY,tasks=(
            DecomposedTask(question=f'统计{year}年各机构的续费金额',intent=PrimaryIntent.METRIC_QUERY,
                structured={'指标':[{'name':'续费金额'}],'是否查询数据库':'是'}),
            DecomposedTask(question=f'查询上述机构{year}年合同',depends_on=(0,),intent=PrimaryIntent.DETAIL_QUERY,
                structured={'展示字段':[{'entity':'合同','field':'合同名称'}],'是否查询数据库':'是'})))
    correction=proposed(2025) if repair=='valid' else proposed(2024) if repair=='invalid' else RuntimeError('transport unavailable')
    planner._model_decompose=AsyncMock(side_effect=[proposed(2024),correction])
    question='统计2025年各机构续费金额，再查询其2025年合同'
    if repair=='valid':
        result=await planner.plan_question(question)
        assert all('2025' in task.question for task in result.tasks)
    else:
        with pytest.raises(TaskInputBindingError):
            await planner.plan_question(question)
    assert planner._model_decompose.await_count==2
    assert '不存在' in planner._model_decompose.call_args.kwargs['retry_feedback']


@pytest.mark.asyncio
@pytest.mark.parametrize('owner,needs_query',[('合同',True),('组织',False)])
async def test_local_detail_projection_requires_its_input_owner(owner,needs_query):
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    def proposed(query):
        return QuestionDecomposition(intent=PrimaryIntent.DETAIL_QUERY,tasks=(
            DecomposedTask(question='查询组织名称',intent=PrimaryIntent.DETAIL_QUERY,
                structured={'展示字段':[{'entity':'组织','field':'组织名称'}],'是否查询数据库':'是'}),
            DecomposedTask(question=f'查询上述组织的{owner}名称',depends_on=(0,),intent=PrimaryIntent.DETAIL_QUERY,
                structured={'展示字段':[{'entity':owner,'field':owner+'名称'}],'是否查询数据库':'是' if query else '否'})))
    planner._model_decompose=AsyncMock(side_effect=[proposed(False),proposed(True)])
    result=await planner.plan_question('查询组织及其合同名称' if needs_query else '查询组织并筛选组织名称')
    assert planner._model_decompose.await_count==(2 if needs_query else 1)
    assert result.tasks[1].structured['是否查询数据库']==('是' if needs_query else '否')
