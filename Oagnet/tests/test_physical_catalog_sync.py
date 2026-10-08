"""Synthetic publication -> vector retrieval -> enum binding, no network writes."""
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api
import mysql_tool as mysql
import physical_catalog_sync as sync
import vector_store
from prompt_build import PromptBuilder
from structured_binding import bind, catalog_candidates
from vector_store import SearchResult, VectorRecord


def matches(where, record):
    if '$and' in where:
        return all(matches(part, record) for part in where['$and'])
    return all((record.id if key == 'record_id' else record.metadata.get(key)) in value['$in']
               if isinstance(value, dict) else record.metadata.get(key) == value
               for key, value in where.items())


class Store:
    persist_dir = 'synthetic'

    def __init__(self, records=()):
        self.records = {r.id: deepcopy(r) for r in records}
        self.add_calls = 0
        self.deleted = []

    def get_by_where(self, where):
        return [deepcopy(r) for r in self.records.values() if matches(where, r)]

    def get_ids_by_where(self, where):
        return [r.id for r in self.get_by_where(where)]

    def search(self, vector, top_k, where):
        return [SearchResult(r.id, 1.0, r.text, deepcopy(r.metadata))
                for r in self.get_by_where(where)][:top_k]

    def add(self, records):
        self.add_calls += 1
        self.records.update({r.id: deepcopy(r) for r in records})

    def delete_by_ids(self, ids):
        self.deleted.extend(ids)
        for ident in ids:
            self.records.pop(ident, None)

    def get_catalog_inventory(self, where):
        return self.get_by_where(where)


def sources(model=900, ds=90, domain=901, enums=None):
    enums = enums if enums is not None else [{'code': '3', 'name': '直销'}, {'code': '8', 'name': '其他'}]
    table = dict(table_id=10, semantic_model_id=model, data_source_id=ds, table_name='sales',
                 fields=[dict(table_id=10, field_id=11, semantic_model_id=model,
                              data_source_id=ds, field_name='channel', data_type='STRING', enum_values=enums)])
    attrs = [dict(business_domain_id=domain, data_source_id=ds, entity_code='sales',
                  attr_code='channel', attr_name='渠道业态', field_mapping='sales.channel')]
    doc = dict(semantic_model={'id': model, 'name': 'Synthetic'},
               business_domain={'id': domain, 'name': 'Synthetic domain'},
               entities=[dict(entity_id=1, entity_code='sales', entity_name='销售订单',
                              attributes=[dict(attribute_id=11, attr_code='channel', attr_name='渠道业态',
                                               field_mapping='sales.channel', data_type='STRING', enum_values=[])])],
               dimensions=[], metrics=[])
    return {'tables': [table]}, attrs, doc


def loaders(monkeypatch, physical, attrs, doc):
    monkeypatch.setattr(sync, 'get_table_field_by_scope', lambda **kw: deepcopy(physical))
    monkeypatch.setattr(sync, 'get_registered_entity_attributes', lambda *a: deepcopy(attrs))
    monkeypatch.setattr(mysql, 'get_dsl_by_scope', lambda *a: deepcopy(doc))
    monkeypatch.setattr(mysql, 'get_business_domains', lambda *a: [{'id': doc['business_domain']['id']}])


def embed(texts):
    return [[1.0, 0.5] for _ in texts]


def publish(monkeypatch, model=900, ds=90, domain=901, store=None):
    physical, attrs, doc = sources(model, ds, domain)
    loaders(monkeypatch, physical, attrs, doc)
    plan = sync.prepare_physical_catalog(model, domain, embed)
    store = store if store is not None else Store()
    semantic = vector_store.rebuild_index_by_scope(store, embed, model, domain,
                                                   attribute_enums=plan.attribute_enums)
    physical_stats = sync.publish_physical_catalog(store, plan)
    return store, plan, semantic, physical_stats


