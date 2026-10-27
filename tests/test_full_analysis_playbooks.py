"""Offline reproductions of the October 22 questions through real tools."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.analysis import AnalysisEngine
from app.analysis.intents import analysis_goals
from app.domain.models import AgentResponse, EvidenceItem, PlannerExtraction, PrimaryIntent as I
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.planning.compound_question import (CompoundJudgment, CompoundJudgmentEntry,
    CompoundTaskPlanner, CompoundQuestionJudge, judgment_display_section)
from app.planning.task_dag import QuestionDecomposition, DecomposedTask
from app.services.progress import progress_scope
from test_complex_execution_correctness import service, context, IDENTITY
from test_analysis_delivery_regressions import METRIC, TODAY, extraction

TREND = '2025年7到12月上海市销售金额趋势分析'
ANOMALY = '检测2025年10月1日至12月30日每日含税销售总额是否存在异常波动，列出异常日期及判定依据'
CAUSE = '对2025年12月相对于11月的含税销售总额变化做归因分析，按渠道业态拆解各渠道的增减贡献'
FORECAST = '基于2025年7月至12月的月度含税销售总额，预测下一月销售额'


def setup(question, intent, label, period, dimensions, unit='月'):
    data = extraction(label, period, dimensions)
    data['时间粒度']['unit'] = unit
    if question == TREND:
        data['过滤条件'] = [{'entity': '销售订单', 'field': '城市', 'op': '=', 'value': '上海市'}]
    plan = build_analysis_task_plan(question, PlannerExtraction(intent=intent, structured=data, parameters=[]), today=TODAY)
    return plan


@pytest.mark.parametrize('question,intent', [(ANOMALY, I.ANOMALY_ANALYSIS), (CAUSE, I.ROOT_CAUSE_ANALYSIS), (FORECAST, I.FORECAST_ANALYSIS)])
@pytest.mark.asyncio
async def test_initial_compound_judge_does_not_show_wrong_query_shape(monkeypatch, question, intent):
    import json
    import app.domain.semantic_description as description
    from app.config import Settings
    async def reference(*args):
        return {'source': 'none', 'content': ''}
    monkeypatch.setattr(description, 'load_semantic_description', reference)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
        'split': False, 'intent': '对比分析' if question == CAUSE else '趋势分析', 'business_domain': '销售', 'sub_questions': []})}}]}))
    judge = CompoundQuestionJudge(Settings(env='test', intent_model_api_key='offline-placeholder'), transport=transport)
    result = await judge._model_judge(question, semantic_model_id=81)
    from app.analysis.intents import LABELS
    assert LABELS[intent] in judgment_display_section(result)
    assert result.is_compound is False


@pytest.mark.asyncio
@pytest.mark.parametrize('question,intent,label,period,dims,unit', [
    (TREND, I.TREND_ANALYSIS, '趋势分析', '2025年7月至12月', ['业务日期'], '月'),
    (ANOMALY, I.TREND_ANALYSIS, '趋势分析', '2025-10-01至2025-12-30', ['业务日期'], '日'),
    # Deliberately reproduce the wrong model intent and missing December.
    (CAUSE, I.COMPARISON_ANALYSIS, '对比分析', '2025年11月', ['渠道业态'], '月'),
    (FORECAST, I.TREND_ANALYSIS, '趋势分析', '2025年7月至12月', ['业务日期'], '月'),
])
async def test_real_analysis_task_has_all_executed_details(monkeypatch, question, intent, label, period, dims, unit):
    agent = service()
    agent.analysis_engine = AnalysisEngine()
    chat, _ = context()
    chat.question = question
    plan = setup(question, intent, label, period, dims, unit)
    task = plan.tasks[-1]
    binding = task.extraction['分析绑定']
    if question == CAUSE:
        assert task.primary_intent == I.ROOT_CAUSE_ANALYSIS
        assert all(p.extraction['时间粒度']['time_range'] == '2025-11-01至2025-12-31' for p in plan.tasks[:-1])
        total = [{'月份': '2025-11', METRIC: 100}, {'月份': '2025-12', METRIC: 144}]
        parts = [{'月份': m, '渠道业态': name, METRIC: v} for m, name, v in
                 [('2025-11', '甲', 80), ('2025-12', '甲', 60), ('2025-11', '乙', 20), ('2025-12', '乙', 84)]]
        refs = {'task-1': (SimpleNamespace(columns=['月份', METRIC]), total),
                'task-2': (SimpleNamespace(columns=['渠道业态', '月份', METRIC]), parts)}
    elif question == ANOMALY:
        assert task.primary_intent == I.ANOMALY_ANALYSIS
        rows = [{'日期': (date(2025, 10, 1)+timedelta(days=i)).isoformat(), METRIC: 1000 if i == 40 else 100+(i%5-2)} for i in range(91)]
        refs = {'task-1': (SimpleNamespace(columns=['日期', METRIC]), rows)}
    else:
        rows = [{'月份': f'2025-{i:02}', METRIC: i*10} for i in range(6 if question == TREND else 7, 13)]
        refs = {'task-1': (SimpleNamespace(columns=['月份', METRIC]), rows)}
        if question == TREND:
            assert len(plan.tasks) == 3
            assert plan.tasks[0].extraction['时间粒度']['time_range'] == '2025-06-01至2025-12-31'
            assert plan.tasks[1].extraction['时间粒度']['time_range'] == '2024-07-01至2024-12-31'
            assert all(p.extraction['过滤条件'][0]['value'] == '上海市' for p in plan.tasks[:-1])
        else:
            assert task.primary_intent == I.FORECAST_ANALYSIS
    monkeypatch.setattr(agent, '_load_task_result_references', AsyncMock(return_value=refs))
    monkeypatch.setattr(agent, '_save_analysis_derived_dataset', AsyncMock(return_value=SimpleNamespace(dataset_id='derived')))
    events = []
    with progress_scope(events.append):
        response = await agent._respond_analysis_task(chat, IDENTITY, task, {}, {}, binding)
    assert response.status == 'COMPLETED'
    facts = response.evidence[0].payload['facts']
    reports = facts['capability_results']
    assert len(reports) >= 5
    assert all(any(report['name'] in event['message'] for event in events) for report in reports)
    assert not any('输出分析结论与事实' in e['message'] for e in events[:-1])
    if question == TREND:
        assert len(facts['period_over_period']['changes']) == 6
        assert facts['year_over_year']['status'] == 'NOT_COMPUTABLE'
        assert facts['result_scope']['labels'] == [f'2025-{i:02}' for i in range(7, 13)]
    elif question == ANOMALY:
        assert facts['candidate_count'] >= 1
        candidate = next(row for row in facts['candidates'] if row['label'] == '2025-11-10')
        assert 'MAD' in candidate['detectors'] and candidate['reasons']
        assert {'MAD', 'IQR', 'Z_SCORE', 'ADJACENT_JUMP'} <= {d['name'] for d in facts['detectors']}
        assert '2025-11-10' in response.answer and 'threshold' in response.answer
    elif question == CAUSE:
        assert facts['contribution_sum'] == 44
        assert facts['offset_decomposition'] == {'positive_change': 64, 'negative_change': -20, 'net_change': 44, 'offset_amount': 20}
        assert facts['residual'] == 0 and facts['additivity_check']['status'] == 'passed'
        assert facts['causality_established'] is False
    else:
        assert facts['selected_model'] == 'linear_drift'
        assert facts['forecast'] == pytest.approx(130)
        assert facts['forecast_period'] == '2026-01'
        assert len(facts['backtests']) == 6  # three candidates, two holdouts
        assert facts['forecast_quality'] == 'EXPLORATORY' and response.reliability.level == 'LIMITED'
        assert all(row['train_points'] == row['validation_index'] for row in facts['backtests'])
        assert '6个月无法估计年度季节性' in response.answer


@pytest.mark.asyncio
async def test_optional_empty_yoy_dependency_does_not_block_real_dag(monkeypatch):
    from test_analysis_delivery_regressions import test_real_dag_tools_deliver_results_even_when_root_insight_times_out, CASES
    # The shared fixture stores a proven zero-row YoY dataset, runs the real
    # scheduler and asserts trend/MoM, scoped charts, fallback and stage order.
    await test_real_dag_tools_deliver_results_even_when_root_insight_times_out(monkeypatch, *CASES[0])


@pytest.mark.asyncio
async def test_composite_goals_expand_independently_and_rebase_dependencies():
    from app.config import Settings
    entries = [CompoundJudgmentEntry(question=TREND), CompoundJudgmentEntry(question=ANOMALY), CompoundJudgmentEntry(question=FORECAST)]
    cases = {TREND: (I.TREND_ANALYSIS, '2025年7月至12月', '月'),
             ANOMALY: (I.ANOMALY_ANALYSIS, '2025-10-01至2025-12-30', '日'),
             FORECAST: (I.FORECAST_ANALYSIS, '2025年7月至12月', '月')}
    async def single(question, **kwargs):
        intent, period, unit = cases[question]
        from app.analysis.intents import LABELS
        structured = extraction(LABELS[intent], period, ['业务日期'])
        structured['时间粒度']['unit'] = unit
        return QuestionDecomposition(intent=intent, tasks=(DecomposedTask(question=question, intent=intent, structured=structured),))
    planner = CompoundTaskPlanner(Settings(env='test'), SimpleNamespace(plan_question=single))
    judgment = CompoundJudgment(is_compound=True, entries=tuple(entries))
    plan = await planner.plan(judgment, source_question='；'.join(e.question for e in entries), semantic_model_id=81)
    assert len(plan.question_groups) == 3
    assert [p.primary_intent for p in plan.tasks if (p.extraction or {}).get('分析绑定')] == [I.TREND_ANALYSIS, I.ANOMALY_ANALYSIS, I.FORECAST_ANALYSIS]
    assert len(plan.tasks) == 7
    assert plan.tasks[4].depends_on == ['task-4']
    assert plan.tasks[6].depends_on == ['task-6']
    assert set(analysis_goals('分别进行趋势分析，同时检测异常，再预测下月销售额')) == {I.TREND_ANALYSIS, I.ANOMALY_ANALYSIS, I.FORECAST_ANALYSIS}


@pytest.mark.parametrize('question', ['分析历史销售趋势，不要预测', '分析历史销售趋势，不是要预测', '趋势分析，无需同比', '趋势分析，不要计算同比'])
def test_explicit_exclusion_is_not_overridden(question):
    assert I.FORECAST_ANALYSIS not in analysis_goals(question)
    if '同比' in question:
        plan = setup(question, I.TREND_ANALYSIS, '趋势分析', '2025年7月至12月', ['业务日期'])
        assert len(plan.tasks) == 2


@pytest.mark.asyncio
async def test_real_compound_dag_keeps_all_chains_when_model_selects_only_trend(monkeypatch):
    from uuid import uuid4
    from app.config import Settings
    from app.planning.compound_question import CompoundQuestionRouter
    from test_complex_execution_correctness import persist
    cases = {TREND: (I.TREND_ANALYSIS, '趋势分析', '2025年7月至12月', '月'),
             ANOMALY: (I.ANOMALY_ANALYSIS, '异常分析', '2025-10-01至2025-12-30', '日'),
             FORECAST: (I.FORECAST_ANALYSIS, '预测分析', '2025年7月至12月', '月')}
    async def single(question, **kwargs):
        intent, label, period, unit = cases[question]
        value = extraction(label, period, ['业务日期'])
        value['时间粒度']['unit'] = unit
        return QuestionDecomposition(intent=intent, tasks=(DecomposedTask(question=question, intent=intent, structured=value),))
    planner = CompoundTaskPlanner(Settings(env='test'), SimpleNamespace(plan_question=single))
    question = '；'.join(cases)
    plan = await planner.plan(CompoundJudgment(is_compound=True,
        entries=tuple(CompoundJudgmentEntry(question=q) for q in cases)), source_question=question, semantic_model_id=81)
    agent = service()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    agent.analysis_engine = AnalysisEngine()
    chat, _ = context()
    chat.question = question
    async def query(child, identity):
        value = child._planner_extraction.structured
        window = value['时间粒度']['time_range']
        if value['时间粒度']['unit'] == '日':
            rows = [{'业务日期': (date(2025,10,1)+timedelta(days=i)).isoformat(), METRIC: 1000 if i == 40 else 100+i%5} for i in range(91)]
        elif '2024' in window:
            rows = []
        else:
            rows = [{'业务日期': f'2025-{i:02}', METRIC: i*10} for i in range(6 if '06-01' in window else 7, 13)]
        ref = await persist(agent, child, ['业务日期', METRIC], rows, conversation=child.conversation_id)
        return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id, status='COMPLETED',
            intent=I.METRIC_QUERY, answer='查询完成', dataset_id=ref.dataset_id,
            evidence=[EvidenceItem(evidence_id='query', kind='QUERY_RESULT', source_ref='synthetic', payload={'row_count': len(rows)})])
    monkeypatch.setattr(agent, '_handle', query)
    agent.analysis_synthesizer = SimpleNamespace(synthesize_combined=AsyncMock(return_value=('模型仅总结趋势',
        SimpleNamespace(final_answer={'overview': '模型仅总结趋势', 'result_task_ids': ['task-3']}))))
    events = []
    with progress_scope(events.append):
        response = await agent._handle_task_plan(chat, IDENTITY, plan)
    assert len(response.task_results) == 7
    assert all(task.status == 'COMPLETED' for task in response.task_results)
    assert len([item for item in response.evidence if item.kind == 'ANALYSIS_RESULT']) == 3
    assert all(title in response.answer for title in ('环比增减量', '异常日期及判定依据', '候选模型比较', '2026-01'))
    positions = {'TASK_STATUS': 0, 'ANALYSIS_TOOL': 0, 'RELIABILITY_CHECK': 1, 'INSIGHT_ANALYSIS': 2, 'FINAL_OUTPUT': 3}
    observed = [positions[event['stage']] for event in events if event['stage'] in positions]
    assert observed == sorted(observed)


@pytest.mark.asyncio
async def test_multiple_attribution_dimensions_reconcile_independently(monkeypatch):
    agent = service()
    agent.analysis_engine = AnalysisEngine()
    chat, _ = context()
    chat.question = CAUSE
    plan = setup(CAUSE, I.ROOT_CAUSE_ANALYSIS, '归因分析', '2025年11月和12月', ['渠道业态', '地区'])
    task = plan.tasks[-1]
    total = [{'月份': '2025-11', METRIC: 100}, {'月份': '2025-12', METRIC: 144}]
    refs = {'task-1': (SimpleNamespace(columns=['月份', METRIC]), total)}
    for task_id, dimension in [('task-2', '渠道业态'), ('task-3', '地区')]:
        refs[task_id] = (SimpleNamespace(columns=[dimension, '月份', METRIC]),
                        [{**row, dimension: '甲'} for row in total])
    monkeypatch.setattr(agent, '_load_task_result_references', AsyncMock(return_value=refs))
    monkeypatch.setattr(agent, '_save_analysis_derived_dataset', AsyncMock(return_value=SimpleNamespace(dataset_id='derived')))
    response = await agent._respond_analysis_task(chat, IDENTITY, task, {}, {}, task.extraction['分析绑定'])
    assert response.status == 'COMPLETED'
    facts = response.evidence[0].payload['facts']
    assert facts['marginal_dimensions_are_additive'] is False
    assert all(panel['facts']['contribution_sum'] == 44 and panel['facts']['residual'] == 0 for panel in facts['dimension_analyses'])
    assert '88' not in response.answer


def test_forecast_multi_step_target_is_never_silently_one_step():
    from app.intent.classifier import RuleBasedIntentClassifier
    assert RuleBasedIntentClassifier._forecast_target('预测未来三个月销售额') == (3, 'month')
    assert RuleBasedIntentClassifier._forecast_target(FORECAST) == (1, 'month')


def test_anomaly_constant_data_and_missing_days_do_not_invent_flags():
    from app.analysis.anomaly_detection import detect
    labels = [f'2025-10-{day:02}' for day in (1,2,3,5,6,7)]
    result = detect(labels, [100]*6)
    assert result['candidate_count'] == 0
    assert next(d for d in result['detectors'] if d['name'] == 'ADJACENT_JUMP')['status'] == 'NOT_COMPUTABLE'


@pytest.mark.asyncio
async def test_preplanned_yoy_queries_still_enter_full_analysis_without_losing_windows():
    from app.config import Settings
    from app.planning.compound_question import CompoundQuestionRouter
    tasks = tuple(DecomposedTask(question='查询'+period+'销售趋势', intent=I.TREND_ANALYSIS,
        structured=extraction('趋势分析', period, ['业务日期'])) for period in ('2025年7月至12月', '2024年7月至12月'))
    tasks += (DecomposedTask(question='根据结果计算同比趋势，不再查询数据库', depends_on=(0,1),
                           intent=I.COMPARISON_ANALYSIS, structured={'是否查询数据库': '否'}),)
    decomposition = QuestionDecomposition(intent=I.TREND_ANALYSIS, tasks=tasks)
    router = CompoundQuestionRouter(Settings(env='test'))
    router._single = SimpleNamespace(plan_question=AsyncMock(return_value=decomposition))
    result = await router._route_single('2025年下半年各月含税销售总额同比趋势',
        judgment=CompoundJudgment(is_compound=False), semantic_model_id=81)
    assert len(result.plan.tasks) == 3
    # STALE_TEST: an explicit YoY request now selects only YoY, without a MoM baseline.
    assert result.plan.tasks[0].extraction['时间粒度']['time_range'] == '2025-07-01至2025-12-31'
    assert result.plan.tasks[1].extraction['时间粒度']['time_range'] == '2024-07-01至2024-12-31'
    assert result.plan.tasks[-1].extraction['分析绑定']['requirements']['methods'] == ['year_over_year_change']


@pytest.mark.parametrize('conflict', ['different_filter', 'registered_calculation', 'uncovered_window'])
def test_period_normalization_preserves_distinct_business_contracts(conflict):
    from dataclasses import replace
    from app.planning.analysis_task_planner import standardize_period_analysis_plan
    values = [extraction('趋势分析', period, ['业务日期']) for period in ('2025年7月至12月', '2024年7月至12月')]
    if conflict == 'different_filter':
        values[1]['过滤条件'] = [{'field': '城市', 'value': '北京市'}]
    elif conflict == 'registered_calculation':
        values[1]['计算绑定'] = {'operator': 'divide'}
    else:
        values[1]['时间粒度']['time_range'] = '2023年7月至12月'
    tasks = tuple(DecomposedTask(question='查询销售数据', intent=I.TREND_ANALYSIS, structured=value) for value in values)
    result = standardize_period_analysis_plan('2025年下半年各月含税销售总额同比趋势',
        QuestionDecomposition(intent=I.TREND_ANALYSIS, tasks=tasks))
    assert result is None
