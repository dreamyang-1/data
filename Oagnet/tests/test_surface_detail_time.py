"""A corrected row query must not retain a model's last_month placeholder."""
from copy import deepcopy
from datetime import date
import json
from types import SimpleNamespace

import pytest
import agent
from test_surface_literal_constraints import catalog, extraction
from test_intent_asl_contract import _detail_ast


class Today(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 23)


def draft():
    ast = json.loads(_detail_ast())
    ast['subject'] = {'entity': 'sales_order'}
    ast['dimensions'] = [{'name': 'sales_order.order_code'}]
    ast['filters'] = [{'field': 'sales_order.amount_with_tax', 'operator': '>', 'value': 1000}]
    ast['time_context'] = {'type': 'last_month', 'unit': 'month', 'anchor': 'sales_order.created_date'}
    ast['ambiguity'] = [{'type': 'time_anchor', 'question': '最近一年没有类型，last_month占位，请确认时间。',
                         'candidates': ['sales_order.created_date', 'business_date']}]
    return ast


@pytest.mark.parametrize('phrase', ['最近一年', '近1年', '过去一年', '近12个月'])
def test_recent_year_repairs_placeholder_and_only_resolved_time_question(monkeypatch, phrase):
    monkeypatch.setattr(agent, 'date', Today)
    ast = draft()
    other = {'type': 'filter', 'question': '请确认日期相关产品型号', 'candidates': ['A', 'B']}
    mixed = {'type': 'time_anchor', 'question': '日期及金额', 'affected_slots': ['time_range', 'filters']}
    ast['ambiguity'].extend([other, mixed])
    content, repairs = agent._repair_surface_detail_time(json.dumps(ast), catalog(), f'查询{phrase}单笔金额大于1000的订单')
    result = json.loads(content)
    assert result['time_context'] == {'type': 'range', 'start': '2025-09-23', 'end': '2026-09-23',
                                     'value': None, 'unit': 'day', 'anchor': 'sales_order.created_date'}
    assert result['ambiguity'] == [other, mixed]
    assert result['filters'] == ast['filters']
    assert result['dimensions'] == ast['dimensions']
    assert result['metrics'] == []
    assert repairs[0]['type'] == 'RESOLVE_DETAIL_TIME_RANGE'
    agent._validate_vector_grounded_asl(content, catalog())


def test_explicit_dates_preserve_requested_range():
    content, _ = agent._repair_surface_detail_time(json.dumps(draft()), catalog(),
        '查询2026-01-01至2026-03-31单笔金额大于1000的订单')
    context = json.loads(content)['time_context']
    assert (context['start'], context['end'], context['type']) == ('2026-01-01', '2026-03-31', 'custom')


@pytest.mark.parametrize('phrase,start,end', [
    ('2025年', '2025-01-01', '2025-12-31'),
    ('2024年2月', '2024-02-01', '2024-02-29'),
])
def test_calendar_period_end_is_inclusive_not_next_period(phrase, start, end):
    content, _ = agent._repair_surface_detail_time(json.dumps(draft()), catalog(), phrase + '订单')
    context = json.loads(content)['time_context']
    assert (context['start'], context['end']) == (start, end)


def test_unrelated_entity_date_cannot_authorize_subject_time():
    knowledge = catalog()
    attributes = knowledge['entities'][0].metadata['attributes']
    attributes[:] = [a for a in attributes if a['data_type'] != 'date']
    knowledge['entities'][1].metadata['attributes'].append({
        'attr_code': 'created_date', 'attr_name': '创建日期', 'data_type': 'date',
        'field_mapping': 'product.created_date'})
    ast = draft()
    ast['time_context']['anchor'] = 'product.created_date'
    content, _ = agent._repair_surface_detail_time(json.dumps(ast), knowledge, '最近一年的订单')
    assert json.loads(content)['time_context'] is None
    assert json.loads(content)['ambiguity']


@pytest.mark.parametrize('missing', [True, False])
def test_missing_or_competing_date_fields_still_ask_specific_anchor(missing):
    knowledge = catalog()
    attributes = knowledge['entities'][0].metadata['attributes']
    if missing:
        attributes[:] = [a for a in attributes if a['data_type'] != 'date']
    else:
        attributes.append({'attr_code': 'paid_date', 'attr_name': '付款日期', 'data_type': 'date',
                           'field_mapping': 'sales_order.paid_date'})
    content, _ = agent._repair_surface_detail_time(json.dumps(draft()), knowledge, '最近一年的订单')
    result = json.loads(content)
    assert result['time_context'] is None
    assert result['ambiguity'][0]['type'] == 'time_anchor'
    assert 'last_month' not in result['ambiguity'][0]['question']
    assert result['ambiguity'][0]['candidates'] == ([] if missing else [
        'sales_order.created_date', 'sales_order.paid_date'])


def test_explicit_date_role_resolves_competing_fields():
    knowledge = catalog()
    knowledge['entities'][0].metadata['attributes'].append({
        'attr_code': 'paid_date', 'attr_name': '付款日期', 'data_type': 'date',
        'field_mapping': 'sales_order.paid_date'})
    content, _ = agent._repair_surface_detail_time(json.dumps(draft()), knowledge, '按付款日期查询最近一年的订单')
    assert json.loads(content)['time_context']['anchor'] == 'sales_order.paid_date'


@pytest.mark.parametrize('question', ['最近半年订单', '上个月订单', '查询订单'])
def test_unhandled_expressions_preserve_existing_model_time(question):
    content = json.dumps(draft())
    assert agent._repair_surface_detail_time(content, catalog(), question) == (content, [])


def test_aggregate_shape_is_not_rewritten():
    ast = draft()
    ast['metrics'] = [{'name': 'sales_total_including_tax'}]
    content = json.dumps(ast)
    assert agent._repair_surface_detail_time(content, catalog(), '最近一年销售趋势') == (content, [])


@pytest.mark.parametrize('all_time', [False, True])
def test_main_restores_detail_then_repairs_time_without_catalog_or_model_io(monkeypatch, all_time):
    monkeypatch.setattr(agent, 'date', Today)
    knowledge = catalog()
    raw = extraction(operator='>')
    ast = draft()
    ast['metrics'] = [{'name': 'sales_total_including_tax'}]
    if all_time:
        # Preserve a correctly generated all-history request, even if an older
        # completed-question surface still contains a recent-year phrase.
        ast['time_context'] = None
        ast['ambiguity'] = []
    class Builder:
        def __init__(self, *a, **k): self.last_knowledge = deepcopy(knowledge)
        def build(self, query): return 'scoped catalog'
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: object())
    monkeypatch.setattr(agent, 'create_deep_agent', lambda **k: SimpleNamespace(
        invoke=lambda _: {'messages': [SimpleNamespace(content=json.dumps(ast))]}))
    monkeypatch.setattr(agent, '_normalize_dynamic_subject', lambda value, *a: value)
    query = '查询最近一年单笔销售额大于1000的订单'
    result = json.loads(agent.main(query + ('\nTIME_SCOPE=ALL_TIME' if all_time else ''),
        completed_question=query, store=object(),
        semantic_model_id=81, business_domain_ids=[205], surface_evidence={'mentions': [{'text':'1000'}]},
        structured_extraction=raw))
    assert result['metrics'] == []
    assert result['filters'] == ast['filters']
    if all_time:
        assert result['time_context'] is None
    else:
        assert result['time_context']['type'] == 'range'
        assert result['time_context']['anchor'] == 'sales_order.created_date'
        assert result['time_context']['start'] == '2025-09-23'
    assert result['ambiguity'] == []