@pytest.mark.parametrize('model,ds,domain', [(900, 90, 901), (910, 91, 911), (920, 92, 921)])
def test_existing_api_publish_includes_physical_and_enums_without_new_parameters(monkeypatch, model, ds, domain):
    physical, attrs, doc = sources(model, ds, domain)
    loaders(monkeypatch, physical, attrs, doc)
    store = Store()
    monkeypatch.setattr(api, '_store', store)
    monkeypatch.setattr(api, 'embed_documents', embed)
    monkeypatch.setattr(api, 'mysql_advisory_lock', lambda *a: nullcontext())
    monkeypatch.setattr(api, 'consistent_catalog_read', nullcontext)
    response = TestClient(api.app).post('/vector/rebuild', json={'semantic_model_id': model, 'business_domain_id': domain})
    assert response.status_code == 200
    assert set(response.json()) == {'success', 'message', 'total', 'by_type'}
    assert response.json()['by_type'] == {'entity': 1, 'attribute': 1, 'table': 1, 'field': 1}
    assert response.json()['total'] == 4
    assert f'ds{ds}:field:sales.channel' in store.records
    attr = next(r for r in store.records.values() if r.metadata['type'] == 'attribute')
    assert attr.metadata['enum_values'] == physical['tables'][0]['fields'][0]['enum_values']
    assert '其他' in attr.text
    assert doc['entities'][0]['attributes'][0]['enum_values'] == []  # source not mutated


def test_rebuild_request_schema_and_legacy_only_rebuild_stay_compatible(monkeypatch):
    schema = api.app.openapi()['components']['schemas']
    assert set(schema['RebuildRequest']['properties']) == {'semantic_model_id', 'business_domain_id'}
    assert set(schema['RebuildResponse']['properties']) == {'success', 'message', 'total', 'by_type'}
    physical, attrs, doc = sources()
    loaders(monkeypatch, physical, attrs, doc)
    store = Store()
    result = vector_store.rebuild_index_by_scope(store, embed, 900, 901)
    assert result['by_type'] == {'entity': 1, 'attribute': 1}
    assert all(r.metadata.get('enum_values') in (None, []) for r in store.records.values())


@pytest.mark.parametrize('values,operator,expected', [
    (['其他'], '=', '8'), (['直销', '其他'], 'IN', ['3', '8']), (['8'], '=', '8'),
])
def test_published_field_only_enum_reaches_real_retrieval_and_asl(monkeypatch, values, operator, expected):
    store, _, _, _ = publish(monkeypatch)
    builder = PromptBuilder(store, lambda _: [1.0, 0.5], semantic_model_id=900, business_domain_ids=[901])
    knowledge = builder.retrieve('渠道业态 其他')
    knowledge['_vector_authorized_fields'] = ['sales.channel']
    candidates = catalog_candidates(knowledge)
    assert not knowledge.get('dimensions')  # enum supplied only by the field registry
    assert {v['value'] for v in candidates['values']} == {'3', '8'}
    extraction = {'意图': '明细查询', '实体': ['销售订单'], '指标': [], '维度': [],
                  '展示字段': [{'entity': '销售订单', 'field': '渠道业态'}],
                  '过滤条件': [{'field': '渠道业态', 'op': operator, 'value': values}],
                  '排序': [], '时间粒度': {'unit': None, 'time_range': None}, '限制': None}
    choice = {'subject': 'sales', 'display_fields': [{'index': 0, 'key': 'sales.channel'}],
              'filters': [{'index': 0, 'key': 'sales.channel', 'value_ids': [v['id'] for v in candidates['values']]}]}
    model = SimpleNamespace(invoke=lambda _: SimpleNamespace(content=json.dumps(choice)))
    asl, _ = bind(extraction, knowledge, model)
    assert asl['ambiguity'] == []
    assert asl['filters'] == [{'field': 'sales.channel', 'operator': operator, 'value': expected}]
    assert asl['time_context'] is None and asl['limit'] is None


