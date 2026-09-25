"""Synthetic catalog fixtures; no production rows, identifiers or model calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api
import agent
import mysql_tool
import vector_store
from dimension_scope import project_dimension_to_domain
from prompt_build import PromptBuilder
from scope_contract import scope_filter
from vector_store import SearchResult, build_records_from_dsl
from scripts.audit_semantic_snapshot import audit_snapshot


def matches(where, metadata):
    if '$and' in where:
        return all(matches(w, metadata) for w in where['$and'])
    return all(metadata.get(k) in v['$in'] if isinstance(v, dict) else metadata.get(k) == v
               for k, v in where.items())


def catalog(model=900, domain=901):
    return dict(semantic_model={'id': model, 'name': 'Synthetic model'},
        business_domain={'id': domain, 'name': 'Synthetic domain'}, metrics=[],
        entities=[dict(entity_id=11, entity_code='sales', entity_name='Sales',
            business_domain=domain, attributes=[dict(attribute_id=12, attr_code='date',
                attr_name='Transaction date', field_mapping='sales.created_at')])],
        dimensions=[dict(dim_code='transaction_date', dim_name='Transaction date', dim_type='time',
            bind_entities=[{'entity': '11', 'attr': '12', 'businessDomain': str(domain),
                            'mappingTable': 'stale_table', 'mappingColumn': 'stale_field'},
                           {'entity': '21', 'attr': '22', 'businessDomain': '902'}],
            bind_metrics=['unknown_metric'], field_mapping={}, special_rules=[],
            dim_hierarchy=[], enum_list=[dict(code='example', name='Example', value='example')])])


class Store:
    persist_dir = 'synthetic-in-memory'
    def __init__(self, records):
        self.records = {r.id: SearchResult(r.id, 1.0, r.text, r.metadata) for r in records}
        self.filters = []
    def count(self): return len(self.records)
    def get_by_where(self, where):
        self.filters.append(where)
        return [r for r in self.records.values() if matches(where, r.metadata)]
    def search(self, vector, top_k, where): return self.get_by_where(where)[:top_k]
    def get_ids_by_where(self, where): return [r.id for r in self.get_by_where(where)]
    def add(self, records):
        self.records.update({r.id: SearchResult(r.id, 1.0, r.text, r.metadata) for r in records})
    def delete_by_ids(self, ids):
        for ident in ids: self.records.pop(ident, None)


def records(doc): return build_records_from_dsl(doc, lambda texts: [[1.0] for _ in texts])


def test_catalog_preserves_governed_ids_for_scope_proof():
    assert mysql_tool._row_to_entity_dict({'id': 11})['entity_id'] == 11
    assert mysql_tool._row_to_attr_dict({'id': 12})['attribute_id'] == 12


def test_publisher_keeps_shared_original_and_adds_owned_projection():
    doc = catalog(); before = deepcopy(doc)
    result = records(doc)
    original = next(r for r in result if r.metadata['type'] == 'dimension')
    scoped = next(r for r in result if r.metadata['type'] == 'scoped_dimension')
    assert original.metadata['business_domain_id'] == -1
    assert len(original.metadata['bind_entities']) == 2
    assert scoped.metadata['business_domain_id'] == 901
    assert scoped.metadata['bind_entities'] == [dict(entity='11', attr='12', businessDomain='901',
        entityName='Sales', attrName='Transaction date', mappingTable='sales', mappingColumn='created_at')]
    assert scoped.metadata['bind_metrics'] == []
    assert scoped.id != original.id
    assert any(r.metadata['type'] == 'scoped_enum' and r.metadata['business_domain_id'] == 901 for r in result)
    assert doc == before


@pytest.mark.parametrize('mutation', ['foreign_entity', 'foreign_attribute', 'foreign_owner',
    'missing_entity_id', 'missing_attribute_id', 'claimed_other_domain', 'opaque_mapping',
    'opaque_hierarchy', 'opaque_rules', 'boolean_id', 'ambiguous_owner'])
def test_unproven_projection_fails_closed_but_preserves_shared_catalog(mutation):
    doc = catalog(); dim = doc['dimensions'][0]; owner = doc['entities'][0]
    if mutation == 'foreign_entity': dim['bind_entities'][0]['entity'] = '99'
    if mutation == 'foreign_attribute': dim['bind_entities'][0]['attr'] = '99'
    if mutation == 'foreign_owner': owner['business_domain'] = 902
    if mutation == 'missing_entity_id': owner.pop('entity_id')
    if mutation == 'missing_attribute_id': owner['attributes'][0].pop('attribute_id')
    if mutation == 'claimed_other_domain': dim['bind_entities'][0]['businessDomain'] = '902'
    if mutation == 'opaque_mapping': dim['field_mapping'] = {'column': 'foreign.secret'}
    if mutation == 'opaque_hierarchy': dim['dim_hierarchy'] = [{'field': 'foreign.secret'}]
    if mutation == 'opaque_rules': dim['special_rules'] = ['foreign.secret']
    if mutation == 'boolean_id': owner['entity_id'] = True
    if mutation == 'ambiguous_owner': doc['entities'].append(deepcopy(owner))
    assert project_dimension_to_domain(dim, doc) is None
    result = records(doc)
    assert not any(r.metadata['type'].startswith('scoped_') for r in result)
    assert any(r.metadata['type'] == 'dimension' and r.metadata['business_domain_id'] == -1 for r in result)


@pytest.mark.parametrize('domains', [[], [901], [902]])
def test_real_publication_to_prompt_dimension_retrieval(domains):
    store = Store(records(catalog()) + records(catalog(domain=902)) + records(catalog(model=999)))
    builder = PromptBuilder(store, lambda _: [1.0], semantic_model_id=900, business_domain_ids=domains)
    builder.build('Transaction date')
    dimensions = builder.last_knowledge['dimensions']
    assert len(dimensions) == 1
    metadata = dimensions[0].metadata
    assert metadata['semantic_model_id'] == 900
    assert metadata['business_domain_id'] == (domains[0] if domains else -1)
    assert metadata['type'] == ('scoped_dimension' if domains else 'dimension')
    if domains:
        assert len(metadata['bind_entities']) == 1
        assert metadata['bind_entities'][0]['mappingTable'] == 'sales'
        assert metadata['bind_entities'][0]['businessDomain'] == str(domains[0])
        assert all('-1' not in str(where) for where in store.filters)


def test_scope_republish_removes_stale_projection_without_deleting_other_domains(monkeypatch):
    doc = catalog(); store = Store(records(doc) + records(catalog(domain=902)))
    other = {k: deepcopy(v.metadata) for k,v in store.records.items() if v.metadata['business_domain_id'] == 902}
    doc['dimensions'][0]['bind_entities'] = []
    monkeypatch.setattr(mysql_tool, 'get_dsl_by_scope', lambda *_: doc)
    report = vector_store.rebuild_index_by_scope(store, lambda texts: [[1.0] for _ in texts], 900, 901)
    assert report['by_scope'][0]['dimension_projection_candidates'] == 1
    assert report['by_scope'][0]['dimension_projections_published'] == 0
    assert not any(r.metadata['type'].startswith('scoped_') and r.metadata['business_domain_id'] == 901 for r in store.records.values())
    assert {k: store.records[k].metadata for k in other} == other


@pytest.mark.parametrize('path', ['exact', 'approximate'])
@pytest.mark.parametrize('model,domain', [(999, 901), (900, 902), (900, -1), (None, 901), (900, None)])
def test_entity_value_response_cannot_borrow_request_scope(path, model, domain, monkeypatch):
    hit = SearchResult('synthetic', 1.0, 'Example', dict(semantic_model_id=model,
        business_domain_id=domain, type='entity_attribute_value', attr_value='Example'))
    monkeypatch.setattr(api, '_store', SimpleNamespace(
        find_exact=lambda _: [hit] if path == 'exact' else [],
        search=lambda *a, **kw: [hit] if path == 'approximate' else []))
    monkeypatch.setattr(api, 'embed_query', lambda _: [1.0])
    response = TestClient(api.app).post('/vector/entity-attributes/search', json={
        'semantic_model_id': 900, 'business_domain_ids': [901], 'query': 'Example'})
    assert response.status_code == 502
    assert response.json()['detail']['code'] == 'SEMANTIC_SCOPE_MISMATCH'


@pytest.mark.parametrize('domains', [[], [901]])
@pytest.mark.parametrize('path', ['exact', 'approximate'])
def test_entity_value_exact_and_vector_paths_preserve_valid_scope(domains, path, monkeypatch):
    hit = SearchResult('synthetic', 1.0, 'Example', dict(semantic_model_id=900,
        business_domain_id=901, type='entity_attribute_value', attr_value='Example'))
    monkeypatch.setattr(api, '_store', SimpleNamespace(
        find_exact=lambda _: [hit] if path == 'exact' else [],
        search=lambda *a, **kw: [hit] if path == 'approximate' else []))
    monkeypatch.setattr(api, 'embed_query', lambda _: [1.0])
    response = TestClient(api.app).post('/vector/entity-attributes/search', json={
        'semantic_model_id': 900, 'business_domain_ids': domains, 'query': 'Example'})
    assert response.status_code == 200
    assert response.json()['matches'][0]['business_domain_id'] == 901


@pytest.mark.parametrize('kind', ['entity','attribute','relation','metric','dimension','entity_attribute_value'])
@pytest.mark.parametrize('path', ['search', 'lookup', 'fallback'])
def test_prompt_rejects_foreign_payload_before_model_rendering(kind, path):
    bad = SearchResult('foreign', 1.0, 'Forbidden text', dict(type=kind,
        semantic_model_id=999, business_domain_id=901))
    def search(*args, where, **kwargs):
        type_clause = next(c for c in where['$and'] if 'type' in c)
        return [bad] if matches(type_clause, bad.metadata) else []
    store = SimpleNamespace(search=search, get_by_where=lambda _: [bad])
    builder = PromptBuilder(store, lambda _: [], semantic_model_id=900, business_domain_ids=[901])
    with pytest.raises(ValueError, match='SEMANTIC_SCOPE_MISMATCH'):
        if path == 'search': builder.build('Example')
        elif path == 'lookup': builder._load_scope_records(kind, [])
        else:
            del store.get_by_where
            builder._load_scope_records(kind, [bad])


def test_dimension_display_uses_exact_domain_projection(monkeypatch):
    store = Store(records(catalog()))
    monkeypatch.setattr(api, '_store', store)
    monkeypatch.setattr(api, 'embed_query', lambda _: [1.0])
    response = api.semantic_display_elements_resolve(api.SemanticDisplayResolveRequest(
        semantic_model_id=900, business_domain_ids=[901], candidates=[dict(
            candidate_id='example', slot='dimension', value='Transaction date')]))
    assert response.matches[0].business_domain_id == 901
    assert response.matches[0].canonical_code == 'transaction_date'


@pytest.mark.parametrize('attribute', ['12', '22'])
def test_real_asl_route_admits_only_the_published_dimension_binding(attribute, monkeypatch):
    import json
    store = Store(records(catalog()))
    generated = json.dumps(dict(version='2.0', intent='query', subject={'entity':'sales'},
        metrics=[], dimensions=[dict(name='transaction_date', attr=attribute, level=None, granularity=None)],
        filters=[], time_context=None, sort=None, limit=5, having=[], ambiguity=[]))
    def factory(**kwargs):
        assert 'stale_table' not in kwargs['system_prompt']
        assert 'mappingColumn: stale_field' not in kwargs['system_prompt']
        return SimpleNamespace(invoke=lambda _: dict(messages=[SimpleNamespace(content=generated)]))
    monkeypatch.setattr(api, '_store', store)
    monkeypatch.setattr(agent, 'embed_query', lambda _: [1.0])
    monkeypatch.setattr(agent, '_get_chat_model', lambda: object())
    monkeypatch.setattr(agent, 'create_deep_agent', factory)
    response = TestClient(api.app).post('/agent/query', json={
        'query':'List Transaction date', 'semantic_model_id':900,
        'business_domain_ids':[901], 'metricless_projection': True})
    if attribute == '12':
        assert response.status_code == 200, response.text
        assert response.json()['semantic_evidence']['requested_business_domain_ids'] == [901]
        assert json.loads(response.json()['result'])['dimensions'][0]['attr'] == '12'
    else:
        assert response.status_code == 502, response.text


def test_old_snapshot_without_owned_projection_remains_unavailable():
    legacy = [r for r in records(catalog()) if not r.metadata['type'].startswith('scoped_')]
    builder = PromptBuilder(Store(legacy), lambda _: [], semantic_model_id=900, business_domain_ids=[901])
    assert builder.retrieve('Transaction date')['dimensions'] == []


def test_local_snapshot_audit_is_read_only_and_exports_no_record_text(tmp_path):
    import hashlib
    import json
    import sqlite3
    path = tmp_path/'catalog.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE embedding_metadata(id INTEGER,key TEXT,string_value TEXT,int_value INTEGER,float_value REAL)')
        conn.executemany('INSERT INTO embedding_metadata VALUES(?,?,?,?,?)', [
            (1,'type','dimension',None,None), (1,'business_domain_id',None,-1,None),
            (1,'dim_name','PRIVATE_TEST_MARKER',None,None)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = audit_snapshot(path)
    assert report['shared_record_type_counts'] == {'dimension': 1}
    assert report['requires_catalog_republication'] is True
    assert report['explicit_dimension_projection_count'] == 0
    assert report['deployment_gate'] == 'UNKNOWN'
    assert 'PRIVATE_TEST_MARKER' not in json.dumps(report)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
