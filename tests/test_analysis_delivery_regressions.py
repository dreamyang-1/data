"""Synthetic reproductions of the three frontend reports; no remote services."""
import asyncio
import copy
import hashlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.analysis import AnalysisEngine
from app.domain.models import AgentResponse, AtomicTask, EvidenceItem, PlannerExtraction, PrimaryIntent, TaskPlan
from app.intent.classifier import render_execution_question
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.planning.compound_question import CompoundJudgment, CompoundQuestionRouter
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner
from app.presentation.reliability import _evidence_line
from app.services.calculation_execution import _steps
from app.services.dag_recovery import recover_task_plan
from app.services.insight_delivery import deliver_root_insight
from app.services.progress import emit_progress, progress_scope
from test_complex_execution_correctness import service, context, persist, IDENTITY
from test_task_planning_stages import setup_planner

METRIC = '含税销售总额'
TODAY = date(2026, 10, 21)
CASES = [
    ('分析2025年10月到12月的含税销售总额趋势，计算各月环比，识别拐点和连续涨跌区间，并展示折线图',
     PrimaryIntent.TREND_ANALYSIS, '趋势分析', '2025年10月到12月', ['业务日期']),
    ('对比2025年11月和12月的含税销售总额，给出差额和变化率',
     PrimaryIntent.COMPARISON_ANALYSIS, '对比分析', '2025年11月和2025年12月', ['业务日期']),
    ('分析2025年10月至12月各渠道业态的含税销售总额占比，按占比降序展示',
     PrimaryIntent.COMPOSITION_ANALYSIS, '占比分析', '2025年10月至12月', ['渠道业态']),
]


def extraction(label, period, dimensions):
    return {'意图': label, '指标': [{'name': METRIC}], '实体': ['销售订单'],
        '维度': dimensions, '过滤条件': [], '时间粒度': {'unit': '月', 'time_range': period, 'anchor': '业务日期'},
        '排序': [{'field': '业务日期', 'order': 'asc'}] if dimensions == ['业务日期'] else [],
        '是否查询数据库': '是'}


@pytest.mark.asyncio
@pytest.mark.parametrize('question,intent,label,period,dimensions', CASES)
async def test_real_model_multitask_planner_enters_standard_analysis(monkeypatch, question, intent, label, period, dimensions):
    structured = extraction(label, period, dimensions)
    invalid = {'type': 'pipeline', 'operations': [
        {'type': 'derive', 'operator': 'subtract', 'left_field': METRIC,
         'right_field': '不存在的上月金额列', 'result_field': '差额'},
        {'type': 'derive', 'operator': 'divide', 'left_field': '差额',
         'right_field': '不存在的上月金额列', 'result_field': '变化率'}]}
    boundaries = {'tasks': [
        {'question': '查询' + period + '的' + METRIC, 'metric_targets': [METRIC]},
        {'question': '基于上述查询结果计算分析，不再查询数据库', 'depends_on': [0], 'metric_targets': [METRIC], 'postprocess': {'source_task_index': 0, 'operation': invalid}},
    ]}
    def extract(fixed):
        return {'tasks': [{'task_index': index, 'extraction': {**copy.deepcopy(structured),
            **({'是否查询数据库': '否', '结果操作': {'source_task_index': 0, 'operation': invalid}} if index else {})}}
            for index in range(len(fixed['tasks']))]}
    planner, _ = setup_planner(monkeypatch, boundaries, extract=extract)
    router = CompoundQuestionRouter(planner.settings)
    router._single = planner
    outcome = await router.plan_with_judgment(CompoundJudgment(is_compound=False), question, semantic_model_id=81)
    assert outcome.plan is None and outcome.single.single_extraction.intent == intent
    plan = build_analysis_task_plan(question, outcome.single.single_extraction, today=TODAY)
    assert plan.tasks[-1].extraction['分析绑定']['intent'] == intent.value
    assert all('不存在' not in str(task.extraction) for task in plan.tasks)
    if intent == PrimaryIntent.TREND_ANALYSIS:
        assert plan.tasks[0].extraction['时间粒度']['time_range'] == '2025-09-01至2025-12-31'
    elif intent == PrimaryIntent.COMPARISON_ANALYSIS:
        assert [task.extraction['时间粒度']['time_range'] for task in plan.tasks[:-1]] == [
            '2025年11月', '2025年12月']