def test_enums_do_not_create_authorized_fields_or_cross_data_sources(monkeypatch):
    physical, attrs, doc = sources()
    attrs[0]['data_source_id'] = 999
    loaders(monkeypatch, physical, attrs, doc)
    assert sync.prepare_physical_catalog(900, 901, embed).attribute_enums == {}
    store, _, _, _ = publish(monkeypatch)
    knowledge = {'entities': [r for r in store.records.values() if r.metadata['type'] == 'entity'],
                 '_vector_authorized_fields': ['different.field']}
    assert catalog_candidates(knowledge)['values'] == []


def test_same_label_on_another_field_is_not_replaced_or_confused(monkeypatch):
    store, _, _, _ = publish(monkeypatch)
    attrs = [r for r in store.records.values() if r.metadata['type'] == 'attribute']
    other = deepcopy(attrs[0]); other.id += ':other'
    other.metadata.update(field_mapping='manufacturer.name', attr_code='name', attr_name='厂牌',
                          enum_values=[{'code': 'M99', 'name': '其他'}])
    values = catalog_candidates({'attributes': attrs + [other],
        '_vector_authorized_fields': ['sales.channel', 'manufacturer.name']})['values']
    assert {(v['field'], v['value']) for v in values if v.get('label') == '其他'} == {
        ('sales.channel', '8'), ('manufacturer.name', 'M99')}


@pytest.mark.parametrize('enums,expected', [
    (['直销', '其他'], {('直销', None), ('其他', None)}),
    ({'3': '直销', '8': '其他'}, {('3', '直销'), ('8', '其他')}),
    ({'value': 8, 'label': '其他'}, {(8, '其他')}),
])
def test_common_field_enum_formats_are_all_available_to_asl(monkeypatch, enums, expected):
    physical, attrs, doc = sources(enums=enums)
    loaders(monkeypatch, physical, attrs, doc)
    plan = sync.prepare_physical_catalog(900, 901, embed)
    store = Store()
    vector_store.rebuild_index_by_scope(store, embed, 900, 901, attribute_enums=plan.attribute_enums)
    knowledge = {'entities': [r for r in store.records.values() if r.metadata['type'] == 'entity'],
                 '_vector_authorized_fields': ['sales.channel']}
    assert {(v['value'], v.get('label')) for v in catalog_candidates(knowledge)['values']} == expected


def test_model_wide_groups_multiple_data_sources_and_preserves_foreign_model(monkeypatch):
    physical, attrs, doc = sources()
    second, second_attrs, _ = sources(ds=91)
    physical['tables'] += second['tables']
    # Same field mapping with distinct enum values must not silently choose a source.
    second['tables'][0]['fields'][0]['enum_values'] = [{'code': '9', 'name': '独立来源'}]
    second_attrs[0].update(entity_code='other_sales', attr_code='other_channel')
    loaders(monkeypatch, physical, attrs + second_attrs, doc)
    plan = sync.prepare_physical_catalog(900, None, embed)
    foreign, _, _, _ = publish(monkeypatch, model=999, ds=99, domain=998)
    store = Store(foreign.records.values()); before = deepcopy(store.records)
    result = sync.publish_physical_catalog(store, plan)
    assert result['by_type'] == {'table': 2, 'field': 2}
    assert store.records['ds90:field:sales.channel'].metadata['enum_values'][1]['code'] == '8'
    assert store.records['ds91:field:sales.channel'].metadata['enum_values'][0]['code'] == '9'
    assert {key: store.records[key] for key in before} == before


