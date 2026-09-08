from copy import deepcopy
from unittest.mock import Mock
import pytest

from pinned_catalog import _digest, _PinnedTranslator, PinnedCatalogError, translate_pinned_catalog
from semantic_scope import RequestScope


def snapshot():
    scope = dict(semantic_model_id=81, business_domain_ids=[205], scope_mode='EXPLICIT_DOMAINS')
    fields = ['id', 'amount', 'name', 'ordered_at']
    entity = dict(entity_id='1', entity_code='orders', entity_name='Orders', business_domain=205,
        attributes=[dict(attribute_id=str(i+1), attr_code=field, attr_name=field,
                         field_mapping='orders.'+field, is_nullable=True) for i, field in enumerate(fields)], relations=[])
    metric = dict(metric_code='total', metric_name='Total', metric_level='原子指标', business_domain=205,
        calculation_rule=dict(calc_formula='SUM(orders.amount)', global_filters=[]),
        source_dependency=dict(bind_entity=['orders']))
    dimension = dict(dim_code='customer_name', dim_name='Customer name', dim_type='普通维度',
        bind_entities=[dict(entity='1', attr='3', mappingTable='orders', mappingColumn='name')])
    sources = dict(contract='catalog-sql-sources-v1', scope=scope,
        entities=[dict(entity_id='1', entity_code='orders', business_domain_id=205,
                       main_table_name='orders', data_source_id=7, sub_table_mappings=[])],
        metrics=[dict(metric_code='total', business_domain_id=205, dependency_codes=[])])
    s = dict(contract_version='catalog-release-v1', scope=scope, source_identity_hash='fixture',
        documents=[dict(semantic_model={'id':81, 'name':'Fixture model'}, business_domain={'id':205, 'name':'Fixture domain'}, entities=[entity], metrics=[metric], dimensions=[dimension])],
        physical_catalog=dict(scope=scope, sql_translation_sources=sources, tables=[dict(table_id=1,
            table_name='orders', semantic_model_id=81, data_source_id=7,
            fields=[dict(field_id=i+1, field_name=f, table_id=1, semantic_model_id=81, data_source_id=7,
                         data_type='DATETIME' if f == 'ordered_at' else 'VARCHAR') for i, f in enumerate(fields)])]))
    return seal(s)


def seal(s):
    s['catalog_version'] = _digest({k:v for k,v in s.items() if k != 'catalog_version'})
    return s


class Pin:
    def __init__(self, s=None):
        self.snapshot = s or snapshot()
        self.identity = dict(scope=deepcopy(self.snapshot['scope']), catalog_version=self.snapshot['catalog_version'],
            vector_index_version='fixture-index', catalog_publish_id='fixture-publication', activation_id='fixture-activation',
            target_identity_hash='fixture-target', records_verified=1)
        self.finish = Mock(side_effect=lambda: deepcopy(self.identity))


def asl():
    return dict(version='2.0', intent='query', subject={'entity':'orders'}, metrics=[{'name':'total'}],
        dimensions=[{'name':'customer_name'}], filters=[{'field':'orders.name', 'operator':'=', 'value':'Alice'}], limit=5)


def translate(pin=None, query=None, scope=None):
    return translate_pinned_catalog(pin or Pin(), scope or RequestScope(81, (205,)), query or asl())


def test_actual_translator_compiles_from_snapshot_and_finishes_before_acceptance(monkeypatch):
    import pymysql, redis
    monkeypatch.setattr(pymysql, 'connect', Mock(side_effect=AssertionError('Independent MySQL read')))
    monkeypatch.setattr(redis, 'Redis', Mock(side_effect=AssertionError('Legacy Redis read')))
    pin = Pin()
    result = translate(pin)
    assert result['success'], result
    assert 'SUM(orders.amount)' in result['sql'] and 'GROUP BY' in result['sql'] and 'LIMIT 5' in result['sql']
    assert 'Alice' in result['sql']
    assert result['catalog_pin'] == pin.identity
    pin.finish.assert_called_once()


@pytest.mark.parametrize('change', ['model', 'domain', 'mode', 'version', 'digest', 'sources', 'owner', 'dimension', 'source_id', 'field'])
def test_cross_scope_stale_or_inconsistent_catalog_cannot_generate_sql(change):
    s = snapshot()
    if change == 'model': s['scope']['semantic_model_id'] = 82
    elif change == 'domain': s['scope']['business_domain_ids'] = [206]
    elif change == 'mode': s['scope']['business_domain_ids'] = []; s['scope']['scope_mode'] = 'MODEL_WIDE'
    elif change == 'sources': s['physical_catalog'].pop('sql_translation_sources')
    elif change == 'owner': s['physical_catalog']['sql_translation_sources']['entities'][0]['entity_id'] = 'wrong'
    elif change == 'dimension': s['documents'][0]['dimensions'][0]['bind_entities'][0]['attr'] = 'wrong'
    elif change == 'source_id': s['physical_catalog']['sql_translation_sources']['entities'][0]['data_source_id'] = 8
    elif change == 'field': s['documents'][0]['entities'][0]['attributes'][0]['field_mapping'] = 'orders.secret'
    seal(s)
    pin = Pin(s)
    if change == 'version': pin.identity['catalog_version'] = 'stale'
    if change == 'digest': s['documents'][0]['metrics'][0]['calculation_rule']['calc_formula'] = 'MAX(orders.amount)'
    result = translate(pin)
    assert not result['success'] and result['sql'] is None
    pin.finish.assert_not_called()