@pytest.mark.asyncio
@pytest.mark.parametrize('question,intent,label,period,dimensions', CASES)
async def test_real_dag_tools_deliver_results_even_when_root_insight_times_out(monkeypatch, question, intent, label, period, dimensions):
    agent = service()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    agent.analysis_engine = AnalysisEngine()
    # Simulate an interruption before successful root cleanup.
    monkeypatch.setattr(agent.sessions, "delete_dag_checkpoint", AsyncMock())
    chat, _ = context()
    chat.question = question
    plan = build_analysis_task_plan(question, PlannerExtraction(intent=intent, parameters=[],
        structured=extraction(label, period, dimensions)), today=TODAY)
    events = []
    calls = []
    async def query(child, identity):
        structured = child._planner_extraction.structured
        window = structured['时间粒度']['time_range']
        calls.append(window)
        if intent == PrimaryIntent.COMPOSITION_ANALYSIS:
            columns = ['渠道业态', METRIC]
            rows = [{'渠道业态': name, METRIC: value} for name, value in [('乙', 20), ('甲', 70), ('丙', 10)]]
        elif intent == PrimaryIntent.COMPARISON_ANALYSIS:
            columns = [METRIC]
            rows = [{METRIC: 100 if '11-01' in window or '11月' in window else 144}]
        else:
            columns = ['业务日期', METRIC]
            rows = [{'业务日期': month, METRIC: value} for month, value in ([
                ('2025-11', 100), ('2025-09', 100), ('2025-12', 144), ('2025-10', 400)] if '2024' not in window else [])]
        ref = await persist(agent, child, columns, rows, conversation=child.conversation_id)
        child._dag_deferred_insight = {'summary': '已取得查询数据。',
            'facts': {'query_data': {'columns': columns, 'rows': rows}},
            'presentation': {'table': agent._markdown_result_table(columns, rows), 'chart': '', 'notes': []}}
        return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id,
            intent=PrimaryIntent.METRIC_QUERY, status='COMPLETED', answer='已取得查询数据。',
            dataset_id=ref.dataset_id, evidence=[EvidenceItem(evidence_id='query', kind='QUERY_RESULT',
                source_ref='synthetic', payload={'row_count': len(rows)})])
    async def slow(*args, **kwargs):
        await asyncio.sleep(60)
    agent.analysis_synthesizer = SimpleNamespace(synthesize_combined=slow)
    agent.settings.analysis_synthesis_timeout_seconds = .02
    monkeypatch.setattr(agent, '_handle', query)
    with progress_scope(lambda event: events.append(event)):
        # Include the frozen earlier stages in this integration assertion.
        await emit_progress('INTENT_RECOGNITION', 'COMPLETED', 'test')
        await emit_progress('TASK_PLANNING', 'COMPLETED', 'test')
        response = await asyncio.wait_for(agent._handle_task_plan(chat, IDENTITY, plan), timeout=3)
    assert all(result.status == 'COMPLETED' for result in response.task_results)
    assert response.answer and '确定性结果' in response.answer
    tool = next(item for item in response.evidence if item.kind == 'ANALYSIS_RESULT')
    facts = tool.payload['facts']
    if intent == PrimaryIntent.TREND_ANALYSIS:
        assert facts['absolute_change'] == -256
        changes = facts['period_over_period']['changes']
        assert [item['change_rate'] for item in changes] == pytest.approx([3, -.75, .44])
        assert facts['result_scope']['labels'] == ['2025-10', '2025-11', '2025-12']
        assert facts['change_points'][0]['label'] == '2025-11'
        assert all('2025-09' not in str(spec.data) for spec in response.chart_specs)
    elif intent == PrimaryIntent.COMPARISON_ANALYSIS:
        assert facts['absolute_change'] == 44 and facts['change_rate'] == pytest.approx(.44)
        assert len(calls) == 2
    else:
        assert [item['share'] for item in facts['shares']] == pytest.approx([.7, .2, .1])
        assert '70.0000%' in response.answer
        assert any('70.0000%' in event['message'] for event in events if event['stage'] == 'ANALYSIS_TOOL')
    order = {'INTENT_RECOGNITION': 0, 'TASK_PLANNING': 1, 'TASK_STATUS': 2, 'ANALYSIS_TOOL': 2,
        'RELIABILITY_CHECK': 3, 'INSIGHT_ANALYSIS': 4, 'FINAL_OUTPUT': 5}
    observed = [order[event['stage']] for event in events if event['stage'] in order]
    assert observed == sorted(observed)
    assert any(e['stage'] == 'INSIGHT_ANALYSIS' and e['status'] == 'DEGRADED' for e in events)
    before = len(calls)
    different_plan = TaskPlan(tasks=[AtomicTask(task_id='task-9', question='重新规划出的另一查询'), AtomicTask(task_id='task-10', question='另一个查询')], planner='DETERMINISTIC_RULE')
    with progress_scope(lambda event: None):
        replay = await agent._handle_task_plan(chat, IDENTITY, different_plan)
    assert len(calls) == before and replay.task_plan == plan