def test_explicit_domain_republish_keeps_other_tables_families_and_models(monkeypatch):
    store, plan, _, _ = publish(monkeypatch)
    existing = VectorRecord('ds90:field:sales.obsolete', 'Old field', [1., .5],
                           dict(type='field', semantic_model_id=900, data_source_id=90, parent='sales'))
    other = [VectorRecord(str(i), 'Foreign', [1., .5], metadata) for i, metadata in enumerate([
        dict(type='field', semantic_model_id=900, data_source_id=90, parent='other_domain_table'),
        dict(type='field', semantic_model_id=999, data_source_id=99, parent='sales'),
        dict(type='entity_attribute_value', semantic_model_id=900, business_domain_id=901),
        dict(type='daily_business', semantic_model_id=900, business_domain_id=901),
    ])]
    store.add([existing, *other]); before = {r.id: deepcopy(r) for r in other}
    result = sync.publish_physical_catalog(store, plan)
    assert result['deleted'] == 1
    assert existing.id not in store.records
    assert {key: store.records[key] for key in before} == before
    count = len(store.records)
    assert sync.publish_physical_catalog(store, plan)['deleted'] == 0
    assert len(store.records) == count


def test_empty_model_wide_snapshot_removes_only_its_physical_family(monkeypatch):
    store, _, _, _ = publish(monkeypatch)
    plan = sync.PhysicalCatalogPlan(900, None, [], set(), {})
    result = sync.publish_physical_catalog(store, plan)
    assert result['deleted'] == 2
    assert {r.metadata['type'] for r in store.records.values()} == {'entity', 'attribute'}


def test_empty_explicit_snapshot_does_not_wipe_shared_data_source(monkeypatch):
    store, _, _, _ = publish(monkeypatch)
    before = deepcopy(store.records)
    assert sync.publish_physical_catalog(store, sync.PhysicalCatalogPlan(900, 902, [], set(), {}))['deleted'] == 0
    assert store.records == before


@pytest.mark.parametrize('mutation', ['source', 'field_source', 'field_model', 'field_table', 'duplicate_table',
                                     'duplicate_field', 'missing_name', 'foreign_attribute', 'ambiguous_source'])
def test_invalid_registry_cannot_publish_or_guess_enum_ownership(monkeypatch, mutation):
    physical, attrs, doc = sources()
    table = physical['tables'][0]; field = table['fields'][0]
    if mutation == 'source': table['data_source_id'] = None
    if mutation == 'field_source': field['data_source_id'] = 99
    if mutation == 'field_model': field['semantic_model_id'] = 999
    if mutation == 'field_table': field['table_id'] = 99
    if mutation == 'duplicate_table': physical['tables'].append(deepcopy(table))
    if mutation == 'duplicate_field': table['fields'].append(deepcopy(field))
    if mutation == 'missing_name': field['field_name'] = ''
    if mutation == 'foreign_attribute': attrs[0]['business_domain_id'] = 902
    if mutation == 'ambiguous_source':
        second = deepcopy(table); second['data_source_id'] = 91
        second['fields'][0].update(data_source_id=91, enum_values=[{'code': '9', 'name': '其他'}])
        physical['tables'].append(second)
        extra = dict(attrs[0], data_source_id=91); attrs.append(extra)
    loaders(monkeypatch, physical, attrs, doc)
    with pytest.raises(ValueError, match='PHYSICAL_CATALOG_SOURCE_INVALID'):
        sync.prepare_physical_catalog(900, 901, embed)


def test_unowned_legacy_physical_registration_is_not_published(monkeypatch):
    physical, attrs, doc = sources()
    physical['tables'][0]['semantic_model_id'] = None
    loaders(monkeypatch, physical, attrs, doc)
    plan = sync.prepare_physical_catalog(900, 901, embed)
    assert plan.records == [] and plan.attribute_enums == {}


@pytest.mark.parametrize('embeddings', [[], [[], []], [[float('nan')], [1.0]]])
def test_embedding_failure_does_not_clear_old_indexes_or_report_success(monkeypatch, embeddings):
    physical, attrs, doc = sources()
    loaders(monkeypatch, physical, attrs, doc)
    store = Store()
    monkeypatch.setattr(api, '_store', store)
    monkeypatch.setattr(api, 'embed_documents', lambda texts: embeddings)
    monkeypatch.setattr(api, 'mysql_advisory_lock', lambda *a: nullcontext())
    monkeypatch.setattr(api, 'consistent_catalog_read', nullcontext)
    response = TestClient(api.app).post('/vector/rebuild', json={'semantic_model_id': 900, 'business_domain_id': 901})
    assert response.status_code != 200 and response.json()['success'] is False
    assert store.add_calls == 0 and store.deleted == []


