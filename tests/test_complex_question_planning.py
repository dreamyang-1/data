"""Question indices and local task indices are different namespaces."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.planning.compound_question import (
    CompoundJudgment, CompoundJudgmentEntry, CompoundTaskPlanner, rule_split_plan,
)
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, TaskPlanningError


async def assemble(sizes, dependencies=None, *, chains=True):
    labels = ['甲','乙','丙','丁','戊']
    entries = tuple(CompoundJudgmentEntry(question=f'查询{labels[i]}企业信息',
        depends_on=tuple((dependencies or {}).get(i, ()))) for i in range(len(sizes)))
    decompositions = [QuestionDecomposition(tasks=tuple(DecomposedTask(
        question=entry.question, depends_on=(j-1,) if chains and j else ())
        for j in range(size))) for entry, size in zip(entries, sizes)]
    planner = CompoundTaskPlanner(Settings(_env_file=None, env='test'),
        SimpleNamespace(plan_question=AsyncMock(side_effect=decompositions)))
    return await planner.plan(CompoundJudgment(is_compound=True, entries=entries),
                              source_question='；'.join(entry.question for entry in entries))


@pytest.mark.asyncio
async def test_question_dependency_uses_previous_group_deliverable():
    plan = await assemble([2, 1], {1:[0]})
    assert plan.tasks[2].depends_on == ['task-2']
    assert plan.question_groups[1].task_offset == 2


@pytest.mark.asyncio
async def test_multitask_group_preserves_external_and_local_dependencies():
    plan = await assemble([2, 2], {1:[0]})
    assert plan.tasks[2].depends_on == ['task-2']
    assert plan.tasks[3].depends_on == ['task-3']


@pytest.mark.asyncio
async def test_multiple_upstream_groups_keep_each_terminal_result():
    plan = await assemble([2, 2, 1], {2:[0,1]}, chains=False)
    assert plan.tasks[4].depends_on == ['task-1','task-2','task-3','task-4']


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', [-1, 1, 4])
async def test_unknown_self_or_future_question_dependency_is_a_planning_error(reference):
    with pytest.raises(TaskPlanningError):
        await assemble([2, 1], {1:[reference]})


@pytest.mark.asyncio
async def test_twelve_tasks_retains_every_question():
    plan = await assemble([4,4,4])
    assert len(plan.tasks) == 12 and len(plan.question_groups) == 3


@pytest.mark.asyncio
async def test_over_budget_is_a_typed_error_before_model_construction():
    with pytest.raises(TaskPlanningError, match='12|上限'):
        await assemble([4,4,4,4])


def test_independent_question_does_not_inherit_another_local_year_or_metric():
    plan = rule_split_plan('统计2025年月度订单笔数；查询上海地区各经销商区域医院覆盖率', max_tasks=12)
    assert plan is not None
    assert '2025' not in plan.tasks[1].question
    assert '指标为订单' not in plan.tasks[1].question
    assert '上海' not in plan.tasks[0].question


def test_explicit_common_prefix_can_still_be_inherited():
    plan = rule_split_plan('2025年；统计各月订单笔数；查询销售额', max_tasks=12)
    assert plan is not None
    assert all('2025' in task.question for task in plan.tasks)


@pytest.mark.parametrize('question', [
    '查询甲企业；查询乙企业；查询丙企业',
    '查询本月销售额，再计算最高值，再计算最低值',
    '生成2025年上海销售分析报告，包含趋势、排名、异常',
])
def test_rule_fallbacks_do_not_silently_truncate(question):
    from app.planning.task_dag import TaskBudgetExceeded
    with pytest.raises(TaskBudgetExceeded):
        rule_split_plan(question, max_tasks=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['budget', 'binding'])
async def test_public_handler_does_not_fall_back_to_query_after_budget_error(kind):
    from app.planning.task_dag import TaskBudgetExceeded, TaskInputBindingError
    from test_complex_execution_correctness import service, context, IDENTITY
    agent = service(); chat, _ = context()
    failure = TaskBudgetExceeded('本轮需要16项任务，超过12项上限，请分批提交') if kind == 'budget' else TaskInputBindingError('来源未登记为任务依赖')
    agent.task_planner = SimpleNamespace(plan=AsyncMock(side_effect=failure))
    agent._handle = AsyncMock(side_effect=AssertionError('预算超限不得执行单查询兜底'))
    result = await agent.handle(chat, IDENTITY)
    assert result.status != 'COMPLETED' and str(failure) in result.answer
    agent._handle.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_recognized_plan_does_not_become_a_single_query():
    from app.domain.models import AtomicTask,TaskPlan
    from app.planning.compound_question import validate_plan
    from test_complex_execution_correctness import service,context,IDENTITY
    agent=service();chat,_=context();chat.question='查询2025年甲数据；查询2025年乙数据'
    async def plan(*args,**kwargs):
        candidate=TaskPlan(planner='STRUCTURED_MODEL',tasks=[
            AtomicTask(task_id='a',question='查询2026年甲数据'),
            AtomicTask(task_id='b',question='查询2025年乙数据')])
        validate_plan(candidate,source_question=chat.question)
        return candidate
    agent.task_planner=SimpleNamespace(plan=plan)
    agent._handle=AsyncMock(side_effect=AssertionError('invalid full plan cannot become a query'))
    response=await agent.handle(chat,IDENTITY)
    assert response.status=='SAFE_FALLBACK'
    agent._handle.assert_not_called()


def test_calculation_binding_survives_sanitizing_and_maps_local_sources():
    from app.domain.models import PrimaryIntent
    from app.planning.task_dag import SingleQuestionPlanner, materialize_computation_binding
    raw = {'是否查询数据库':'否', '计算绑定': {'operation':'divide',
        'numerator':{'source_task_index':0,'column':'医院数'},
        'denominator':{'source_task_index':1,'column':'总数'},
        'result_field':'覆盖率','format':'percent'}}
    structured = SingleQuestionPlanner._sanitize_extraction(raw, intent=PrimaryIntent.METRIC_QUERY)
    bound = materialize_computation_binding(structured, task_index=2, task_offset=4,
        dependencies=['task-5','task-6'])
    assert bound['计算绑定']['numerator'] == {'source_task_id':'task-5','column':'医院数'}
    assert bound['计算绑定']['denominator']['source_task_id'] == 'task-6'
    assert raw['计算绑定']['numerator']['source_task_index'] == 0
    with pytest.raises(TaskPlanningError):
        materialize_computation_binding(structured, task_index=2, task_offset=4, dependencies=['task-5'])


@pytest.mark.asyncio
async def test_model_judgment_and_decomposition_use_separate_dependency_namespaces():
    import httpx
    import json
    from pydantic import SecretStr
    from app.planning.compound_question import CompoundQuestionRouter
    async def handler(request):
        messages = json.loads(request.content)['messages']
        question = messages[-1]['content']
        if '问题拆分判断器' in messages[0]['content']:
            payload = {'split':True,'sub_questions':[
                {'question':'查询甲企业信息','intent':'明细查询','depends_on':[]},
                {'question':'根据上述结果查询乙企业信息','intent':'明细查询','depends_on':[0]}]}
        else:
            extraction = {'意图':'明细查询','展示字段':[{'field':'名称'}]}
            payload = {'intent':'DETAIL_QUERY','tasks':[
                {'question':question,'depends_on':[], 'extraction':extraction},
                {'question':question,'depends_on':[0], 'extraction':extraction}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    router = CompoundQuestionRouter(Settings(_env_file=None, env='test', intent_model_api_key=SecretStr('test'),
        multi_question_model_enabled=True), transport=httpx.MockTransport(handler))
    routed = await router.route_and_plan('查询甲企业信息；根据上述结果查询乙企业信息')
    assert routed.judgment.entries[1].depends_on == (0,)
    assert [task.depends_on for task in routed.plan.tasks] == [[],['task-1'],['task-2'],['task-3']]
