"""Dimension slots resolve within their own catalog category before fallback."""
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest

from binding_ownership import parameter_catalog
from structured_binding import _exact_choice_keys, catalog_candidates
from test_binding_ownership import run


def fixture():
    entities = [
        {'entity_code': 'department', 'entity_id': 'department-id', 'entity_name': '科室',
         'entity_alias': ['适用科室'], 'attributes': [
             {'attribute_id': 'department-name', 'attr_name': '科室名称', 'attr_code': 'dept_name',
              'field_mapping': 'department.dept_name', 'is_main_attribute': True}]},
        {'entity_code': 'product_dept_relation', 'entity_id': 'relation-id', 'entity_name': '商品科室关联',
         'attributes': [{'attribute_id': 'relation-dept', 'attr_name': '科室编码', 'attr_code': 'dept_code',
                         'field_mapping': 'product_dept_relation.dept_code'}]},
        {'entity_code': 'sales_order', 'entity_name': '销售订单', 'attributes': []},
    ]
    dimension = {'dim_code': 'applicable_department', 'dim_name': '适用科室', 'synonyms': ['适配科室'],
                 'bind_entities': [{'entity': 'relation-id', 'attr': 'relation-dept',
                                    'mappingTable': 'product_dept_relation', 'mappingColumn': 'dept_code'}]}
    knowledge = {'entities': [Obj(metadata=e) for e in entities], 'dimensions': [Obj(metadata=dimension)],
                 'metrics': [Obj(metadata={'metric_code': 'sales', 'metric_name': '销售额'})],
                 '_vector_authorized_fields': ['department.dept_name', 'product_dept_relation.dept_code']}
    extraction = {'实体': ['销售订单', '商品科室关联'], '指标': [{'name': '销售额'}],
                  '维度': ['适用科室'], '展示字段': [], '过滤条件': [], '排序': [],
                  '时间粒度': None, '限制': None}
    plan = {'subject': 'sales_order', 'metrics': [{'index': 0, 'key': 'sales'}],
            'dimensions': [{'index': 0, 'key': 'applicable_department', 'attr': 'department-name'}]}
    return extraction, knowledge, plan


@pytest.mark.parametrize('mode', ['correct', 'wrong', 'missing', 'error'])
@pytest.mark.parametrize('slot', ['适用科室', {'name': '适用科室'}, {'field': '适用科室'}])
def test_dimension_is_not_owned_by_an_entity_with_the_same_alias(mode, slot):
    e, k, p = fixture()
    e['维度'] = [slot]
    if mode == 'wrong':
        p['dimensions'][0]['key'] = 'department.dept_name'
    elif mode == 'missing':
        p['dimensions'] = []
    elif mode == 'error':
        p['dimensions'][0]['error'] = '同名实体无法区分'
    original = deepcopy(e)
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'] == [{'name': 'applicable_department', 'attr': 'relation-dept',
                                   'level': None, 'granularity': None}]
    assert e == original


@pytest.mark.parametrize('other_category', ['entity_name', 'entity_alias', 'attribute', 'metric'])
def test_dimension_alias_is_not_shadowed_by_other_catalog_categories(other_category):
    e, k, p = fixture()
    e['维度'] = ['适配科室']
    if other_category == 'entity_name':
        k['entities'][0].metadata['entity_name'] = '适配科室'
    elif other_category == 'entity_alias':
        for entity in k['entities'][:2]:
            entity.metadata['entity_alias'] = ['适配科室']
    elif other_category == 'attribute':
        k['entities'][0].metadata['attributes'][0]['attr_name'] = '适配科室'
    else:
        k['metrics'].append(Obj(metadata={'metric_code': 'another_metric', 'metric_name': '适配科室'}))
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['name'] == 'applicable_department'


@pytest.mark.parametrize('same_mapping', [True, False])
@pytest.mark.parametrize('alias', [False, True])
def test_two_matching_dimensions_remain_ambiguous_despite_model_choice(same_mapping, alias):
    e, k, p = fixture()
    other = deepcopy(k['dimensions'][0].metadata)
    other['dim_code'] = 'another_dimension'
    if alias:
        other.update(dim_name='其他科室维度', synonyms=['适用科室'])
    if not same_mapping:
        other['bind_entities'] = [{'entity': 'department-id', 'attr': 'department-name',
                                  'mappingTable': 'department', 'mappingColumn': 'dept_name'}]
    k['dimensions'].append(Obj(metadata=other))
    ast, _ = run(e, k, p)
    assert not ast['dimensions']
    assert len(ast['ambiguity']) == 1
    assert set(ast['ambiguity'][0]['candidates']) == {'applicable_department', 'another_dimension'}


@pytest.mark.parametrize('owner,success', [('商品科室关联', True), ('科室', False)])
def test_explicit_owner_is_checked_against_the_dimension_mapping(owner, success):
    e, k, p = fixture()
    e['维度'] = [{'entity': owner, 'name': '适用科室'}]
    ast, _ = run(e, k, p)
    assert (not ast['ambiguity']) is success
    if success:
        assert ast['dimensions'][0]['attr'] == 'relation-dept'


def test_entity_alias_fallback_still_works_when_no_dimension_matches():
    e, k, p = fixture()
    k['dimensions'] = [Obj(metadata={'dim_code': 'department', 'dim_name': '科室',
        'bind_entities': [{'entity': 'department-id', 'attr': 'department-name',
                           'mappingTable': 'department', 'mappingColumn': 'dept_name'}]})]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['name'] == 'department'


def test_dimension_category_lookup_does_not_mutate_shared_catalog():
    e, k, _ = fixture()
    catalog = catalog_candidates(k)
    before = deepcopy(catalog)
    scoped = parameter_catalog(catalog, '适用科室', e, 'dimensions')
    assert not scoped.get('_parameter_owners')
    assert _exact_choice_keys('dimensions', '适用科室', scoped) == ['applicable_department']
    assert catalog == before


def test_exact_dimension_code_can_disambiguate_same_name_definitions():
    e, k, p = fixture()
    other = deepcopy(k['dimensions'][0].metadata)
    other['dim_code'] = 'another_dimension'
    k['dimensions'].append(Obj(metadata=other))
    e['维度'] = ['applicable_department']
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['name'] == 'applicable_department'
