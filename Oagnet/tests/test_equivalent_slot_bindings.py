"""Directory representation duplicates are not cross-entity homonyms."""
from copy import deepcopy

import pytest

from structured_binding import _exact_choice_keys, catalog_candidates
from test_binding_ownership import fixture, run
from types import SimpleNamespace as Obj


def setup_case():
    extraction, knowledge, plan = fixture()
    hospital = knowledge['entities'][1].metadata
    hospital['entity_id'] = 'hospital-uuid'
    hospital['attributes'][1].update(attr_name='医院等级', synonyms=['医院级别'], attribute_id='grade-uuid')
    knowledge['dimensions'] = [Obj(metadata={
        'dim_code': 'hospital_level', 'dim_name': '医院等级', 'synonyms': ['医院级别'],
        'dim_type': '枚举维度', 'dim_hierarchy': [], 'special_rules': [],
        'bind_entities': [{'entity': 'hospital-uuid', 'attr': 'grade-uuid',
                           'mappingTable': 'hospital', 'mappingColumn': 'level'}]})]
    return extraction, knowledge, plan


@pytest.mark.parametrize('target', ['dimensions', 'sort', 'filters'])
@pytest.mark.parametrize('label', ['医院等级', '医院级别'])
@pytest.mark.parametrize('qualified', [False, True])
def test_same_attribute_identity_collapses_across_slot_roles(target, label, qualified):
    _, knowledge, _ = setup_case()
    original = {'entity': '医院', 'field': label} if qualified else label
    assert _exact_choice_keys(target, original, catalog_candidates(knowledge)) == ['hospital.level']


@pytest.mark.parametrize('model_mode', ['wrong', 'missing', 'error'])
def test_original_group_query_corrects_model_choice_without_changing_extraction(model_mode):
    extraction, knowledge, plan = setup_case()
    extraction.update(指标=[{'name': '销售额'}], 维度=['医院等级'], 展示字段=[], 过滤条件=[])
    plan.update(metrics=[{'index': 0, 'key': 'sales'}], dimensions=[{'index': 0, 'key': 'hospital_level'}])
    if model_mode == 'wrong': plan['dimensions'][0]['key'] = 'dealer.level'
    if model_mode == 'missing': plan['dimensions'] = []
    if model_mode == 'error': plan['dimensions'][0]['error'] = '同名目录项'
    before = deepcopy(extraction)
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert [item['name'] for item in ast['dimensions']] == ['hospital.level']
    assert extraction == before


@pytest.mark.parametrize('conflict', ['column', 'owner', 'attribute_id', 'hierarchy', 'rule', 'grain'])
def test_conflicting_or_derived_dimensions_stay_ambiguous(conflict):
    _, knowledge, _ = setup_case()
    meta = knowledge['dimensions'][0].metadata
    mapping = meta['bind_entities'][0]
    if conflict == 'column': mapping['mappingColumn'] = 'name'  # old model's erroneous ID mapping is not equivalent
    if conflict == 'owner': mapping['entity'] = 'dealer'
    if conflict == 'attribute_id': mapping['attr'] = 'other-uuid'
    if conflict == 'hierarchy': meta['dim_hierarchy'] = [{'level': 1}]
    if conflict == 'rule': meta['special_rules'] = ['自定义分组']
    if conflict == 'grain': meta['granularity_support'] = ['month']
    assert len(_exact_choice_keys('dimensions', '医院等级', catalog_candidates(knowledge))) == 2


def test_same_name_on_other_entity_is_not_merged_with_hospital_dimension():
    _, knowledge, _ = setup_case()
    knowledge['entities'][0].metadata['attributes'][1]['attr_name'] = '医院等级'
    assert len(_exact_choice_keys('dimensions', '医院等级', catalog_candidates(knowledge))) == 2


def test_exact_canonical_dimension_code_remains_authoritative():
    _, knowledge, _ = setup_case()
    assert _exact_choice_keys('dimensions', 'hospital_level', catalog_candidates(knowledge)) == ['hospital_level']


def test_metric_name_is_not_merged_into_a_dimension_or_column():
    _, knowledge, _ = setup_case()
    knowledge['metrics'][0].metadata['metric_name'] = '医院等级'
    assert 'sales' in _exact_choice_keys('sort', '医院等级', catalog_candidates(knowledge))
