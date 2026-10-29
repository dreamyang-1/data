"""Production semantic lookup must survive an empty/unavailable Redis server."""
import json

import pymysql
import pytest

import sql_translator_prod as prod
from semantic_scope import ScopedLoader, ScopedTranslator, ScopeError
from test_single_domain_execution import catalog_db, backend, grant, asl


@pytest.fixture(autouse=True)
def no_semantic_redis(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Production semantic lookup touched Redis')
    monkeypatch.setattr(prod.redis, 'Redis', forbidden)


def test_default_loader_uses_mysql_without_creating_redis(backend):
    base, reads, _ = backend
    assert isinstance(base.loader, prod.MySQLDSLLoader)
    assert base.loader.source == 'mysql'
    plan = base.translate_only(asl(), '81')
    assert plan['success'], plan
    assert base.fetch_data_source('81', '10')['id'] == 10
    assert base.loader.get_entity('sales', '81')['attributes'][0]['mapping_column'] == 'amount'
    assert base.loader.iter_dimensions('81')
    assert base.loader.iter_scoped_dimensions('81')
    assert base.loader.resolve_relationship_paths(81, 'sales', ['foreign_sales'])['unresolved_targets'] == ['foreign_sales']
    assert all('semantic_model_id=%s' in sql for sql, _ in reads)


@pytest.mark.parametrize('model', [None, 0, -1, True, 1.2, '*', '81:*'])
def test_invalid_model_rejected_before_any_storage_read(backend, model):
    base, reads, _ = backend
    with pytest.raises(ValueError):
        base.loader.get_entity('sales', model)
    assert not reads


@pytest.mark.parametrize('kind,code,table,column', [
    ('entity', 'sales', 'entity_type', 'code'),
    ('metric', 'sales_amount', 'indicator', 'indicator_code'),
    ('dimension', 'sales_date', 'dimension', 'dim_code'),
])
def test_deleted_assets_do_not_reappear_from_legacy_cache(backend, catalog_db, kind, code, table, column):
    catalog_db.execute(f'UPDATE semantic_model_{table} SET is_deleted=1 WHERE {column}=?', (code,))
    assert getattr(backend[0].loader, 'get_' + kind)(code, '81') is None


def test_disabled_entity_not_executable(backend, catalog_db):
    catalog_db.execute('UPDATE semantic_model_entity_type SET status=0 WHERE code=?', ('sales',))
    result = backend[0].translate_only(asl(), '81')
    assert not result['success'] and result['error_code'] == 'SEMANTIC_ASSET_NOT_FOUND'


def test_same_code_in_another_model_has_independent_formula(backend, catalog_db):
    catalog_db.execute("UPDATE semantic_model_entity_type SET code='sales' WHERE semantic_model_id=82")
    catalog_db.execute("UPDATE semantic_model_indicator SET indicator_code='sales_amount' WHERE semantic_model_id=82")
    loader = backend[0].loader
    assert loader.get_metric('sales_amount', '81')['calculation_rule']['calc_formula'] == 'SUM(sales.amount)'
    assert loader.get_metric('sales_amount', '82')['calculation_rule']['calc_formula'] == 'SUM(foreign_sales.amount)'
    assert loader.get_entity('sales', '81')['data_source_id'] == 10
    assert loader.get_entity('sales', '82')['data_source_id'] == 12
    assert loader._find_entity_code_by_id('foreign_sales', '81') is None
    assert loader.find_all_entity_codes_by_table('foreign_sales', '81') == []


@pytest.mark.parametrize('reference', ['entity_code', 'entity_id'])
def test_metric_binding_supports_both_platform_schema_versions(backend, catalog_db, reference):
    if reference == 'entity_id':
        catalog_db.execute('ALTER TABLE semantic_model_entity_bind_indicator RENAME COLUMN entity_code TO entity_id')
    metric = backend[0].loader.get_metric('sales_amount', '81')
    assert metric['source_dependency']['bind_entity'] == ['sales']


def test_model_refresh_discards_old_formula_without_redis_write(backend, catalog_db):
    loader = backend[0].loader
    loader.get_metric('sales_amount', '81')
    loader.get_metric('foreign_sales_amount', '82')
    catalog_db.execute("UPDATE semantic_model_indicator SET calculation_formula='SUM(sales.amount)*2' WHERE indicator_code='sales_amount'")
    loader.clear_cache('81')
    assert set(loader._snapshots) == {82}
    assert loader.get_metric('sales_amount', '81')['calculation_rule']['calc_formula'] == 'SUM(sales.amount)*2'


def test_expired_memory_cache_does_not_hide_mysql_outage(backend, monkeypatch):
    loader = backend[0].loader
    assert loader.get_entity('sales', '81')
    loader._cache_ttl_seconds = 0
    def unavailable(*args, **kwargs):
        raise pymysql.err.OperationalError(2003, 'private connection detail')
    monkeypatch.setattr(loader.catalog, '_query', unavailable)
    result = backend[0].translate_only(asl(), '81')
    assert not result['success']
    assert result['error_code'] == 'SEMANTIC_CATALOG_UNAVAILABLE' and result['retryable']
    assert 'private' not in json.dumps(result)


def test_exact_missing_source_never_chooses_another_model_or_source(backend):
    base, _, _ = backend
    assert base.fetch_data_source('81', '12') is None
    assert base.fetch_data_source('81', '999') is None
    with pytest.raises(ValueError, match='多个数据源'):
        base.fetch_data_source('81')


def test_duplicate_active_code_is_not_resolved_by_row_order(backend, catalog_db):
    catalog_db.execute("INSERT INTO semantic_model_entity_type SELECT semantic_model_id,business_domain_id,is_deleted,'duplicate',code,name,main_table_name,data_source_id,status FROM semantic_model_entity_type WHERE code='sales'")
    with pytest.raises(ValueError, match='重复编码'):
        backend[0].loader.get_entity('sales', '81')


def test_scoped_loader_checks_model_before_database_read(backend):
    base, reads, _ = backend
    scoped = ScopedLoader(grant(), base.loader)
    with pytest.raises(ScopeError):
        scoped.get_entity('sales', '82')
    assert not reads


def test_explicit_scope_mysql_outage_returns_sanitized_retryable_failure(backend, monkeypatch):
    def unavailable(*args, **kwargs):
        raise pymysql.err.OperationalError(2003, 'private connection detail')
    monkeypatch.setattr(prod.SemanticCatalog, '_query', unavailable)
    result = ScopedTranslator(grant(), backend[0]).translate_only(asl(), '81')
    assert result['error_code'] == 'SEMANTIC_CATALOG_UNAVAILABLE'
    assert result['retryable'] and result['business_domain_ids'] == [205]
    assert 'private' not in json.dumps(result)


def test_metadata_reads_are_bounded_per_model_bundle(backend):
    base, reads, _ = backend
    base.loader.get_entity('sales', '81')
    first = len(reads)
    for _ in range(10):
        base.loader.get_metric('sales_amount', '81')
        base.loader.find_all_entity_codes_by_table('sales', '81')
        base.loader.iter_entities('81')
    assert first == 7 and len(reads) == first
