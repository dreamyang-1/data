"""Materialized predecessor keys replace references, never explicit literals."""
import hashlib
import json

import pytest

from app.adapters.http import HttpDataRetrievalAdapter
from app.domain.models import CanonicalAnalysisRequest, DependencyConstraint, PlannerExtraction, PrimaryIntent
from app.services.intent_asl_contract import build_intent_asl_contract
from app.services.intent_asl_contract import validate_intent_asl_contract_completeness
from app.services.turn_admission import TurnAdmissionGate
from app.services.orchestrator import DataAnalysisOrchestrator, _INTERNAL_ASSUMPTIONS


def request_for(value, *, field='经销商名称', constrained=True, operator='=', question='查询上述经销商在2025年销售的产品'):
    condition = {'entity': '经销商', 'field': field, 'operator': operator, 'value': value}
    values = [f'D{index:03}' for index in range(20)]
    constraint = DependencyConstraint(source_task_id='task-1', source_dataset_id='complete-result',
        source_column='dealer.dealer_code', values=values,
        value_fingerprint=hashlib.sha256(json.dumps(values).encode()).hexdigest())
    request = CanonicalAnalysisRequest(conversation_id='child', tenant_id='t', user_id='u',
        original_question=question, primary_intent=PrimaryIntent.DETAIL_QUERY, entity='商品',
        fields=['商品名称'], filters=[condition], dependency_constraints=[constraint] if constrained else [])
    request._planner_extraction = PlannerExtraction(intent=PrimaryIntent.DETAIL_QUERY, structured={
        '展示字段': [{'entity': '商品', 'field': '商品名称'}],
        '过滤条件': [{**condition, 'op': operator}], '时间粒度': {'time_range': '2025年'}})
    return request


@pytest.mark.parametrize('value', ['复用上一轮查询结果：2025年销售额最高的经销商名称',
    ['根据上述任务结果：选出的经销商名称'],
    '复用上一轮查询结果中的经销商名称',
    ['根据上述任务结果中的经销商名称']])
def test_materialized_reference_does_not_become_an_extra_business_filter(value):
    request = request_for(value)
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert [item['field'] for item in request.filters] == ['dealer.dealer_code']
    assert len(request.filters[0]['value']) == 20  # all selected keys, not a ten-row preview
    assert [item['field'] for item in request._planner_extraction.structured['过滤条件']] == ['dealer.dealer_code']
    assert build_intent_asl_contract(request)['filters'][0]['value'] == request.filters[0]['value']
    assert request._planner_extraction.structured['时间粒度']['time_range'] == '2025年'


@pytest.mark.parametrize('overrides', [
    {'value': '明确指定的经销商'},
    {'value': '复用上一轮查询结果：经销商', 'constrained': False},
    {'value': '复用上一轮查询结果：医院', 'field': '医院名称'},
    {'value': '复用上一轮查询结果：经销商', 'operator': 'NOT IN'},
    {'value': '复用上一轮查询结果：公司甲', 'question': '仅查询名称为“复用上一轮查询结果：公司甲”的经销商产品'},
    {'value': '复用上一轮查询结果中的经销商名称',
     'question': '仅查询名称为“复用上一轮查询结果中的经销商名称”的经销商产品'},
])
def test_explicit_filters_and_unbound_references_are_preserved(overrides):
    request = request_for(**overrides)
    original = request.filters[0].copy()
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert original in request.filters


def test_mixed_named_selection_keeps_the_named_part():
    request = request_for(['复用上一轮查询结果：经销商', '明确指定的经销商'], operator='IN')
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert request.filters[0]['value'] == ['明确指定的经销商']


def test_resolved_reference_is_not_rebound_as_a_separate_named_entity():
    reference = '复用上一轮查询结果：2025年销售额最高的经销商名称'
    request = request_for(reference)
    request.semantic_entity_mentions = [reference, '明确指定的厂家']
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert request.semantic_entity_mentions == ['明确指定的厂家']


@pytest.mark.parametrize('value', [
    '${task_0.经销商名称}',
    ['${task_0.经销商名称}'],
    ['${TASK-1.dealer_name}'],
])
def test_task_placeholder_reference_is_replaced_by_materialized_keys(value):
    """计划器把前序任务值写成模板占位符时，由依赖约束的标准键接管。"""
    request = request_for(value, operator='IN' if isinstance(value, list) else '=')
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert [item['field'] for item in request.filters] == ['dealer.dealer_code']
    assert [item['field'] for item in request._planner_extraction.structured['过滤条件']] == ['dealer.dealer_code']


