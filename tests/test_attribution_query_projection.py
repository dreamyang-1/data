"""Regression: generated marginal queries must own their display grain."""
import copy

import pytest

from app.domain.models import PlannerExtraction, PrimaryIntent
from app.planning.analysis_task_planner import build_analysis_task_plan, _retrieval_extraction


@pytest.fixture
def attribution_input():
    return {
        '实体': ['销售订单'], '指标': [{'name': '含税销售总额'}],
        '维度': ['业务日期', '渠道业态', '商品', '经销商'],
        '展示字段': [
            {'entity': '销售订单', 'field': '渠道业态'},
            {'entity': '商品', 'field': '商品名称'},
            {'entity': '经销商', 'field': '经销商名称'},
        ],
        '过滤条件': [{'entity': '城市', 'field': '城市名称', 'op': '=', 'value': '上海市'}],
        '时间粒度': {'unit': 'month', 'anchor': '业务日期', 'time_range': '2025-11-01至2025-12-31'},
        '排序': [],
        '_analysis_tool_plan': {
            'catalog_version': 'analysis-tools-v1', 'chain': 'attribution',
            'coverage_mode': 'full_profile', 'grain': 'month',
            'comparison_periods': [
                {'role': 'BASE', 'start': '2025-11-01', 'end_exclusive': '2025-12-01'},
                {'role': 'CURRENT', 'start': '2025-12-01', 'end_exclusive': '2026-01-01'},
            ],
            'user_evidence': '对2025年12月相对于11月的含税销售总额变化做归因分析',
        },
    }


def compiled(structured):
    return build_analysis_task_plan(
        '对2025年12月相对于11月的含税销售总额变化做归因分析',
        PlannerExtraction(intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
                          parameters=[], structured=structured),
    )


def test_total_query_has_no_marginal_display_fields(attribution_input):
    plan = compiled(attribution_input)
    assert plan.tasks[0].extraction['维度'] == ['业务日期']
    assert plan.tasks[0].extraction.get('展示字段') == []


@pytest.mark.parametrize('index,dimension,display', [
    (1, '渠道业态', {'entity': '销售订单', 'field': '渠道业态'}),
    (2, '商品', {'entity': '商品', 'field': '商品名称'}),
    (3, '经销商', {'entity': '经销商', 'field': '经销商名称'}),
])
def test_marginal_query_keeps_own_label_without_other_dimensions(
    attribution_input, index, dimension, display,
):
    task = compiled(attribution_input).tasks[index]
    assert task.extraction['维度'] == ['业务日期', dimension]
    assert task.extraction['展示字段'] == [display]


def test_rewrite_preserves_periods_filters_tools_dependencies_and_original(attribution_input):
    original = copy.deepcopy(attribution_input)
    plan = compiled(attribution_input)
    assert attribution_input == original
    assert len(plan.tasks) == 5
    assert plan.tasks[-1].depends_on == [task.task_id for task in plan.tasks[:-1]]
    for task in plan.tasks[:-1]:
        assert task.extraction['过滤条件'] == original['过滤条件']
        assert task.extraction['指标'] == original['指标']
        assert task.extraction['时间粒度']['time_range'] == '2025-11-01至2025-12-31'
        assert task.extraction['_analysis_tool_plan'] == original['_analysis_tool_plan']
    assert plan.tasks[-1].extraction['展示字段'] == original['展示字段']


def test_single_channel_plan_is_total_channel_and_calculation(attribution_input):
    attribution_input['维度'] = ['业务日期', '渠道业态']
    plan = compiled(attribution_input)
    assert len(plan.tasks) == 3
    assert plan.tasks[0].extraction['展示字段'] == []
    assert plan.tasks[1].extraction['展示字段'] == [attribution_input['展示字段'][0]]


def test_unrelated_query_projection_is_not_globally_pruned(attribution_input):
    result = _retrieval_extraction(attribution_input)
    assert result['展示字段'] == attribution_input['展示字段']


