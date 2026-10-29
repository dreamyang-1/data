"""Remote drift regressions: recover catalog bindings, never alter query shape."""
import json
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest

from structured_binding import bind
from test_structured_binding import dealer_list_fixture, fixture, run


@pytest.mark.parametrize('choice', [None, [], [{'index': 0, 'error': 'omitted by model'}]])
def test_missing_filter_plan_is_recovered_before_subject_resolution(choice):
    from agent import _detail_projection_subject_candidate
    extraction, knowledge, plan = dealer_list_fixture()
    plan['filters'] = choice
    before = deepcopy(extraction)
    ast, _ = bind(extraction, knowledge, Obj(invoke=lambda _: Obj(content=json.dumps(plan))),
                  detail_subject_resolver=_detail_projection_subject_candidate)
    assert not ast['ambiguity']
    assert ast['subject'] == {'entity': 'sales_order'}
    assert ast['filters'] == [{'field': 'product.product_name', 'operator': '=',
                               'value': '空心纤维血液透析器'}]
    assert ast['time_context'] is None and ast['metrics'] == []
    assert extraction == before


def test_partial_display_does_not_prevent_catalog_filter_recovery():
    extraction, knowledge, plan = fixture()
    extraction.update(指标=[], 展示字段=[{'field': '医院名称'}, {'field': '联系方式'}])
    plan.update(metrics=[], filters=[], display_fields=[
        {'index': 0, 'key': 'hospital.name'}, {'index': 1, 'error': 'no catalog field'}])
    ast, repairs = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'hospital.province', 'operator': '=', 'value': '上海市'}]
    assert [d['name'] for d in ast['dimensions']] == ['hospital.name']
    assert any(r['type'] == 'OMIT_UNAVAILABLE_DISPLAY_FIELD' for r in repairs)


@pytest.mark.parametrize('values,valid', [(['上海', '北京'], True),
                                        (['上海', '未收录地区'], False),
                                        (['上海', None], False)])
@pytest.mark.parametrize('model_choice', ['missing', 'bad_id', 'error'])
def test_declared_field_fallback_never_drops_any_in_value(values, valid, model_choice):
    extraction, knowledge, plan = fixture()
    extraction['过滤条件'] = [{'field': '省份', 'op': 'IN', 'value': values}]
    # Same literal occurs in two fields; the declared role resolves the tie.
    knowledge['entity_attribute_values'] = [Obj(metadata={
        'source_field': field, 'attr_name': role, 'attr_value': value})
        for field, role in [('hospital.province', '省份'), ('hospital.name', '城市')]
        for value in ['上海市', '北京市']]
    plan['filters'] = [] if model_choice == 'missing' else [
        {'index': 0, 'key': 'hospital.province', 'value_ids': [999]}]
    if model_choice == 'error':
        plan['filters'] = [{'index': 0, 'error': 'no binding'}]
    ast, _ = run(extraction, knowledge, plan)
    assert bool(ast['ambiguity']) is not valid
    if valid:
        assert ast['filters'] == [{'field': 'hospital.province', 'operator': 'IN',
                                   'value': ['上海市', '北京市']}]
    else:
        assert ast['filters'] == []


def test_ambiguous_or_unauthorized_catalog_value_cannot_repair_filter():
    extraction, knowledge, plan = fixture()
    extraction['过滤条件'][0]['field'] = '地域'
    plan['filters'] = []
    knowledge['entity_attribute_values'].append(Obj(metadata={
        'source_field': 'hospital.name', 'attr_value': '上海市'}))
    ast, _ = run(extraction, knowledge, plan)
    assert ast['ambiguity'] and not ast['filters']
    knowledge['_vector_authorized_fields'] = ['hospital.amount']
    ast, _ = run(extraction, knowledge, plan)
    assert ast['ambiguity'] and not ast['filters']


