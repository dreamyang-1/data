"""Every binding category uses the declared owner within its authorized catalog."""
import json
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest

from binding_ownership import parameter_catalog
from structured_binding import catalog_candidates, bind
from structured_time import time_anchor
from test_binding_ownership import fixture, run


def dated_catalog():
    _, knowledge, _ = fixture()
    for entity in knowledge['entities'][:2]:
        code = entity.metadata['entity_code']
        entity.metadata['attributes'].append({'attr_name': '业务日期', 'attr_code': 'event_date',
            'field_mapping': code + '.event_date', 'data_type': 'DATE'})
        knowledge['_vector_authorized_fields'].append(code + '.event_date')
    knowledge['metrics'][0].metadata['time_caliber'] = {'time_anchor': 'dealer.event_date'}
    return catalog_candidates(knowledge)


def ast(subject='dealer'):
    return {'subject': {'entity': subject}, 'metrics': [{'name': 'sales'}],
            'dimensions': [], 'display_fields': [], 'filters': []}


def test_explicit_date_homonym_uses_registered_owner_not_model():
    catalog = dated_catalog()
    assert time_anchor({'anchor': '业务日期'}, ast(), catalog, {},
                       {'anchor': 'hospital.event_date'}) == 'dealer.event_date'


def test_explicit_cross_entity_date_keeps_its_own_owner():
    assert time_anchor({'anchor': '业务日期', 'entity': '医院'}, ast(), dated_catalog(), {}, {}) == 'hospital.event_date'


def test_unresolved_date_owner_cannot_fall_back_to_query_subject():
    with pytest.raises(ValueError):
        time_anchor({'anchor': '业务日期', 'entity': '不存在的实体'}, ast(), dated_catalog(), {}, {})


def test_unique_other_entity_date_is_not_restricted_to_subject():
    catalog = dated_catalog()
    catalog['fields']['hospital.event_date']['attr_name'] = '验收日期'
    for binding in catalog['fields']['hospital.event_date']['attribute_bindings']:
        binding['attr_name'] = '验收日期'
    assert time_anchor({'anchor': '验收日期'}, ast(), catalog, {}, {}) == 'hospital.event_date'


@pytest.mark.parametrize('subject', ['sales_order', 'dealer'])
def test_bound_dimension_attribute_disambiguates_date(subject):
    catalog = dated_catalog()
    catalog['dimensions']['event_time'] = {'dim_type': '时间', 'bind_entities': [
        {'entity': 'dealer', 'attr': 1, 'mappingTable': 'dealer', 'mappingColumn': 'event_date'},
        {'entity': 'hospital', 'attr': 2, 'mappingTable': 'hospital', 'mappingColumn': 'event_date'}]}
    query = ast(subject)
    query['dimensions'] = [{'name': 'event_time', 'attr': 2}]
    assert time_anchor({'anchor': '业务日期'}, query, catalog, {}, {}) == 'hospital.event_date'


def test_metric_date_evidence_resolves_homonyms_when_subject_does_not():
    assert time_anchor({'anchor': '业务日期'}, ast('sales_order'), dated_catalog(), {}, {}) == 'dealer.event_date'


def test_ambiguous_dates_cannot_be_selected_by_model():
    catalog = dated_catalog()
    catalog['metrics']['sales'].pop('time_caliber')
    with pytest.raises(ValueError):
        time_anchor({'anchor': '业务日期'}, ast('sales_order'), catalog, {}, {'anchor': 'dealer.event_date'})


def test_metric_date_cannot_restore_an_unauthorized_field():
    catalog = dated_catalog()
    del catalog['fields']['dealer.event_date']
    with pytest.raises(ValueError):
        time_anchor({'anchor': 'dealer.event_date'}, ast(), catalog, {}, {})


def test_implicit_date_ambiguity_cannot_be_resolved_by_model_guess():
    catalog = dated_catalog()
    catalog['fields']['dealer.updated_date'] = {'attr_name': '更新日期', 'data_type': 'DATE', 'owner': 'dealer'}
    query = ast()
    query['metrics'] = []
    query['display_fields'] = [{'name': 'dealer.name'}]
    with pytest.raises(ValueError):
        time_anchor({}, query, catalog, {}, {'anchor': 'dealer.event_date'})


@pytest.mark.parametrize('target', ['metrics', 'dimensions', 'display_fields', 'filters', 'sort', 'time'])
def test_all_categories_reject_unknown_explicit_owner(target):
    result = parameter_catalog(dated_catalog(), {'name': '名称', 'entity': '不存在的实体'}, {}, target)
    assert result.get('_parameter_owner_error')


@pytest.mark.parametrize('target', ['metrics', 'filters', 'sort'])
def test_metric_homonyms_are_scoped_for_selection_threshold_and_sort(target):
    catalog = dated_catalog()
    catalog['metrics'] = {code: {'metric_name': '金额', 'source_dependency': {'bind_entity': [code]}}
                          for code in ('dealer', 'hospital')}
    scoped = parameter_catalog(catalog, {'name': '金额', 'entity': '医院'}, {}, target)
    assert set(scoped['metrics']) == {'hospital'}


@pytest.mark.parametrize('target', ['dimensions', 'display_fields', 'filters', 'sort'])
def test_dimension_homonyms_are_scoped_before_selection(target):
    catalog = dated_catalog()
    catalog['dimensions'] = {code: {'dim_name': '业务日期', 'bind_entities': [
        {'entity': code, 'mappingTable': code, 'mappingColumn': 'event_date'}]}
        for code in ('dealer', 'hospital')}
    scoped = parameter_catalog(catalog, {'name': '业务日期', 'entity': '医院'}, {}, target)
    assert set(scoped['dimensions']) == {'hospital'}


def test_filter_recovery_cannot_escape_declared_owner():
    e, k, p = fixture()
    e['过滤条件'] = [{'entity': '不存在的实体', 'field': '型号', 'op': '=', 'value': ['TDC-3']}]
    result, _ = run(e, k, p)
    assert result['ambiguity'] and not result['filters']


def test_wrong_model_metric_is_corrected_within_declared_owner():
    e, k, p = fixture()
    for code in ('dealer', 'hospital'):
        k['metrics'].append(Obj(metadata={'metric_code': code + '_count', 'metric_name': '数量',
            'source_dependency': {'bind_entity': [code]}}))
    e['指标'] = [{'name': '数量', 'entity': '经销商'}]
    p['metrics'] = [{'index': 0, 'key': 'hospital_count'}]
    result, _ = run(e, k, p)
    assert not result['ambiguity']
    assert result['metrics'][0]['name'] == 'dealer_count'


def test_scope_does_not_mutate_shared_catalog():
    catalog = dated_catalog()
    before = deepcopy(catalog)
    parameter_catalog(catalog, {'field': '业务日期', 'entity': '医院'}, {}, 'time')
    assert catalog == before