def test_entity_group_keeps_requested_identity_and_label(attribution_input):
    attribution_input['维度'] = ['业务日期', '商品']
    attribution_input['展示字段'].append({'entity': '商品', 'field': '商品编码'})
    task = compiled(attribution_input).tasks[1]
    assert task.extraction['展示字段'] == [
        {'entity': '商品', 'field': '商品名称'},
        {'entity': '商品', 'field': '商品编码'},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('index', [0, 1, 2, 3])
async def test_compiled_inputs_reach_classification_without_prose_product_filters(attribution_input, index):
    from app.domain.models import TrustedIdentity
    from test_orchestrator import service

    task = compiled(attribution_input).tasks[index]
    agent = service()
    request = await agent._classify(
        task.question, TrustedIdentity(tenant_id='projection-test', user_id='projection-test'),
        'projection-test', pre_resolved=True,
        planner_extraction=PlannerExtraction(intent=task.primary_intent, structured=task.extraction),
    )
    assert request.dimensions == task.extraction['维度']
    assert request.fields == [item['field'] for item in task.extraction['展示字段']]
    assert request.filters == [{'entity':'城市', 'field':'城市名称', 'operator':'=', 'value':'上海市'}]
    assert request.semantic_entity_mentions == []
    assert '2025年11月1日' not in str(request.filters)


def test_authoritative_input_keeps_scope_and_catalog_validation(attribution_input):
    from app.domain.models import CanonicalAnalysisRequest
    from app.services.orchestrator import DataAnalysisOrchestrator

    task = compiled(attribution_input).tasks[1]
    request = CanonicalAnalysisRequest(conversation_id='scope-test', tenant_id='t', user_id='u',
        original_question=task.question, primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=130, business_domain_ids=[205])
    DataAnalysisOrchestrator._apply_dag_structured_query_shape(
        request, PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY, structured=task.extraction))
    assert request.semantic_model_id == 130
    assert request.business_domain_ids == [205]
    invalid = copy.deepcopy(task.extraction)
    invalid['_analysis_tool_plan']['capabilities'] = ['invented_tool']
    with pytest.raises(ValueError, match='UNREGISTERED'):
        DataAnalysisOrchestrator._apply_dag_structured_query_shape(
            request, PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY, structured=invalid))


def test_ordinary_independent_queries_keep_existing_handoff(attribution_input):
    from app.domain.models import CanonicalAnalysisRequest
    from app.services.orchestrator import DataAnalysisOrchestrator

    request = CanonicalAnalysisRequest(conversation_id='ordinary-test', tenant_id='t', user_id='u',
        original_question='查询商品明细', primary_intent=PrimaryIntent.DETAIL_QUERY,
        fields=['商品名称'], dimensions=['商品'], semantic_entity_mentions=['已确认的真实商品'])
    before = request.model_dump()
    plain = copy.deepcopy(attribution_input)
    plain.pop('_analysis_tool_plan')
    DataAnalysisOrchestrator._apply_dag_structured_query_shape(
        request, PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY, structured=plain))
    assert request.model_dump() == before


def test_concurrent_object_comparison_route_preserves_two_explicit_objects(attribution_input):
    """The colleague's direct-object route must survive the projection fix."""
    structured = copy.deepcopy(attribution_input)
    structured['维度'] = ['业务日期']
    structured['展示字段'] = []
    structured['过滤条件'] = []
    structured['_analysis_tool_plan'] = {
        'chain':'comparison', 'coverage_mode':'explicit', 'capabilities':['comparison_difference'],
        'grain':'month', 'target_window':{'start':'2025-11-01','end_exclusive':'2026-01-01'},
        'comparison_kind':'objects', 'comparison_objects':[
            {'role':'BASE','entity':'销售订单','field':'渠道业态','value':'直销'},
            {'role':'CURRENT','entity':'销售订单','field':'渠道业态','value':'分销'},
        ], 'user_evidence':'对比直销和分销的含税销售总额',
    }
    plan = build_analysis_task_plan('对比直销和分销的含税销售总额',
        PlannerExtraction(intent=PrimaryIntent.COMPARISON_ANALYSIS, structured=structured))
    assert len(plan.tasks) == 3
    assert [task.extraction['过滤条件'][-1]['value'] for task in plan.tasks[:2]] == ['直销','分销']
    binding = plan.tasks[-1].extraction['分析绑定']
    assert [period['role'] for period in binding['periods']] == ['基期','当前期']
    assert binding['requirements'].get('comparison_dimensions', []) == []
