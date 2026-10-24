import json

import pytest

from test_explicit_filter_owner import resolve
from test_structured_binding import fixture, run


@pytest.mark.parametrize('owner,field', [('医院', 'hospital.province_id'), ('经销商', 'dealer.province_id')])
def test_explicit_filter_owner_does_not_require_duplicate_top_level_entity(owner, field):
    ast, _, _, _ = resolve({'实体': ['销售订单'], '过滤条件': [
        {'entity': owner, 'field': '省份', 'op': '=', 'value': ['上海市']}
    ]}, resolver=lambda *args: ['310000'])
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': field, 'operator': '=', 'value': '310000'}]


def test_source_entity_with_dictionary_label_reuses_field_binders_owner():
    ast, _, _, _ = resolve({'实体': ['销售订单'], '过滤条件': [
        {'entity': '销售订单', 'field': '省份', 'op': '=', 'value': ['上海市']}
    ]})
    assert not ast['ambiguity']
    assert ast['filters'][0]['field'] == 'dim_province.province_name'


@pytest.mark.parametrize('label,unit', [('月份', '月'), ('季度', '季度'), ('年度', '年')])
def test_grain_projection_and_sort_bind_to_the_same_governed_date(label, unit):
    e, k, p = fixture()
    e['维度'] = [label]
    e['排序'] = [{'field': label, 'order': 'asc'}]
    e['时间粒度'] = {'unit': unit, 'time_range': '2025年', 'anchor': None}
    k['metrics'][0].metadata['time_caliber'] = {'time_anchor': 'hospital.date'}
    p['dimensions'] = [{'index': 0, 'error': '没有独立月份字段'}]
    p['sort'] = [{'index': 0, 'error': '没有独立月份字段'}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['name'] == ast['sort']['field'] == 'hospital.date'
    assert ast['dimensions'][0]['granularity'] in {'month', 'quarter', 'year'}
    assert ast['time_context']['start'] == '2025-01-01'


def test_time_range_without_grouping_stays_scalar():
    e, k, p = fixture()
    e['时间粒度'] = {'unit': '月', 'time_range': '2025年12月', 'anchor': None}
    k['metrics'][0].metadata['time_caliber'] = {'time_anchor': 'hospital.date'}
    ast, _ = run(e, k, p)
    assert not ast['ambiguity'] and ast['dimensions'] == [] and ast['sort'] is None