@pytest.mark.parametrize('value', [
    '依赖任务1输出的经销商名称列表',
    ['依赖任务1输出的经销商名称列表'],
    '任务2返回的经销商名称列表',
    '依赖前序任务结果的名单',
])
def test_prose_dependency_reference_is_replaced_by_materialized_keys(value):
    """2026-10-21 端到端实测：计划器把前序值写成“依赖任务N输出的XX列表”
    自然语言占位时同样由依赖约束标准键接管，不能当筛选值做向量绑定。"""
    request = request_for(value, operator='IN' if isinstance(value, list) else '=')
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert [item['field'] for item in request.filters] == ['dealer.dealer_code']
    assert [item['field'] for item in request._planner_extraction.structured['过滤条件']] == ['dealer.dealer_code']


def test_prose_reference_mixed_with_explicit_literal_keeps_the_literal():
    request = request_for(['依赖任务1输出的经销商名称列表', '明确指定的经销商'], operator='IN')
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert request.filters[0]['field'] == '经销商名称'
    assert request.filters[0]['value'] == ['明确指定的经销商']


def test_task_placeholder_mixed_with_explicit_literal_keeps_the_literal():
    request = request_for(['${task_0.经销商名称}', '明确指定的经销商'], operator='IN')
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert [item['field'] for item in request.filters] == ['经销商名称', 'dealer.dealer_code']
    assert request.filters[0]['value'] == ['明确指定的经销商']


@pytest.mark.parametrize('overrides', [
    {'value': '${task_0.医院名称}', 'field': '医院名称'},
    {'value': '${task_0.经销商名称}', 'question': '仅查询名称为“${task_0.经销商名称}”的经销商产品'},
    {'value': '${unknown}', 'field': '经销商名称'},
])
def test_task_placeholder_outside_bound_scope_is_preserved(overrides):
    request = request_for(**overrides)
    original = request.filters[0].copy()
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    assert original in request.filters


@pytest.mark.parametrize('lose_keys', [False, True])
def test_completeness_proves_reference_resolution_without_weakening_literal_checks(lose_keys):
    request = request_for('复用上一轮查询结果：2025年最高销售额经销商名称')
    request.turn_admission = TurnAdmissionGate().evaluate(question=request.original_question,
        current=request, previous=None, message_id='dependent-task')
    original_facts = request.turn_admission.current_turn_facts.model_copy(deep=True)
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    contract = build_intent_asl_contract(request)
    if lose_keys:
        contract['filters'] = []
    errors = validate_intent_asl_contract_completeness(contract, request)
    assert bool(errors) == lose_keys
    assert request.turn_admission.current_turn_facts == original_facts


def test_completeness_still_rejects_lost_user_literal_with_bound_dependency():
    request = request_for('明确指定的经销商')
    request.turn_admission = TurnAdmissionGate().evaluate(question=request.original_question,
        current=request, previous=None, message_id='literal-task')
    HttpDataRetrievalAdapter._merge_dependency_filters(request)
    request.filters = request.filters[1:]
    assert validate_intent_asl_contract_completeness(build_intent_asl_contract(request), request)


@pytest.mark.parametrize('period,expected', [('2025年', 'REQUIRED'), (None, 'FORBIDDEN')])
def test_child_declared_period_does_not_conflict_with_stale_all_history_marker(period, expected):
    request = request_for('明确指定的经销商')
    request.assumptions = ['TIME_SCOPE=ALL_TIME', 'KEEP_EXISTING_BEHAVIOR']
    request._planner_extraction.structured['时间粒度']['time_range'] = period
    token = _INTERNAL_ASSUMPTIONS.set(('DAG_BOUND_STRUCTURED_QUERY',))
    try:
        DataAnalysisOrchestrator._apply_dag_structured_query_shape(request, request._planner_extraction)
    finally:
        _INTERNAL_ASSUMPTIONS.reset(token)
    assert build_intent_asl_contract(request)['time_policy'] == expected
    assert 'KEEP_EXISTING_BEHAVIOR' in request.assumptions
