"""Current request scope must survive every retrieval and repair path."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent
import api
import mysql_tool
from prompt_build import PromptBuilder
from scope_contract import normalize_domains, require_model_id, scope_filter
from vector_store import SearchResult, MilvusVectorStore, build_scope_filter


def matches(where, metadata):
    if '$and' in where:
        return all(matches(item, metadata) for item in where['$and'])
    return all(metadata.get(key) in value['$in'] if isinstance(value, dict)
               else metadata.get(key) == value for key, value in where.items())


@pytest.mark.parametrize('model', [None, 0, -1, True, 81.0, '81'])
def test_model_is_required_strict_positive(model):
    with pytest.raises(ValueError, match='REQUEST_SCOPE_INVALID'):
        require_model_id(model)
    with pytest.raises(ValueError, match='REQUEST_SCOPE_INVALID'):
        PromptBuilder(object(), lambda _: [], semantic_model_id=model)


@pytest.mark.parametrize('kind', ['entity', 'attribute', 'relation', 'metric', 'dimension', 'entity_attribute_value'])
@pytest.mark.parametrize('domains', [[], [205]])
def test_all_roles_are_model_bound_and_explicit_excludes_shared(kind, domains):
    builder = PromptBuilder(object(), lambda _: [], semantic_model_id=81, business_domain_ids=domains)
    where = builder._build_where(kind)
    for model, domain in [(81, 205), (81, 206), (81, -1), (81, None), (82, 205)]:
        assert matches(where, dict(type=kind, semantic_model_id=model, business_domain_id=domain)) == (
            model == 81 and (not domains or domain == 205))
    compiled = MilvusVectorStore._compile_filter(where)
    assert '81' in compiled
    if domains:
        assert '205' in compiled and '-1' not in compiled and '206' not in compiled


def test_shared_vector_filter_obeys_the_same_contract():
    assert build_scope_filter(81, 205) == scope_filter(81, business_domain_ids=[205])
    assert build_scope_filter(81) == {'semantic_model_id': 81}


@pytest.mark.parametrize('domains', [[205, 206], [206, 205, 205]])
def test_multi_domain_rejected_before_retrieval(domains):
    with pytest.raises(ValueError, match='EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'):
        PromptBuilder(object(), lambda _: pytest.fail('embedding must not run'),
                      semantic_model_id=81, business_domain_ids=domains)
    with pytest.raises(ValueError, match='EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'):
        agent.main('revenue', semantic_model_id=81, business_domain_ids=domains)


def test_legacy_domain_and_explicit_array_must_agree():
    assert normalize_domains(205) == [205]
    assert normalize_domains(205, [205, 205]) == [205]
    with pytest.raises(ValueError, match='conflicts'):
        normalize_domains(205, [])
    with pytest.raises(ValueError, match='conflicts'):
        normalize_domains(205, [206])


@pytest.mark.parametrize('route,payload', [
    ('/agent/query', {'query': 'revenue'}),
    ('/vector/entity-attributes/search', {'query': 'example'}),
    ('/vector/semantic-elements/resolve', {'candidates': []}),
])
def test_public_multi_domain_is_fail_closed(route, payload, monkeypatch):
    monkeypatch.setattr(api, 'main', lambda *a, **kw: pytest.fail('model must not run'))
    response = TestClient(api.app).post(route, json={**payload, 'semantic_model_id': 81, 'business_domain_ids': [205, 206]})
    assert response.status_code == 422
    assert response.json()['code'] == 'EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'


def test_metric_evidence_query_does_not_admit_unassigned_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(mysql_tool, '_query', lambda sql, args: calls.append((sql, args)) or [])
    mysql_tool.get_metric_evidence(81, ['revenue'], [205])
    sql, params = calls[0]
    assert params == (81, 'revenue', 205)
    assert 'business_domain_id IN (%s)' in sql
    assert 'business_domain_id IS NULL' not in sql


@pytest.mark.parametrize('model,domain', [(82, 205), (81, 206), (81, -1), (81, None)])
def test_sql_evidence_cannot_borrow_vector_scope(model, domain):
    metric = SearchResult(id='m', score=1, text='revenue', metadata={
        'semantic_model_id': 81, 'business_domain_id': 205,
        'metric_code': 'revenue', 'metric_name': 'revenue'})
    row = dict(semantic_model_id=model, business_domain_id=domain, indicator_code='revenue',
               indicator_name='revenue', calculation_formula='SUM(sales.amount)')
    with pytest.raises(ValueError, match='SEMANTIC_SCOPE_MISMATCH'):
        agent._build_semantic_evidence(json.dumps({'metrics': [{'name': 'revenue'}]}),
                                      {'metrics': [metric]}, 81, [205], sql_loader=lambda *_: [row])


@pytest.mark.parametrize('model,domain', [(82, 205), (81, 206), (81, -1), (81, None)])
def test_display_provenance_is_checked_before_a_label_is_returned(model, domain, monkeypatch):
    metadata = dict(semantic_model_id=model, business_domain_id=domain, metric_name='revenue', metric_code='revenue')
    monkeypatch.setattr(api, 'embed_query', lambda text: [])
    monkeypatch.setattr(api._store, 'search', lambda *a, **kw: [SimpleNamespace(id='m', metadata=metadata, score=1)])
    response = TestClient(api.app).post('/vector/semantic-elements/resolve', json={
        'semantic_model_id': 81, 'business_domain_ids': [205],
        'candidates': [{'candidate_id': 'm', 'slot': 'metric', 'value': 'revenue'}]})
    assert response.status_code == 502
    assert response.json()['detail']['code'] == 'SEMANTIC_SCOPE_MISMATCH'


def test_display_returns_exact_scope_provenance(monkeypatch):
    observed = []
    def search(*args, where, **kwargs):
        observed.append(where)
        return [SimpleNamespace(id='m', score=1, metadata=dict(semantic_model_id=81,
            business_domain_id=205, metric_name='revenue', metric_code='revenue'))]
    monkeypatch.setattr(api, 'embed_query', lambda text: [])
    monkeypatch.setattr(api._store, 'search', search)
    response = api.semantic_display_elements_resolve(api.SemanticDisplayResolveRequest(
        semantic_model_id=81, business_domain_ids=[205],
        candidates=[dict(candidate_id='m', slot='metric', value='revenue')]))
    assert response.business_domain_ids == [205]
    assert response.matches[0].semantic_model_id == 81
    assert response.matches[0].business_domain_id == 205
    assert not matches(observed[0], dict(type='metric', semantic_model_id=81, business_domain_id=-1))


def test_physical_metadata_queries_are_domain_scoped_before_fetch(monkeypatch):
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args): calls.append((sql, args))
        def fetchall(self): return []
    class Connection:
        def cursor(self, *args): return Cursor()
        def close(self): pass
    monkeypatch.setattr(mysql_tool, '_get_connection', Connection)
    assert mysql_tool.get_table_field_by_scope(81, business_domain_id=205)['tables'] == []
    assert len(calls) == 2
    for sql, params in calls:
        assert params == [81, 81, 205]
        assert 'b.id=%s' in sql and 'b.semantic_model_id=%s' in sql
        assert 'e.main_table_name=t.name' in sql
        assert 't.semantic_model_id=%s' in sql


def test_filter_repair_does_not_drop_the_domain(monkeypatch):
    observed = []
    monkeypatch.setattr(agent, 'get_registered_entity_attributes', lambda model, domain: [])
    monkeypatch.setattr(agent, 'get_table_field_by_scope', lambda **kwargs: observed.append(kwargs) or {'tables': []})
    assert agent._contract_filter_candidates('name', {}, semantic_model_id=81, domain_scope=205) == []
    assert observed == [dict(semantic_model_id=81, business_domain_id=205)]


def test_time_anchor_repair_does_not_drop_the_domain():
    observed = []
    entity = SimpleNamespace(metadata={'entity_code': 'sales', 'attributes': [
        {'attr_code': 'amount', 'field_mapping': 'sales.amount'}]})
    def loader(*args, **kwargs):
        observed.append((args, kwargs))
        return {'tables': []}
    agent._registered_subject_time_anchor({'subject': {'entity': 'sales'}},
        {'entities': [entity]}, 81, table_field_loader=loader, domain_scope=205)
    assert observed == [((81, None), {'business_domain_id': 205})]


def test_real_query_chain_uses_only_the_explicit_domain(monkeypatch):
    records = [
        SearchResult(id='entity-205', score=1, text='sales', metadata={
            'type': 'entity', 'semantic_model_id': 81, 'business_domain_id': 205,
            'entity_code': 'sales', 'entity_name': 'sales', 'attributes': [
                {'attr_code': 'amount', 'attr_name': 'amount', 'field_mapping': 'sales.amount'}]}),
        SearchResult(id='metric-205', score=1, text='revenue', metadata={
            'type': 'metric', 'semantic_model_id': 81, 'business_domain_id': 205,
            'metric_code': 'revenue', 'metric_name': 'revenue',
            'source_dependency': {'bind_entity': ['sales']},
            'calculation_rule': {'calc_formula': 'SUM(sales.amount)'}}),
        SearchResult(id='metric-206', score=1, text='forbidden-other-domain', metadata={
            'type': 'metric', 'semantic_model_id': 81, 'business_domain_id': 206,
            'metric_code': 'outside', 'metric_name': 'forbidden-other-domain'}),
        SearchResult(id='dimension-shared', score=1, text='forbidden-shared', metadata={
            'type': 'dimension', 'semantic_model_id': 81, 'business_domain_id': -1,
            'dim_code': 'outside_date', 'dim_name': 'forbidden-shared'}),
        SearchResult(id='metric-other-model', score=1, text='forbidden-other-model', metadata={
            'type': 'metric', 'semantic_model_id': 82, 'business_domain_id': 205,
            'metric_code': 'outside', 'metric_name': 'forbidden-other-model'}),
    ]
    observed = []
    class Store:
        def count(self): return len(records)
        def search(self, vector, top_k, where):
            observed.append(where)
            return [item for item in records if matches(where, item.metadata)][:top_k]
        def get_by_where(self, where):
            observed.append(where)
            return [item for item in records if matches(where, item.metadata)]
    result = json.dumps(dict(version='2.0', intent='query', subject={'entity': 'sales'},
        metrics=[{'name': 'revenue'}], dimensions=[], filters=[], time_context=None,
        sort=None, limit=None, having=[], ambiguity=[]))
    def model_factory(**kwargs):
        assert 'forbidden-' not in kwargs['system_prompt']
        return SimpleNamespace(invoke=lambda _: {'messages': [SimpleNamespace(content=result)]})
    def metric_loader(model, codes, domains):
        assert (model, codes, domains) == (81, ['revenue'], [205])
        return [dict(semantic_model_id=81, business_domain_id=205, indicator_code='revenue',
                     indicator_name='revenue', calculation_formula='SUM(sales.amount)')]
    monkeypatch.setattr(api, '_store', Store())
    monkeypatch.setattr(agent, 'embed_query', lambda _: [1.0])
    monkeypatch.setattr(agent, '_get_chat_model', lambda: object())
    monkeypatch.setattr(agent, 'create_deep_agent', model_factory)
    monkeypatch.setattr(agent, 'get_metric_evidence', metric_loader)
    response = TestClient(api.app).post('/agent/query', json={
        'query': 'revenue', 'semantic_model_id': 81, 'business_domain_ids': [205], 'metric_ids': ['81:revenue']})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['business_domain_ids'] == [205]
    assert body['semantic_evidence']['resolved_business_domain_ids'] == [205]
    assert body['semantic_evidence']['selected_metrics'][0]['business_domain_id'] == 205
    assert observed and all(any('business_domain_id' in clause for clause in where['$and']) for where in observed)
