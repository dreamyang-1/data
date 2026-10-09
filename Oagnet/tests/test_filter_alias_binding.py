"""Entity aliases and dimension labels must retain their governed field owner."""
import json
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest

from structured_binding import bind


def fixture(*, alias=True, name_binding=True):
    entities = []
    for code, name, attributes in [
        ('department', '科室', [
            ('dept_code', '科室编码', False), ('dept_name', '科室名称', True)]),
        ('product_dept_relation', '商品科室关联', [('dept_code', '科室编码', False)]),
        ('product_line', '产品线', [
            ('line_level1', '产品线一级分类', False), ('line_level2', '产品线二级分类', False)]),
        ('hospital', '医院', [('hospital_name', '医院名称', True)]),
        ('sales_order', '销售订单', []),
    ]:
        entities.append(Obj(metadata={'entity_code': code, 'entity_id': code + '-id',
            'entity_name': name, 'entity_alias': ['适用科室', '临床科室'] if code == 'department' and alias else [],
            'attributes': [{'attr_code': column, 'attr_name': label,
                'field_mapping': f'{code}.{column}', 'is_main_attribute': main}
                for column, label, main in attributes]}))
    mappings = [{'entity': code + '-id', 'mappingTable': code, 'mappingColumn': 'dept_code'}
                for code in ('department', 'product_dept_relation')]
    if name_binding:
        mappings.append({'entity': 'department-id', 'mappingTable': 'department', 'mappingColumn': 'dept_name'})
    values = [Obj(metadata={'entity_code': code, 'attr_code': column,
              'source_table': code, 'source_field': column, 'attr_value': value})
              for code, column in [('department', 'dept_name'),
                  ('product_line', 'line_level1'), ('product_line', 'line_level2')]
              for value in ['消化内科', '呼吸内科']]
    knowledge = {'entities': entities, 'metrics': [Obj(metadata={'metric_code': 'order_count',
                 'metric_name': '订单笔数', 'source_dependency': {'bind_entity': ['sales_order']}})],
                 'dimensions': [Obj(metadata={'dim_code': 'applicable_department',
                     'dim_name': '适用科室', 'bind_entities': mappings})],
                 'entity_attribute_values': values,
                 '_vector_authorized_fields': [a['field_mapping'] for e in entities for a in e.metadata['attributes']]}
    extraction = {'实体': ['医院', '销售订单', '科室'], '指标': [{'name': '订单笔数'}],
        '维度': ['医院名称'], '展示字段': [],
        '过滤条件': [{'field': '适用科室', 'op': '=', 'value': ['消化内科']}],
        '排序': [{'field': '订单笔数', 'order': 'desc'}],
        '时间粒度': {'unit': None, 'time_range': None}, '限制': 1, '输出要求': '默认输出表格'}
    plan = {'subject': 'sales_order', 'metrics': [{'index': 0, 'key': 'order_count'}],
            'dimensions': [{'index': 0, 'key': 'hospital.hospital_name'}],
            'filters': [{'index': 0, 'key': 'department.dept_name'}],
            'sort': [{'index': 0, 'key': 'order_count'}]}
    return extraction, knowledge, plan


def run(extraction, knowledge, plan):
    return bind(extraction, knowledge, Obj(invoke=lambda _: Obj(content=json.dumps(plan))))


@pytest.mark.parametrize('alias,name_binding', [(False, False), (True, True)])
@pytest.mark.parametrize('model_choice', ['correct', 'dimension', 'wrong_owner', 'missing', 'error'])
def test_same_dimension_label_does_not_override_unique_governed_value_field(alias, name_binding, model_choice):
    e, k, p = fixture(alias=alias, name_binding=name_binding)
    if model_choice == 'dimension':
        p['filters'][0]['key'] = 'applicable_department'
    elif model_choice == 'wrong_owner':
        p['filters'][0]['key'] = 'product_line.line_level1'
    elif model_choice == 'missing':
        p['filters'] = []
    elif model_choice == 'error':
        p['filters'][0] = {'index': 0, 'error': '无法匹配字段'}
    before = deepcopy(e)
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == []
    assert ast['filters'] == [{'field': 'department.dept_name', 'operator': '=', 'value': '消化内科'}]
    assert e == before
    assert ast['metrics'][0]['name'] == 'order_count'
    assert ast['dimensions'][0]['name'] == 'hospital.hospital_name'
    assert ast['sort']['direction'] == 'DESC' and ast['limit'] == 1
    assert ast['time_context'] is None