@pytest.mark.parametrize('scope', [RequestScope(82, (205,)), RequestScope(81, (206,)), RequestScope(81),
    RequestScope(81, (205,206)), RequestScope(True, (205,)), RequestScope('81', (205,)), RequestScope(81, (205,), 8)])
def test_current_upstream_scope_is_the_only_authority(scope):
    assert not translate(scope=scope)['success']


def test_database_and_knowledge_scope_fingerprints_do_not_cross_reuse():
    a = translate(scope=RequestScope(81, (205,), 7, ('a',)))
    b = translate(scope=RequestScope(81, (205,), 7, ('b',)))
    assert a['success'] and b['success']
    assert a['authorized_scope_fingerprint'] != b['authorized_scope_fingerprint']


@pytest.mark.parametrize('fault', ['authority', 'index', 'activation'])
def test_drift_at_acceptance_withholds_generated_sql(fault):
    pin = Pin()
    if fault == 'activation': pin.finish.side_effect = lambda: {**pin.identity, 'activation_id':'changed'}
    else: pin.finish.side_effect = ValueError('private catalog data must not leak')
    result = translate(pin)
    assert not result['success'] and result['sql'] is None
    assert 'private catalog' not in str(result)
    pin.finish.assert_called_once()


@pytest.mark.parametrize('key,value', [('version','1.0'), ('intent','execute'), ('business_domain_ids',[]),
    ('authorized_semantic_scope',{}), ('semantic_model_id',82), ('database_id',7), ('knowledge_base_names',[])])
def test_asl_cannot_override_authority_or_version(key, value):
    query = asl(); query[key] = value
    assert not translate(query=query)['success']


@pytest.mark.parametrize('method', ['fetch_data_source','execute_query','execute_sql_only','execute_sql_on_data_source'])
def test_plan_only_entry_never_executes_business_sql(method):
    translator = _PinnedTranslator(RequestScope(81, (205,)), snapshot())
    with pytest.raises(PinnedCatalogError, match='PLAN_ONLY'):
        getattr(translator, method)('arbitrary')


def test_detail_projection_and_explicit_time_use_same_snapshot():
    query = asl(); query['metrics'] = []
    query['dimensions'] = [{'name':'orders.name'}, {'name':'orders.ordered_at'}]
    query['time_context'] = {'type':'range', 'anchor':'orders.ordered_at', 'start':'2026-01-01', 'end':'2026-03-31'}
    result = translate(query=query)
    assert result['success'], result
    assert 'GROUP BY' not in result['sql'] and 'orders.ordered_at' in result['sql']


@pytest.mark.parametrize('query', [dict(filters=[{'field':'secret', 'operator':'=', 'value':'x'}]),
    dict(filters=[{'field':'foreign.name', 'operator':'=', 'value':'x'}]),
    dict(time_context={'type':'year', 'value':2026, 'anchor':'orders.secret'})])
def test_unregistered_fields_are_not_recovered_by_physical_name_guessing(query):
    assert not translate(query={**asl(), **query})['success']


@pytest.mark.parametrize('fault', [None, 'authority', 'inventory', 'activation', 'reuse'])
def test_real_publication_pin_and_sql_translator_share_acceptance(monkeypatch, fault):
    # Cross-service contract test; persistence and embeddings are in memory.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'Oagnet'
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.syspath_prepend(str(root / 'tests'))
    from test_catalog_publication import MemoryStore, FakeRedis, embed
    from catalog_registry import RedisCatalogReleaseRegistry
    from catalog_publication import CatalogPublication
    s = snapshot(); store = MemoryStore()
    registry = RedisCatalogReleaseRegistry(store.catalog_target_identity, FakeRedis())
    publication = CatalogPublication(store, registry, lambda *args: deepcopy(s))
    publication.publish(81, [205], embed_fn=embed, publication_id='fixture-sql',
                        producer_revision='fixture', embedding_contract='fixture')
    pin = publication.pin(81, [205])
    original = _PinnedTranslator.translate_only
    def translate_then_drift(self, *args):
        result = original(self, *args)
        if fault == 'authority':
            s['physical_catalog']['sql_translation_sources']['entities'][0]['data_source_id'] = 8
            seal(s)
        elif fault == 'inventory': store.records.pop(next(iter(store.records)))
        elif fault == 'activation':
            monkeypatch.setattr(registry, 'active', lambda *args: None)
        return result
    monkeypatch.setattr(_PinnedTranslator, 'translate_only', translate_then_drift)
    result = translate(pin)
    assert result['success'] is (fault in (None, 'reuse')), result
    if fault == 'reuse': assert not translate(pin)['success']
    elif fault is not None: assert result['sql'] is None


