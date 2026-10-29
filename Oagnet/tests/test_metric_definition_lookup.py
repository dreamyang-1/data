from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from metric_definition_lookup import MetricDefinitionLookupRequest, lookup_metric_definitions


def row(**updates):
    meta = {'type': 'metric', 'semantic_model_id': 130, 'business_domain_id': 205,
        'metric_code': 'orders', 'metric_name': '订单笔数', 'synonyms': ['订单数'],
        'business_definition': {'description': '订单号去重计数'},
        'calculation_rule': {'calc_formula': 'COUNT(DISTINCT order_key)'}}
    meta.update(updates)
    return SimpleNamespace(id='m-orders', metadata=meta)


def lookup(rows, names=None, domains=None):
    searches = []
    class Store:
        def find_exact(self, where): searches.append(where); return rows
        def search(self, vector, **kwargs): searches.append(kwargs['where']); return []
    result = lookup_metric_definitions(MetricDefinitionLookupRequest(semantic_model_id=130,
        business_domain_ids=domains or [], query='解释订单口径', metric_names=names or ['订单笔数']), Store(), lambda query: [0.1])
    return result, searches


@pytest.mark.parametrize('name', ['订单笔数', '订单数', 'orders'])
def test_exact_names_aliases_and_codes_retrieve_registered_vector_definition(name):
    response, filters = lookup([row()], [name], [205])
    item = response['results'][0]
    assert item['status'] == 'RESOLVED' and item['definition']['business_definition']['description'] == '订单号去重计数'
    assert response['business_domain_ids'] == [205]
    assert all({'business_domain_id': {'$in': [205]}} in f['$and'] for f in filters)


def test_unknown_term_is_not_replaced_by_a_similar_metric():
    response, _ = lookup([row()], ['订单金额'])
    assert response['results'] == [{'input': '订单金额', 'status': 'NOT_FOUND'}]


def test_conflicting_definitions_remain_ambiguous():
    response, _ = lookup([row(), row(business_definition={'description': '另一种含义'})])
    assert response['results'][0]['status'] == 'AMBIGUOUS'
    assert 'definition' not in response['results'][0]


def test_identical_duplicate_records_do_not_cause_false_ambiguity():
    response, _ = lookup([row(), row()])
    assert response['results'][0]['status'] == 'RESOLVED'


@pytest.mark.parametrize('updates', [{'semantic_model_id': 131}, {'business_domain_id': 206}])
def test_cross_scope_records_are_rejected(updates):
    with pytest.raises(ValueError, match='SEMANTIC_SCOPE_MISMATCH'):
        lookup([row(**updates)], domains=[205])


@pytest.mark.parametrize('update', [{'semantic_model_id': True}, {'semantic_model_id': 0},
    {'business_domain_ids': [205, 206]}, {'business_domain_ids': [-1]}])
def test_invalid_or_expanded_scope_cannot_enter_lookup(update):
    values = {'semantic_model_id': 130, 'metric_names': ['订单笔数'], 'query': '解释口径', **update}
    with pytest.raises(ValidationError):
        MetricDefinitionLookupRequest(**values)


def test_partial_lookup_preserves_all_requested_topics():
    response, _ = lookup([row()], ['订单笔数', '未登记指标'])
    assert [x['status'] for x in response['results']] == ['RESOLVED', 'NOT_FOUND']