@pytest.mark.parametrize('label', ['科室', '适用科室', '临床科室'])
def test_entity_name_or_alias_restricts_value_recovery_to_that_entity(label):
    e, k, p = fixture()
    k['dimensions'] = []
    e['过滤条件'][0]['field'] = label
    p['filters'] = []
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == []
    assert ast['filters'][0]['field'] == 'department.dept_name'


@pytest.mark.parametrize('operator', ['=', 'IN', '!=', 'NOT IN'])
def test_alias_multi_value_filter_keeps_all_values_and_operator(operator):
    e, k, p = fixture()
    e['过滤条件'][0].update(op=operator, value=['消化内科', '呼吸内科'])
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == []
    assert ast['filters'] == [{'field': 'department.dept_name',
        'operator': 'NOT IN' if operator in {'!=', 'NOT IN'} else 'IN',
        'value': ['消化内科', '呼吸内科']}]


def test_unknown_member_never_executes_the_matched_part_of_in():
    e, k, p = fixture()
    e['过滤条件'][0].update(op='IN', value=['消化内科', '不存在的科室'])
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and ast['filters'] == []


def test_shared_alias_cannot_be_selected_by_model_rank():
    e, k, p = fixture()
    k['dimensions'] = []
    k['entities'][2].metadata['entity_alias'] = ['适用科室']
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and ast['filters'] == []


def test_explicit_parameter_entity_resolves_a_shared_alias():
    e, k, p = fixture()
    k['entities'][2].metadata['entity_alias'] = ['适用科室']
    e['过滤条件'][0]['entity'] = '科室'
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == [] and ast['filters'][0]['field'] == 'department.dept_name'


def test_multiple_dimension_owners_with_same_value_still_need_disambiguation():
    e, k, p = fixture(alias=False)
    k['dimensions'][0].metadata['bind_entities'].append(
        {'entity': 'product_line-id', 'mappingTable': 'product_line', 'mappingColumn': 'line_level1'})
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and ast['filters'] == []


def test_attribute_exact_match_wins_over_an_unrelated_entity_alias():
    e, k, p = fixture()
    k['entities'][2].metadata['entity_alias'] = ['科室名称']
    e['过滤条件'][0]['field'] = '科室名称'
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == [] and ast['filters'][0]['field'] == 'department.dept_name'


def test_alias_display_field_cannot_return_another_entity_attribute():
    e, k, p = fixture()
    e.update(指标=[], 维度=[], 展示字段=[{'field': '适用科室'}], 过滤条件=[], 排序=[])
    p.update(metrics=[], dimensions=[], filters=[], sort=[],
        display_fields=[{'index': 0, 'key': 'product_line.line_level1'}])
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and ast['dimensions'] == []


def test_unrecalled_or_unauthorized_name_field_is_not_created_from_an_alias():
    e, k, p = fixture()
    k['_vector_authorized_fields'].remove('department.dept_name')
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and ast['filters'] == []


def test_registered_code_stays_a_code_not_a_similar_name():
    e, k, p = fixture()
    e['过滤条件'][0]['value'] = ['DEP0044']
    k['entity_attribute_values'].append(Obj(metadata={'entity_code': 'department',
        'attr_code': 'dept_code', 'source_field': 'department.dept_code', 'attr_value': 'DEP0044'}))
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == []
    assert ast['filters'] == [{'field': 'department.dept_code', 'operator': '=', 'value': 'DEP0044'}]


def test_canonical_name_beats_an_identifier_display_label_without_changing_value():
    e, k, p = fixture()
    k['entity_attribute_values'].append(Obj(metadata={'entity_code': 'department',
        'attr_code': 'dept_code', 'source_field': 'department.dept_code',
        'attr_value': 'DEP0044', 'label': '消化内科'}))
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == []
    assert ast['filters'] == [{'field': 'department.dept_name', 'operator': '=', 'value': '消化内科'}]


def test_exact_attribute_is_not_overwritten_by_a_same_named_dimension():
    e, k, p = fixture()
    k['entities'][0].metadata['attributes'][1]['attr_name'] = '适用科室'
    p['filters'][0]['key'] = 'product_line.line_level1'
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == [] and ast['filters'][0]['field'] == 'department.dept_name'