@pytest.mark.parametrize('level', [1, None, '原子指标'])
def test_numeric_or_missing_catalog_level_does_not_change_formula(level):
    s = snapshot(); s['documents'][0]['metrics'][0]['metric_level'] = level
    result = translate(Pin(seal(s)))
    assert result['success'] and 'SUM(orders.amount)' in result['sql']


@pytest.mark.parametrize('fault', [None, 'cycle', 'missing'])
def test_declared_dependencies_expand_without_interpreting_numeric_level(fault):
    s = snapshot(); doc = s['documents'][0]
    metric = deepcopy(doc['metrics'][0]); metric['metric_code'] = 'double_total'; metric['metric_level'] = 3
    metric['calculation_rule']['calc_formula'] = 'total * 2'
    doc['metrics'].append(metric)
    s['physical_catalog']['sql_translation_sources']['metrics'].append(dict(metric_code='double_total',
        business_domain_id=205, dependency_codes=['double_total' if fault == 'cycle' else 'missing' if fault == 'missing' else 'total']))
    query = asl(); query['metrics'] = [{'name':'double_total'}]
    result = translate(Pin(seal(s)), query)
    assert result['success'] is (fault is None), result
    if fault is None: assert 'SUM(orders.amount) * 2' in result['sql']


@pytest.mark.parametrize('fault', [None, 'missing_endpoint', 'foreign_source'])
def test_structured_declared_relationship_is_used_without_inventing_endpoints(fault):
    s = snapshot(); doc = s['documents'][0]; sources = s['physical_catalog']['sql_translation_sources']
    customer = deepcopy(doc['entities'][0]); customer.update(entity_id='2', entity_code='customers')
    for i, attr in enumerate(customer['attributes']):
        attr['attribute_id'] = str(i+101); attr['field_mapping'] = attr['field_mapping'].replace('orders.', 'customers.')
    doc['entities'].append(customer)
    source = deepcopy(sources['entities'][0]); source.update(entity_id='2', entity_code='customers', main_table_name='customers')
    sources['entities'].append(source)
    table = deepcopy(s['physical_catalog']['tables'][0]); table.update(table_id=2, table_name='customers')
    for field in table['fields']: field['table_id'] = 2
    s['physical_catalog']['tables'].append(table)
    if fault == 'foreign_source':
        table['data_source_id'] = source['data_source_id'] = 8
        for field in table['fields']: field['data_source_id'] = 8
    doc['entities'][0]['relations'] = [dict(relation_code='ordered_by', relation_type='N:1', target_entity='2',
        join_key=dict(source_field='orders.id', target_field='customers.missing' if fault == 'missing_endpoint' else 'customers.id'))]
    query = asl(); query['dimensions'] = [{'name':'customers.name'}]
    result = translate(Pin(seal(s)), query)
    assert result['success'] is (fault is None), result
    if fault is None: assert 'customers.id' in result['sql'] and 'JOIN' in result['sql']
    if fault == 'missing_endpoint':
        # An unusable relationship cannot authorize a join, but does not block
        # an unrelated plan that reads only its own entity.
        assert translate(Pin(seal(s)), asl())['success']


@pytest.mark.parametrize('query,expected', [
    ('SELECT SUM(t.x) AS `amount`, t.y AS `label` FROM t', ['amount','label']),
    ("SELECT CONCAT('FROM, AS `fake`', t.x) AS `real` FROM t WHERE t.x=' AS `other`'", ['real']),
    ("SELECT DISTINCT CASE WHEN t.x=1 THEN 'a,b' ELSE 'FROM' END AS `label` FROM t", ['label']),
    ("SELECT CAST(t.x AS DECIMAL(10,2)) AS `number` FROM t", ['number']),
    ("SELECT 'a'' FROM ''b' AS `label` FROM t", ['label']),
    ('SELECT t.x FROM t', None), ('SELECT 1 AS `x`', None),
    ('SELECT (t.x AS `x` FROM t', None), ('DELETE FROM t', None),
    ('SELECT t.x /* AS `fake` FROM t */ AS `real` FROM t', None)])
def test_projection_alias_evidence_comes_from_select_columns(query,expected):
    from pinned_catalog import sql_projection_aliases
    assert sql_projection_aliases(query)==expected
