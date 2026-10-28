"""Offline regression of Test4's real planning shapes, using synthetic data only."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.analysis import AnalysisEngine
from app.analysis.coverage import source_field_gaps
from app.analysis.engine import AnalysisError
from app.analysis.query_insight import build_query_result_insight
from app.config import Settings
from app.domain.models import AgentResponse, EvidenceItem, MetricRef, PlannerExtraction, PrimaryIntent as I
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.planning.compound_question import (CompoundJudgment, CompoundQuestionJudge, CompoundQuestionRouter,
    COVERAGE_DEFINED, COVERAGE_UNDEFINED, _analysis_goal_label)
from app.planning.task_dag import DecomposedTask, QuestionDecomposition
from app.services.progress import emit_progress, progress_scope
from test_analysis_delivery_regressions import METRIC, TODAY, extraction
from test_complex_execution_correctness import service, context, persist, IDENTITY

COMPARISON = '对比2025年11月和12月各渠道业态的含税销售总额，列出差额和变化率，按增减额降序排列'
CAUSE = '对2025年12月相对于11月的含税销售总额变化做归因分析，按渠道业态拆解各渠道的增减贡献'
ANOMALY = '检测2025年7月至12月月度含税销售总额的异常月份，列出命中方法、检验值、阈值和判定依据'
FORECAST = '基于2025年7月至12月的月度含税销售总额，预测下一月，说明回测依据与适用范围'


@pytest.mark.asyncio
@pytest.mark.parametrize('question,label,gaps,intent', [
    (ANOMALY, '趋势分析', ['阈值', '检验值', '命中方法'], I.ANOMALY_ANALYSIS),
    (FORECAST, '趋势分析', ['回测依据', '适用范围'], I.FORECAST_ANALYSIS),
    (CAUSE, '解释口径', [], I.ROOT_CAUSE_ANALYSIS),
])
async def test_judge_outputs_are_not_source_field_gaps(monkeypatch, question, label, gaps, intent):
    import app.domain.semantic_description as description
    async def reference(*args):
        return {'source': 'none', 'content': ''}
    monkeypatch.setattr(description, 'load_semantic_description', reference)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [{'message': {
        'content': json.dumps({'split': False, 'intent': label, 'business_domain': '销售', 'sub_questions': [],
            'schema_coverage': COVERAGE_UNDEFINED if gaps else COVERAGE_DEFINED, 'unsupported_fields': gaps})}}]}))
    judge = CompoundQuestionJudge(Settings(_env_file=None, env='test', intent_model_api_key='offline'), transport=transport)
    result = await judge._model_judge(question, semantic_model_id=81)
    assert not result.unsupported_fields and result.schema_coverage == COVERAGE_DEFINED
    from app.analysis.intents import LABELS
    assert result.intent == LABELS[intent]


@pytest.mark.parametrize('question,fields,remaining', [
    (ANOMALY, ['阈值、检验值', '患者疗效'], ('患者疗效',)),
    (ANOMALY, ['阈值', '客户阈值额度'], ('客户阈值额度',)),
    (FORECAST, ['回测依据', '客户信用额度'], ('客户信用额度',)),
    ('查询阈值', ['阈值'], ('阈值',)),
])
def test_real_source_gaps_are_never_bypassed(question, fields, remaining):
    assert source_field_gaps(question, fields) == remaining


def test_definition_question_retains_its_metadata_intent():
    assert _analysis_goal_label('解释口径', '什么是预测分析的回测误差') == '解释口径'


def test_nonperiod_comparison_preserves_existing_engine_route():
    task_plan = plan('对比各渠道业态的含税销售总额', I.COMPARISON_ANALYSIS, '对比分析', ['渠道业态'])
    assert 'comparison_dimensions' not in task_plan.tasks[-1].extraction['分析绑定']['requirements']


def test_source_date_sort_is_not_mistaken_for_a_derived_comparison_sort():
    data = extraction('对比分析', '2025年11月和2025年12月', ['业务日期', '渠道业态'])
    data['排序'] = [{'field': '业务日期', 'order': 'asc'}, {'field': '增减额', 'order': 'desc'}]
    task_plan = build_analysis_task_plan(COMPARISON, PlannerExtraction(intent=I.COMPARISON_ANALYSIS,
        parameters=[], structured=data), today=TODAY)
    assert task_plan.tasks[-1].extraction['分析绑定']['requirements']['output_sort'] == [{'field': '增减额', 'order': 'desc'}]
    assert all(task.extraction['排序'] == [{'field': '业务日期', 'order': 'asc'}] for task in task_plan.tasks[:-1])


def plan(question, intent, label, dims, period='2025年7月至12月'):
    return build_analysis_task_plan(question, PlannerExtraction(intent=intent, parameters=[],
        structured=extraction(label, period, dims)), today=TODAY)


@pytest.mark.asyncio
async def test_sorted_generic_two_period_plan_is_canonicalized():
    router = CompoundQuestionRouter(service().settings)
    data = extraction('对比分析', '2025年11月和2025年12月', ['渠道业态'])
    operation = {'type': 'pipeline', 'operations': [
        {'type': 'derive', 'operator': 'subtract', 'left_field': METRIC,
         'right_field': '2025年11月含税销售总额', 'result_field': '增减额'},
        {'type': 'derive', 'operator': 'divide', 'left_field': '增减额',
         'right_field': '2025年11月含税销售总额', 'result_field': '变化率'},
        {'type': 'sort', 'field': '增减额', 'direction': 'desc'}]}
    tasks = [DecomposedTask(question='查询'+p+'各渠道业态含税销售总额',
        intent=I.METRIC_QUERY, structured={**copy.deepcopy(data), '时间粒度': {'unit': '月', 'anchor': '业务日期', 'time_range': p}})
        for p in ['2025年11月', '2025年12月']]
    tasks.append(DecomposedTask(question='基于上述结果比较，不再查询数据库', depends_on=(0, 1),
        intent=I.COMPARISON_ANALYSIS, structured={**data,
            '是否查询数据库': '否', '结果操作': {'source_task_index': 1, 'operation': operation}}))
    router._single = SimpleNamespace(plan_question=AsyncMock(return_value=QuestionDecomposition(intent=I.COMPARISON_ANALYSIS, tasks=tuple(tasks))))
    routed = await router.plan_with_judgment(CompoundJudgment(is_compound=False), COMPARISON, semantic_model_id=81)
    assert routed.plan is not None
    binding = routed.plan.tasks[-1].extraction['分析绑定']
    assert binding['analysis_type'] == 'comparison'
    assert binding['requirements']['comparison_dimensions'] == ['渠道业态']
    assert binding['requirements']['output_sort'] == [{'field': '增减额', 'order': 'desc'}]
    assert all(task.extraction['排序'] == [] for task in routed.plan.tasks[:-1])


def query_response(agent, child, ref, rows):
    child._dag_deferred_insight = {'summary': '已取得查询数据。',
        'facts': {'query_data': {'columns': ref.columns, 'rows': rows}},
        'presentation': {'table': agent._markdown_result_table(ref.columns, rows), 'chart': '', 'notes': []}}
    return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id, intent=I.METRIC_QUERY,
        status='COMPLETED', answer='已取得查询数据。', dataset_id=ref.dataset_id,
        evidence=[EvidenceItem(evidence_id='query', kind='QUERY_RESULT', source_ref='synthetic', payload={'row_count': len(rows)})])


async def run_dag(monkeypatch, agent, question, task_plan, rows_for_query):
    chat, _ = context()
    chat.question = question
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    agent.analysis_engine = AnalysisEngine()
    monkeypatch.setattr(agent.sessions, 'delete_dag_checkpoint', AsyncMock())
    async def query(child, identity):
        columns, rows = rows_for_query(child._planner_extraction.structured)
        ref = await persist(agent, child, columns, rows, conversation=child.conversation_id)
        return query_response(agent, child, ref, rows)
    monkeypatch.setattr(agent, '_handle', query)
    agent.analysis_synthesizer = SimpleNamespace(synthesize_combined=AsyncMock(side_effect=TimeoutError()))
    events = []
    with progress_scope(events.append):
        await emit_progress('INTENT_RECOGNITION', 'COMPLETED', 'test')
        await emit_progress('TASK_PLANNING', 'COMPLETED', 'test')
        response = await agent._handle_task_plan(chat, IDENTITY, task_plan)
        await emit_progress('FINAL_OUTPUT', 'COMPLETED', 'test')
    order = {'INTENT_RECOGNITION': 0, 'TASK_PLANNING': 1, 'TASK_STATUS': 2, 'ANALYSIS_TOOL': 2,
             'RELIABILITY_CHECK': 3, 'INSIGHT_ANALYSIS': 4, 'FINAL_OUTPUT': 5}
    observed = [order[e['stage']] for e in events if e['stage'] in order]
    assert observed == sorted(observed) and set(observed) == set(range(6))
    return response, events


@pytest.mark.asyncio
async def test_group_comparison_dag_keeps_identity_missing_and_zero_bases(monkeypatch):
    task_plan = plan(COMPARISON, I.COMPARISON_ANALYSIS, '对比分析', ['渠道业态'], '2025年11月和2025年12月')
    task_plan.tasks[-1].extraction['分析绑定']['requirements']['output_sort'] = [{'field': '增减额', 'order': 'desc'}]
    def query(data):
        base = '11月' in data['时间粒度']['time_range']
        values = [('1', '同名', 100), ('2', '同名', 80), ('3', '零基期', 0)] if base else [
            ('2', '同名', 60), ('1', '更名', 150), ('3', '零基期', 20), ('8', '新增', 10)]
        return ['渠道业态', '渠道业态（编码）', METRIC], [
            {'渠道业态': name, '渠道业态（编码）': code, METRIC: v} for code, name, v in values]
    response, events = await run_dag(monkeypatch, service(), COMPARISON, task_plan, query)
    assert all(task.status == 'COMPLETED' for task in response.task_results)
    facts = next(e.payload['facts'] for e in response.evidence if e.kind == 'ANALYSIS_RESULT')
    rows = facts['comparisons']
    assert [row['group_key'] for row in rows] == [['1'], ['3'], ['2'], ['8']]
    assert [row['absolute_change'] for row in rows] == [50, 20, -20, None]
    assert rows[0]['ratio'] == 1.5 and rows[0]['base_100_index'] == 150
    assert rows[1]['change_rate'] is None and rows[-1]['status'] == '缺少基期记录'
    assert '50.0000%' in response.answer and '缺少基期记录' in response.answer
    assert any('分组' in e['message'] for e in events if e['stage'] == 'ANALYSIS_TOOL')


@pytest.mark.parametrize('defect', ['duplicate', 'empty_code', 'wrong_identity', 'infinity', 'overflow', 'one_period'])
def test_invalid_grouped_comparison_does_not_guess_or_emit_invalid_numbers(defect):
    _, request = context()
    request.primary_intent = I.COMPARISON_ANALYSIS
    request.metrics = [MetricRef(input=METRIC)]
    request._analysis_contract = {'comparison_dimensions': ['渠道业态']}
    columns = ['渠道业态', '渠道业态（编码）', METRIC, '期间', 'period_role']
    rows = [{'渠道业态': '甲', '渠道业态（编码）': '1', METRIC: v, '期间': p, 'period_role': role}
        for v, p, role in [(100, '2025-11', '基期'), (150, '2025-12', '当前期')]]
    if defect == 'duplicate':
        rows.append(rows[0].copy())
    elif defect == 'empty_code':
        rows[0]['渠道业态（编码）'] = ''
    elif defect == 'wrong_identity':
        request._analysis_contract['comparison_dimensions'] = ['客户']
    elif defect == 'infinity':
        rows[0][METRIC] = float('inf')
    elif defect == 'overflow':
        rows[0][METRIC], rows[1][METRIC] = -1e308, 1e308
    else:
        rows.pop()
    with pytest.raises(AnalysisError):
        AnalysisEngine()._comparison(request, columns, rows, None)


@pytest.mark.asyncio
async def test_attribution_real_retrieval_contract_retains_both_months(monkeypatch):
    # Reproduce extraction with the grouping dimension but no date dimension.
    task_plan = plan(CAUSE, I.COMPARISON_ANALYSIS, '对比分析', ['渠道业态'], '2025年11月')
    for task in task_plan.tasks[:-1]:
        assert '业务日期' in task.extraction['维度'] and '分月' in task.question
        assert task.extraction['时间粒度']['time_range'] == '2025-11-01至2025-12-31'
    def query(data):
        # Unlike old fixtures, only return time if the actual request groups by it.
        grouped = '渠道业态' in data['维度']
        monthly = '业务日期' in data['维度']
        if not monthly:
            return [METRIC], [{METRIC: 240}]
        if not grouped:
            return ['业务日期', METRIC], [{'业务日期': m, METRIC: v} for m, v in [('2025-11', 100), ('2025-12', 140)]]
        values = [('2025-11', '1', 80), ('2025-12', '1', 60), ('2025-11', '2', 20), ('2025-12', '2', 70), ('2025-12', '8', 10)]
        return ['业务日期', '渠道业态', '渠道业态（编码）', METRIC], [
            {'业务日期': m, '渠道业态': '同名' if code in ['1', '2'] else '新增', '渠道业态（编码）': code, METRIC: v}
            for m, code, v in values]
    response, _ = await run_dag(monkeypatch, service(), CAUSE, task_plan, query)
    assert all(task.status == 'COMPLETED' for task in response.task_results)
    facts = next(e.payload['facts'] for e in response.evidence if e.kind == 'ANALYSIS_RESULT')
    assert len(facts['ranked_candidates']) == 3
    assert facts['observed_change'] == 40
    assert '同名（1）' in response.answer and '同名（2）' in response.answer


@pytest.mark.asyncio
@pytest.mark.parametrize('question,intent,label', [(ANOMALY, I.ANOMALY_ANALYSIS, '异常分析'), (FORECAST, I.FORECAST_ANALYSIS, '预测分析')])
async def test_monthly_tools_show_methods_and_results(monkeypatch, question, intent, label):
    task_plan = plan(question, intent, label, ['业务日期'])
    def query(data):
        return ['业务日期', METRIC], [{'业务日期': f'2025-{month:02}', METRIC: value}
            for month, value in zip(range(7, 13), [100, 110, 120, 130, 500, 150])]
    response, events = await run_dag(monkeypatch, service(), question, task_plan, query)
    assert all(task.status == 'COMPLETED' for task in response.task_results)
    facts = next(e.payload['facts'] for e in response.evidence if e.kind == 'ANALYSIS_RESULT')
    reports = facts['capability_results']
    assert len(reports) >= 5
    assert all(any(report['name'] in e['message'] for e in events) for report in reports)
    assert response.answer and '确定性结果' in response.answer
    if intent == I.ANOMALY_ANALYSIS:
        assert 'MAD' in response.answer and '阈值' in response.answer and '2025-11' in response.answer
    else:
        assert '回测' in response.answer and '预测' in response.answer


@pytest.mark.asyncio
async def test_incomplete_analysis_is_not_shown_as_completed(monkeypatch):
    task_plan = plan(COMPARISON, I.COMPARISON_ANALYSIS, '对比分析', ['渠道业态'], '2025年11月和2025年12月')
    def query(data):
        # Missing stable identity must fail safely, not align by position/name.
        return ['渠道业态', METRIC], [{'渠道业态': '甲', METRIC: 100}]
    response, events = await run_dag(monkeypatch, service(), COMPARISON, task_plan, query)
    terminal = [e for e in events if e.get('task_terminal') and e.get('task_id') == task_plan.tasks[-1].task_id]
    assert response.task_results[-1].status != 'COMPLETED'
    assert any(e['status'] == 'DEGRADED' and '未完成' in e['message'] for e in terminal)
    assert not any(e['status'] == 'COMPLETED' for e in terminal)
    assert '未完成' in response.answer


def test_numeric_channel_ids_are_not_measure_statistics():
    _, request = context()
    request.metrics = [MetricRef(input=METRIC)]
    request.dimensions = ['渠道业态']
    output = build_query_result_insight(request, ['渠道业态', '渠道业态（编码）', METRIC], [
        {'渠道业态': 1, '渠道业态（编码）': 1, METRIC: 100},
        {'渠道业态': 6, '渠道业态（编码）': 6, METRIC: 200}],
        total_row_count=2, total_row_count_confirmed=True, truncated=False)
    assert '3.5' not in output.answer and '平均值为150' in output.answer
    profile = {col['name']: col for col in output.facts['data_profile']['columns']}
    assert 'numeric_summary' not in profile['渠道业态'] and 'numeric_summary' not in profile['渠道业态（编码）']
