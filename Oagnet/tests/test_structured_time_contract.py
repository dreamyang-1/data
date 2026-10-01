import json
from copy import deepcopy
from datetime import date
from types import SimpleNamespace as Obj

import pytest
from structured_binding import bind
from test_structured_binding import fixture
from structured_time import bounds

TODAY = date(2026, 9, 30)


@pytest.mark.parametrize('value,start,end', [
    ('2026年第一季度', '2026-01-01', '2026-03-31'),
    ('2025年第四季度', '2025-10-01', '2025-12-31'),
    ('2024年第1季度', '2024-01-01', '2024-03-31'),
    ('2026Q2', '2026-04-01', '2026-06-30'),
    ('2025-Q3', '2025-07-01', '2025-09-30'),
    ('2025年', '2025-01-01', '2025-12-31'),
    ('2024年2月', '2024-02-01', '2024-02-29'),
    ('2025-02', '2025-02-01', '2025-02-28'),
    ('2025年12月31日', '2025-12-31', '2025-12-31'),
    ('2025-10-01 至 2025-12-31', '2025-10-01', '2025-12-31'),
    ('2025年第一季度至2025年第四季度', '2025-01-01', '2025-12-31'),
    (['2025-10-01', '2025-12-31'], '2025-10-01', '2025-12-31'),
    ({'start': '2025-10-01', 'end': '2025-12-31'}, '2025-10-01', '2025-12-31'),
    ({'start': '2025-10-01', 'end_exclusive': '2026-01-01'}, '2025-10-01', '2025-12-31'),
    ('近6个月', '2026-03-30', '2026-09-30'),
    ('最近十二个月', '2025-09-30', '2026-09-30'),
    ('最近半年', '2026-03-30', '2026-09-30'),
    ('过去两个季度', '2026-03-30', '2026-09-30'),
    ('本季度', '2026-07-01', '2026-09-30'),
    ('上季度', '2026-04-01', '2026-06-30'),
    ('2025年下半年', '2025-07-01', '2025-12-31'),
    ('本周', '2026-09-28', '2026-10-04'),
    ('上月', '2026-08-01', '2026-08-31'),
    ('去年', '2025-01-01', '2025-12-31'),
    ('昨天', '2026-09-29', '2026-09-29'),
])
def test_declared_period_bounds(value, start, end):
    assert bounds(value, TODAY) == (start, end)


@pytest.mark.parametrize('value', ['2026年第五季度', '2025-02-30', '2026-12-01至2026-01-01',
                                  '财年', '最近0个月', '2025年及2026年', True, {}])
def test_invalid_or_ambiguous_period_never_becomes_model_dates(value):
    e, k, p = fixture()
    e['时间粒度']['time_range'] = value
    p['time'] = {'anchor': 'hospital.date', 'mode': 'range', 'start': '2025-01-01', 'end': '2025-12-31'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))), today=TODAY)
    assert ast['ambiguity'] and ast['time_context'] is None


@pytest.mark.parametrize('decision', [None, {}, {'anchor': 'not.authorized'},
    {'anchor': 'hospital.date', 'mode': 'none'},
    {'anchor': 'hospital.date', 'mode': 'range', 'start': '2025-01-01', 'end': '2025-12-31'},
    {'anchor': 'business_date', 'mode': 'calendar', 'type': 'this_year'},
    {'anchor': 'hospital.amount', 'mode': 'rolling', 'amount': 12, 'unit': 'month'},
    {'error': '未匹配到授权的时间字段'},
])
@pytest.mark.parametrize('period,start,end', [('2026年第一季度', '2026-01-01', '2026-03-31'),
                                           ('2025年第四季度', '2025-10-01', '2025-12-31')])
def test_time_comes_from_structure_even_if_model_omits_or_changes_it(decision, period, start, end):
    e, k, p = fixture()
    e['时间粒度'] = {'unit': '季度', 'time_range': period}
    p['time'] = decision
    before = deepcopy(e)
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))), today=TODAY)
    assert not ast['ambiguity']
    assert ast['time_context'] == {'type': 'range', 'start': start, 'end': end, 'unit': 'day', 'anchor': 'hospital.date'}
    assert ast['dimensions'] == []
    assert e == before


@pytest.mark.parametrize('source_grain,expected', [(None, None), ('月', 'month'), ('季度', 'quarter'), ('YEAR', 'year'), ('周', 'week')])
@pytest.mark.parametrize('model_grain', [None, 'day', '季度', 'nonsense', {'unit': 'month'}])
def test_model_has_no_grain_write_authority(source_grain, expected, model_grain):
    e, k, p = fixture()
    e['维度'] = ['日期']
    e['时间粒度']['unit'] = source_grain
    p['dimensions'] = [{'index': 0, 'key': 'hospital.date', 'granularity': model_grain}]
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['granularity'] == expected
    assert ast['time_context'] is None


def test_non_time_dimension_and_detail_date_do_not_acquire_model_grain():
    for metric_query in [True, False]:
        e, k, p = fixture()
        target = 'dimensions' if metric_query else 'display_fields'
        source = '维度' if metric_query else '展示字段'
        e[source] = ['医院名称' if metric_query else '日期']
        e['时间粒度']['unit'] = '季度'
        if not metric_query:
            e['指标'] = []
            p['metrics'] = []
        p[target] = [{'index': 0, 'key': 'hospital.name' if metric_query else 'hospital.date', 'granularity': 'quarter'}]
        ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
        assert not ast['ambiguity']
        assert ast['dimensions'][0]['granularity'] is None