@pytest.mark.parametrize('stage', ['upsert', 'missing', 'text', 'metadata', 'vector'])
def test_physical_write_readback_failures_keep_old_records(monkeypatch, stage):
    store, plan, _, _ = publish(monkeypatch)
    old = VectorRecord('ds90:field:sales.old', 'old', [1., .5],
                       dict(type='field', semantic_model_id=900, data_source_id=90, parent='sales'))
    store.add([old]); store.deleted.clear()
    if stage == 'upsert':
        monkeypatch.setattr(store, 'add', lambda records: (_ for _ in ()).throw(OSError('offline failure')))
    else:
        def readback(where):
            results = store.get_by_where(where)
            if stage == 'missing': return results[:-1]
            if stage == 'text': results[0].text = 'truncated'
            if stage == 'metadata': results[0].metadata['enum_values'] = []
            if stage == 'vector': results[0].vector = []
            return results
        monkeypatch.setattr(store, 'get_catalog_inventory', readback)
    with pytest.raises((OSError, sync.PhysicalCatalogPublicationError)):
        sync.publish_physical_catalog(store, plan)
    assert old.id in store.records and store.deleted == []


def test_existing_foreign_model_record_id_is_never_overwritten(monkeypatch):
    store, plan, _, _ = publish(monkeypatch)
    store.records[plan.records[0].id].metadata['semantic_model_id'] = 999
    calls = store.add_calls
    with pytest.raises(ValueError, match='belongs to another model'):
        sync.publish_physical_catalog(store, plan)
    assert store.add_calls == calls


def test_physical_readback_failure_does_not_mark_api_publish_success(monkeypatch):
    physical, attrs, doc = sources()
    loaders(monkeypatch, physical, attrs, doc)
    store = Store()
    monkeypatch.setattr(store, 'get_catalog_inventory', lambda where: [])
    monkeypatch.setattr(api, '_store', store)
    monkeypatch.setattr(api, 'embed_documents', embed)
    monkeypatch.setattr(api, 'mysql_advisory_lock', lambda *a: nullcontext())
    monkeypatch.setattr(api, 'consistent_catalog_read', nullcontext)
    response = TestClient(api.app).post('/vector/rebuild', json={'semantic_model_id': 900, 'business_domain_id': 901})
    assert response.status_code == 500 and response.json()['success'] is False


def test_scoped_physical_loader_includes_only_governed_main_and_subtables(monkeypatch):
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args): calls.append((sql, args))
        def fetchall(self): return []
    class Connection:
        def cursor(self, *args): return Cursor()
        def close(self): pass
    monkeypatch.setattr(mysql, '_get_connection', Connection)
    mysql.get_table_field_by_scope(900, business_domain_id=901)
    assert len(calls) == 2
    for sql, args in calls:
        assert args == [900, 900, 901]
        for guard in ['b.id=%s', 'b.semantic_model_id=%s', 's.entity_type_id=e.id',
                      's.semantic_model_id=b.semantic_model_id', 's.sub_table_name=t.name',
                      'e.data_source_id=t.data_source_id']:
            assert guard in sql


def test_server_only_like_filter_support_is_preserved():
    assert vector_store.MilvusVectorStore._compile_filter({'attr_name': {'$like': '%尾缀'}}) == 'attr_name like "%尾缀"'
    store = vector_store.MilvusVectorStore.__new__(vector_store.MilvusVectorStore)
    store.get_by_where = lambda where: [where]
    assert store.find_like({'attr_name': {'$like': '%名称'}}) == [{'attr_name': {'$like': '%名称'}}]
