"""Recognition owns intent; extraction and task execution only carry it."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.models import AtomicTask, ChatRequest, PlannerExtraction, PrimaryIntent as I, TaskPlan, TrustedIdentity, QuestionTaskGroup
from app.planning.compat import inherit_recognized_intent, plan_question
from app.planning.task_dag import _PRIMARY_INTENT_LABELS, extraction_parser_prompt
from test_task_planning_stages import setup_planner
from test_workflow import make_orchestrator


PARAMETERS = {'意图': '指标查询', '实体': ['经销商'], '指标': [], '维度': [],
    '展示字段': [{'entity': '经销商', 'field': '经销商名称'}],
    '过滤条件': [{'entity': '经销商', 'field': '合作时长', 'op': '>', 'value': ['3个月']}],
    '排序': [], '时间粒度': None, '限制': None, '是否查询数据库': '是'}


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', [I.DETAIL_QUERY, I.METRIC_QUERY, I.COMPARISON_ANALYSIS,
    I.TREND_ANALYSIS, I.COMPOSITION_ANALYSIS, I.ANOMALY_ANALYSIS, I.ROOT_CAUSE_ANALYSIS, I.FORECAST_ANALYSIS])
async def test_conflicting_extraction_cannot_change_recognized_intent(monkeypatch, intent):
    values = copy.deepcopy(PARAMETERS)
    planner, calls = setup_planner(monkeypatch,
        {'intent': 'METRIC_QUERY', 'tasks': [{'question': '合作时长大于3个月的经销商有哪些'}]},
        extract=lambda request: {'intent': 'METRIC_QUERY', 'extraction': values})
    result = await planner.plan_question('合作时长大于3个月的经销商有哪些',
        semantic_model_id=130, root_intent=intent)
    assert result.intent is intent and result.tasks[0].intent is intent
    assert result.tasks[0].structured['意图'] == _PRIMARY_INTENT_LABELS[intent]
    assert result.tasks[0].structured['过滤条件'] == PARAMETERS['过滤条件']
    assert result.tasks[0].structured['展示字段'] == PARAMETERS['展示字段']
    assert result.tasks[0].structured['指标'] == []
    assert len(calls) == 2
    assert intent.value in calls[1]['messages'][0]['content']
    assert values == PARAMETERS


def test_extraction_prompt_does_not_request_intent_classification():
    from app.planning.task_plan_stages import PARAMETER_PROMPT
    text = extraction_parser_prompt()
    assert '判断用户分析目的' not in text
    assert '意图必须输出' not in text
    assert '"意图":' not in text
    assert '不判断、不修改、不输出意图或intent字段' in PARAMETER_PROMPT
    assert '保持已识别意图不变' in text


@pytest.mark.asyncio
async def test_extractor_can_omit_intent_entirely(monkeypatch):
    values = {k:v for k,v in PARAMETERS.items() if k != '意图'}
    planner, _ = setup_planner(monkeypatch, {'tasks': [{'question': '查询经销商名称'}]},
        extract=lambda request: {'extraction': values})
    result = await planner.plan_question('查询经销商名称', root_intent=I.DETAIL_QUERY)
    assert result.tasks[0].intent is I.DETAIL_QUERY
    assert result.tasks[0].structured['意图'] == '明细查询'


@pytest.mark.asyncio
async def test_legacy_planner_adapter_and_local_task_keep_recognition_intent():
    operation = {'source_task_index': 0, 'operation': {'type': 'limit', 'count': 2}}
    plan = TaskPlan(planner='DETERMINISTIC_RULE', tasks=[
        AtomicTask(task_id='task-1', question='查询各经销商订单笔数', primary_intent=I.METRIC_QUERY,
            extraction={'意图':'指标查询','是否查询数据库':'是'}),
        AtomicTask(task_id='task-2', question='根据前序结果取前两行，不再查询数据库',
            depends_on=['task-1'], primary_intent=I.COMPARISON_ANALYSIS,
            extraction={'意图':'指标计算','是否查询数据库':'否','结果操作':operation})])
    class LegacyPlanner:
        async def plan(self, question):
            return SimpleNamespace(plan=plan, single_extraction=None, single_task_name=None, single_task_question=None)
    result = await plan_question(LegacyPlanner(), '查询经销商', semantic_context='',
        semantic_model_id=130, root_intent=I.DETAIL_QUERY)
    assert all(t.primary_intent is I.DETAIL_QUERY and t.extraction['意图']=='明细查询' for t in result.plan.tasks)
    assert plan.tasks[1].extraction['结果操作'] == operation
    assert plan.tasks[1].extraction['是否查询数据库'] == '否'


@pytest.mark.asyncio
async def test_executor_does_not_launch_second_intent_verdict(monkeypatch):
    import app.services.orchestrator as module
    agent = make_orchestrator()
    identity = TrustedIdentity(tenant_id='test', user_id='test')
    start = Mock(side_effect=AssertionError('intent was already recognized'))
    finish = AsyncMock(side_effect=AssertionError('intent must not be replaced'))
    monkeypatch.setattr(module, 'start_intent_sidecar', start)
    monkeypatch.setattr(module, 'finish_intent_sidecar', finish)
    result = await agent._classify('查询经销商', identity, 'intent-authority', pre_resolved=True,
        planner_extraction=PlannerExtraction(intent=I.DETAIL_QUERY, structured=copy.deepcopy(PARAMETERS)))
    assert result.primary_intent is I.DETAIL_QUERY
    assert result.intent_source == 'INTENT_RECOGNITION'
    start.assert_not_called()
    finish.assert_not_awaited()


def test_fast_execution_entry_uses_recognition_verdict():
    agent = make_orchestrator()
    request = agent._classify_with_rules('统计订单金额', TrustedIdentity(tenant_id='t',user_id='u'), 'c')
    assert request.primary_intent is not I.DETAIL_QUERY
    extraction = PlannerExtraction(intent=I.DETAIL_QUERY, structured={'是否去重':'是'})
    agent._apply_explicit_projection_mode(request, extraction)
    assert request.primary_intent is I.DETAIL_QUERY


def test_analysis_binding_cannot_replace_recognized_task_intent():
    agent = make_orchestrator()
    chat = ChatRequest(question='比较各月订单数量', conversation_id='c', semantic_model_id=130,
        message_id='m', application_id='app')
    task = AtomicTask(task_id='task-1', question=chat.question, primary_intent=I.COMPARISON_ANALYSIS)
    request = agent._analysis_task_request(chat, TrustedIdentity(tenant_id='t',user_id='u'), task,
        {'intent':'TREND_ANALYSIS', 'analysis_type':'trend'})
    assert request.primary_intent is I.COMPARISON_ANALYSIS


def test_calculation_failure_retains_recognized_intent():
    agent = make_orchestrator()
    chat = ChatRequest(question='根据前序结果计算均值', conversation_id='c', semantic_model_id=130,
        message_id='m', application_id='app')
    chat._planner_extraction = PlannerExtraction(intent=I.DETAIL_QUERY)
    assert agent._pure_computation_unavailable(chat).intent is I.DETAIL_QUERY


@pytest.mark.asyncio
async def test_recognition_node_verdict_wins_over_rule_fallback_and_parameter_intent():
    from app.planning.compound_question import CompoundJudgment
    judgment = CompoundJudgment(is_compound=False, intent='明细查询')
    class Router:
        async def route_and_plan(self, question, **kwargs):
            raise AssertionError('must reuse the recognized judgment')
        async def plan_with_judgment(self, prior, question, root_intent=None, **kwargs):
            assert root_intent is I.DETAIL_QUERY
            return SimpleNamespace(plan=None, judgment=prior, single=SimpleNamespace(
                single_extraction=PlannerExtraction(intent=I.FORECAST_ANALYSIS, structured=copy.deepcopy(PARAMETERS)),
                single_task_name=None,single_task_question=question))
    result = await plan_question(Router(), '查询合作时长大于3个月的经销商',
        semantic_context='', semantic_model_id=130, prior_judgment=judgment, root_intent=I.METRIC_QUERY)
    assert result.single_extraction.intent is I.DETAIL_QUERY
    assert result.single_extraction.structured['意图']=='明细查询'


def test_compound_tasks_inherit_their_own_recognized_question_intent():
    from app.planning.compound_question import CompoundJudgment, CompoundJudgmentEntry
    judgment=CompoundJudgment(is_compound=True,entries=(
        CompoundJudgmentEntry(question='查询经销商名单',intent='明细查询'),
        CompoundJudgmentEntry(question='分析销售额趋势',intent='趋势分析')))
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[
        AtomicTask(task_id=f'task-{i+1}',question='查询任务结果',primary_intent=I.METRIC_QUERY,
            extraction={'意图':'指标查询','是否查询数据库':'是'}) for i in range(3)],
        question_groups=[QuestionTaskGroup(question='查询经销商名单',task_offset=0,task_count=1),
                         QuestionTaskGroup(question='分析销售额趋势',task_offset=1,task_count=2)])
    inherit_recognized_intent(I.METRIC_QUERY,plan=plan,judgment=judgment)
    assert [t.primary_intent for t in plan.tasks]==[I.DETAIL_QUERY,I.TREND_ANALYSIS,I.TREND_ANALYSIS]
    assert [t.extraction['意图'] for t in plan.tasks]==['明细查询','趋势分析','趋势分析']