def test_time_dimension_binding_can_supply_authorized_field_missing_from_attribute_topk():
    e, k, p = fixture()
    k['entities'][0].metadata['attributes'].pop()
    k['dimensions'] = [Obj(metadata={'dim_code': 'business_date', 'dim_type': '时间', 'bind_entities': [
        {'mappingTable': 'hospital', 'mappingColumn': 'date'}]})]
    e['时间粒度']['time_range'] = '2026年第一季度'
    p['time'] = {'anchor': 'business_date'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert not ast['ambiguity']
    assert ast['time_context']['anchor'] == 'hospital.date'
    k['_vector_authorized_fields'].remove('hospital.date')
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert ast['ambiguity'] and ast['time_context'] is None


def test_declared_metric_anchor_wins_over_unrelated_model_date():
    e, k, p = fixture()
    e['时间粒度']['time_range'] = '2025年第四季度'
    k['metrics'][0].metadata['time_caliber'] = {'time_anchor': 'hospital.date'}
    k['entities'][0].metadata['attributes'].append({'attr_name': '更新日期', 'field_mapping': 'hospital.updated_date', 'data_type': 'DATE'})
    k['_vector_authorized_fields'].append('hospital.updated_date')
    p['time'] = {'anchor': 'hospital.updated_date'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert not ast['ambiguity'] and ast['time_context']['anchor'] == 'hospital.date'


def test_missing_catalog_time_does_not_use_unrelated_entity_timestamp():
    e, k, p = fixture()
    e['时间粒度']['time_range'] = '2026年第一季度'
    k['entities'][0].metadata['attributes'].pop()
    k['_vector_authorized_fields'].remove('hospital.date')
    k['entities'].append(Obj(metadata={'entity_code': 'unrelated', 'attributes': [
        {'attr_name': '创建日期', 'field_mapping': 'unrelated.date'}]}))
    k['_vector_authorized_fields'].append('unrelated.date')
    p['time'] = {'anchor': 'unrelated.date'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert ast['ambiguity'] and ast['time_context'] is None
    assert '时间范围已经明确' in ast['ambiguity'][0]['question']


@pytest.mark.parametrize('period', ['2026年第一季度', '2025年第四季度', {'start': '2026-01-01', 'end': '2026-03-31'}, None])
def test_time_recall_reads_structured_slot_not_words(period):
    from prompt_build import PromptBuilder
    text = json.dumps({'指标': [], '时间粒度': {'unit': None, 'time_range': period}}, ensure_ascii=False)
    assert PromptBuilder._requires_time_dimension_recall(text) is (period is not None)


def test_metric_name_does_not_create_time_recall_requirement():
    from prompt_build import PromptBuilder
    assert not PromptBuilder._requires_time_dimension_recall(json.dumps({
        '指标': [{'name': '最近一年销售额'}], '时间粒度': {'unit': None, 'time_range': None}}, ensure_ascii=False))


def test_declared_quarter_loads_time_dimension_outside_topk():
    from prompt_build import PromptBuilder
    from test_temporal_metric_invariants import _result, _metric, _ScopedTimeDimensionStore
    dims = [_result('dimension', 'dealer', dim_name='经销商'),
            _result('dimension', 'business_date', dim_name='业务日期', dim_type='时间维度',
                    bind_entities=[{'mappingTable': 'sales_order', 'mappingColumn': 'created_date'}])]
    builder = PromptBuilder(_ScopedTimeDimensionStore([_metric('sales_total', '销售额', [])], dims),
                            lambda _: [0.1], top_k=1, semantic_model_id=81)
    knowledge = builder.retrieve(json.dumps({'实体': ['经销商', '商品'], '时间粒度': {
        'unit': None, 'time_range': '2026年第一季度'}}, ensure_ascii=False))
    assert 'business_date' in {d.metadata['dim_code'] for d in knowledge['dimensions']}


def test_followup_changes_period_without_reusing_previous_model_dates():
    e, k, p = fixture()
    e['维度'] = ['日期']
    e['时间粒度'] = {'unit': '月', 'time_range': '2026年第一季度'}
    p['dimensions'] = [{'index': 0, 'key': 'hospital.date', 'granularity': 'quarter'}]
    p['time'] = {'anchor': 'hospital.date', 'mode': 'range', 'start': '2025-10-01', 'end': '2025-12-31'}
    for period, start, end in [('2026年第一季度', '2026-01-01', '2026-03-31'),
                              ('2025年第四季度', '2025-10-01', '2025-12-31')]:
        e['时间粒度']['time_range'] = period
        ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
        assert not ast['ambiguity']
        assert ast['dimensions'][0]['granularity'] == 'month'
        assert (ast['time_context']['start'], ast['time_context']['end']) == (start, end)


def test_other_parameter_values_are_not_taken_from_model():
    e, k, p = fixture()
    e['限制'] = 3
    e['过滤条件'] = [{'field': '金额', 'op': '>', 'value': [100]}]
    e['排序'] = [{'field': '医院总数', 'order': 'desc'}]
    p.update(limit=99, time={'mode': 'calendar', 'type': 'this_year', 'anchor': 'hospital.date'},
             filters=[{'index': 0, 'key': 'hospital.amount', 'value': -1, 'operator': '<'}],
             sort=[{'index': 0, 'key': 'count_hospital', 'direction': 'ASC'}])
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert not ast['ambiguity']
    assert ast['limit'] == 3 and ast['time_context'] is None
    assert ast['filters'] == [{'field': 'hospital.amount', 'operator': '>', 'value': 100}]
    assert ast['sort']['direction'] == 'DESC'