@pytest.mark.parametrize('extra', ['metrics', 'dimensions', 'display_fields', 'filters', 'sort'])
def test_empty_sections_ignore_model_invention_with_audit(extra):
    extraction, knowledge, plan = fixture()
    names = dict(metrics='指标', dimensions='维度', display_fields='展示字段',
                 filters='过滤条件', sort='排序')
    extraction[names[extra]] = []
    if extra == 'metrics':
        extraction['展示字段'] = [{'field': '医院名称'}]
        plan['display_fields'] = [{'index': 0, 'key': 'hospital.name'}]
    plan[extra] = [{'index': 0, 'key': 'hospital.name'}]
    ast, repairs = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert ast['time_context'] is None
    assert any(r['type'] == 'IGNORED_UNDECLARED_BINDINGS' and r['slot'] == names[extra]
               for r in repairs)
    if extra == 'metrics':
        assert not ast['metrics']
    elif extra in {'dimensions', 'display_fields'}:
        assert not ast['dimensions']
    else:
        assert not ast[extra]


@pytest.mark.parametrize('subject', [None, 'hospital', 'sales_order'])
def test_metric_source_subject_is_stable_with_extra_involved_entities(subject):
    extraction, knowledge, plan = fixture()
    extraction['实体'] = ['医院', '经销商', '销售订单']
    knowledge['entities'].append(Obj(metadata={'entity_code': 'sales_order', 'attributes': []}))
    knowledge['metrics'][0].metadata['source_dependency'] = {'bind_entity': ['sales_order']}
    plan['subject'] = subject
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity'] and ast['subject'] == {'entity': 'sales_order'}
    assert ast['dimensions'] == []


def test_source_binding_cannot_introduce_unauthorized_subject():
    extraction, knowledge, plan = fixture()
    knowledge['metrics'][0].metadata['source_dependency'] = {'bind_entity': ['other_model_order']}
    ast, _ = run(extraction, knowledge, plan)
    assert ast['subject'] == {'entity': 'hospital'}


def test_exact_value_wins_over_wrong_same_field_model_candidate():
    extraction, knowledge, plan = fixture()
    knowledge['entity_attribute_values'].append(Obj(metadata={
        'source_field': 'hospital.province', 'attr_value': '北京市'}))
    plan['filters'][0]['value_ids'] = [1]
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity'] and ast['filters'][0]['value'] == '上海市'


def test_wrong_field_value_id_is_not_a_semantic_name_selection():
    extraction, knowledge, plan = fixture()
    extraction['过滤条件'] = [{'field': '分类', 'op': '=', 'value': ['透析器']}]
    knowledge['entity_attribute_values'] = [Obj(metadata={
        'source_field': 'hospital.name', 'attr_value': '01透析器具'})]
    plan['filters'] = [{'index': 0, 'key': 'hospital.province', 'value_ids': [0]}]
    ast, _ = run(extraction, knowledge, plan)
    assert ast['ambiguity'] and ast['filters'] == []


def test_remote_enum_and_dimension_physical_binding_are_preserved():
    extraction, knowledge, plan = fixture()
    knowledge['dimensions'] = [Obj(metadata={
        'dim_code': 'channel', 'dim_name': '渠道业态', 'field_mapping': 'hospital.province',
        'enum_list': [{'code': '3', 'name': '直销'}, {'code': '5', 'name': '分销'}]})]
    extraction['过滤条件'] = [{'field': '渠道业态', 'op': 'IN', 'value': ['直销', '分销']}]
    plan['filters'] = [{'index': 0, 'key': 'channel', 'value_ids': [999]}]
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'hospital.province', 'operator': 'IN', 'value': ['3', '5']}]


def test_remote_metric_threshold_stays_having_without_new_filters():
    extraction, knowledge, plan = fixture()
    knowledge['metrics'][0].metadata['calculation_rule'] = {'calc_formula': 'count_hospital = COUNT(DISTINCT hospital.code)'}
    extraction['过滤条件'] = [{'field': '区域全部医院总数', 'op': '>', 'value': [3]}]
    plan['filters'] = [{'index': 0, 'key': 'count_hospital'}]
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity'] and ast['filters'] == []
    assert ast['having'] == ['COUNT(DISTINCT hospital.code) > 3']


def test_remote_literal_date_filter_stays_deterministic():
    extraction, knowledge, plan = fixture()
    extraction['过滤条件'] = [{'field': '日期', 'op': '=', 'value': ['2025年7月']}]
    plan['filters'] = [{'index': 0, 'key': 'hospital.date'}]
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'hospital.date', 'operator': 'BETWEEN',
                               'value': ['2025-07-01', '2025-07-31']}]
    assert ast['time_context'] is None and ast['dimensions'] == []