def test_canonical_time_and_sort_retain_planner_declarations():
    agent = service()
    chat, request = context()
    request.primary_intent = PrimaryIntent.COMPARISON_ANALYSIS
    planned = PlannerExtraction(intent=request.primary_intent, parameters=[],
        structured=extraction('对比分析', '2025年11月和2025年12月', ['业务日期']))
    request._planner_extraction = planned
    agent._apply_planned_temporal_shape(request, planned)
    text = render_execution_question(request)
    assert request.time_range.start == date(2025, 11, 1)
    assert request.time_range.end_exclusive == date(2026, 1, 1)
    assert '2025-12-31' in text and '按业务日期从低到高' in text


def test_missing_derive_field_is_named_before_numeric_validation():
    with pytest.raises(ValueError, match='缺少输入字段：上月金额.*实际字段：金额'):
        _steps(['金额'], [{'金额': 100}], {'type': 'derive', 'operator': 'subtract',
            'left_field': '金额', 'right_field': '上月金额', 'result_field': '差额'})


@pytest.mark.asyncio
async def test_failed_calculation_cannot_be_silently_replaced_by_model_arithmetic():
    generate = AsyncMock(return_value=('伪造同比123%', SimpleNamespace(final_answer={})))
    material = [{'status': 'FAILED', 'calculation_incomplete': True, 'summary': '环比计算未完成：缺少上月字段。'}]
    answer, synthesis = await deliver_root_insight(material, generate, timeout=1)
    generate.assert_not_called()
    assert '未完成' in answer and '123' not in answer and synthesis is None
    text = _evidence_line(EvidenceItem(evidence_id='failed', kind='ANALYSIS_RESULT', source_ref='test',
        payload={'code': 'CALCULATION_CONTRACT_INVALID', 'reason': '缺少上月字段'}))
    assert '未完成' in text and '已形成可复算' not in text


@pytest.mark.asyncio
async def test_generic_computation_emits_real_steps_results_and_failure(monkeypatch):
    agent = service()
    chat, _ = context()
    ref = await persist(agent, chat, ['渠道', METRIC], [{'渠道': '甲', METRIC: 70}, {'渠道': '乙', METRIC: 30}])
    task = AtomicTask(task_id='task-2', question='计算占比，不再查询数据库', depends_on=['task-1'], extraction={
        '结果操作': {'source_task_id': 'task-1', 'operation': {'type': 'pipeline', 'operations': [
            {'type': 'aggregate', 'group_by': [], 'aggregations': [{'field': METRIC, 'function': 'sum', 'alias': '合计'}], 'retain_rows': True},
            {'type': 'derive', 'operator': 'divide', 'left_field': METRIC, 'right_field': '合计', 'result_field': '占比'}]}}})
    events = []
    with progress_scope(lambda event: events.append(event)):
        response = await agent._respond_dataset_computation(chat, IDENTITY, task,
            {'task-1': (ref, [{'渠道': '甲', METRIC: 70}, {'渠道': '乙', METRIC: 30}])})
    assert response.status == 'COMPLETED'
    assert events[0]['status'] == 'RUNNING' and events[-1]['status'] == 'COMPLETED'
    assert any('aggregate' in e['message'] for e in events)
    assert '0.7' in events[-1]['message']
    task.extraction['结果操作']['operation'] = {'type': 'derive', 'operator': 'divide',
        'left_field': METRIC, 'right_field': '未返回字段', 'result_field': '占比'}
    with progress_scope(lambda event: events.append(event)):
        failed = await agent._respond_dataset_computation(chat, IDENTITY, task,
            {'task-1': (ref, [{METRIC: 70}])})
    assert failed.status == 'FAILED' and '未返回字段' in failed.answer
    assert events[-1]['status'] == 'DEGRADED'


def test_checkpoint_recovery_requires_same_question_scope_and_valid_plan_hash():
    plan = TaskPlan(tasks=[AtomicTask(task_id='task-1', question='查询销售额'), AtomicTask(task_id='task-2', question='查询数量')], planner='DETERMINISTIC_RULE')
    checkpoint = {'task_plan': plan.model_dump(mode='json'), 'source_question': '问题', 'authorized_scope': 'scope',
        'plan_fingerprint': hashlib.sha256(plan.model_dump_json().encode()).hexdigest()}
    assert recover_task_plan(checkpoint, '问题', 'scope') == plan
    assert recover_task_plan(checkpoint, '另一问题', 'scope') is None
    assert recover_task_plan(checkpoint, '问题', '另一scope') is None
    checkpoint['task_plan']['tasks'][0]['question'] = '被修改的计划'
    assert recover_task_plan(checkpoint, '问题', 'scope') is None
