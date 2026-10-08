from copy import deepcopy
import pytest

from catalog_release import CatalogEvidenceError, catalog_scope, snapshot_from_documents
from catalog_sql_sources import capture_sql_sources


def rows():
    return ([dict(entity_id=1, entity_code='orders', business_domain_id=205,
                  data_source_id=7, main_table_name='orders')],
            [dict(entity_type_id=1, sub_table_name='lines', main_join_column='id', sub_join_column='order_id')],
            [dict(indicator_code='total', business_domain_id=205, dependence_atomic_indicator='base,other')])


def install(monkeypatch, values):
    import mysql_tool
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        if 'SELECT e.id AS entity_id' in sql: return deepcopy(values[0])
        if 'SELECT s.entity_type_id' in sql: return deepcopy(values[1])
        if 'SELECT indicator_code' in sql: return deepcopy(values[2])
        if 'SELECT DISTINCT t.*' in sql or 'SELECT DISTINCT f.*' in sql: return []
        raise AssertionError('Unexpected catalog query')
    monkeypatch.setattr(mysql_tool, '_query', query)
    return calls


@pytest.mark.parametrize('domains', [[], [205]])
def test_sql_facts_are_scoped_metadata_only_and_preserve_declared_join(monkeypatch, domains):
    calls = install(monkeypatch, rows())
    result = capture_sql_sources(catalog_scope(81, domains))
    assert result['entities'][0]['sub_table_mappings'] == [dict(sub_table_name='lines', main_join_column='id', sub_join_column='order_id')]
    assert result['metrics'][0]['dependency_codes'] == ['base', 'other']
    assert len(calls) == 5
    assert all(sql.lstrip().startswith('SELECT') and params == (81, *domains) for sql, params in calls)
    assert all(('AND b.id=%s' in sql or 'AND business_domain_id=%s' in sql) == bool(domains) for sql, _ in calls)
    assert all(word not in ' '.join(sql for sql, _ in calls).lower() for word in ('password', 'username', 'host', 'redis'))


@pytest.mark.parametrize('fault', ['duplicate_entity', 'missing_owner', 'duplicate_subtable', 'duplicate_metric', 'invalid_dependency', 'invalid_source', 'invalid_table'])
def test_unprovable_sql_sources_fail_closed(monkeypatch, fault):
    values = rows()
    if fault == 'duplicate_entity': values[0].append(deepcopy(values[0][0]))
    elif fault == 'missing_owner': values[1][0]['entity_type_id'] = 2
    elif fault == 'duplicate_subtable': values[1].append(deepcopy(values[1][0]))
    elif fault == 'duplicate_metric': values[2].append(deepcopy(values[2][0]))
    elif fault == 'invalid_dependency': values[2][0]['dependence_atomic_indicator'] = '{"unrecognized":1}'
    elif fault == 'invalid_source': values[0][0]['data_source_id'] = True
    else: values[0][0]['main_table_name'] = 'orders;delete'
    install(monkeypatch, values)
    with pytest.raises(CatalogEvidenceError): capture_sql_sources(catalog_scope(81, [205]))


@pytest.mark.parametrize('change', ['data_source_id', 'main_table_name', 'sub_table_mappings', 'dependency_codes'])
def test_each_sql_planning_fact_changes_catalog_version(monkeypatch, change):
    install(monkeypatch, rows())
    scope = catalog_scope(81, [205])
    facts = capture_sql_sources(scope)
    physical = {'scope': scope, 'tables': [], 'sql_translation_sources': facts}
    docs = [{'semantic_model': {'id':81}, 'business_domain': {'id':205}}]
    before = snapshot_from_documents(scope, {'server_uuid':'fixture'}, docs, physical)
    target = facts['metrics'][0] if change == 'dependency_codes' else facts['entities'][0]
    target[change] = 'changed'
    after = snapshot_from_documents(scope, {'server_uuid':'fixture'}, docs, physical)
    assert before['catalog_version'] != after['catalog_version']


def test_subtable_physical_metadata_keeps_model_domain_and_source_ownership(monkeypatch):
    import mysql_tool
    calls = install(monkeypatch, rows())
    original = mysql_tool._query
    def query(sql, params):
        if 'SELECT DISTINCT' not in sql: return original(sql, params)
        calls.append((sql, params))
        assert 's.entity_type_id=e.id' in sql and 's.semantic_model_id=b.semantic_model_id' in sql
        assert 't.data_source_id=ds.id' in sql and 'ds.id=e.data_source_id' in sql
        assert 't.semantic_model_id=b.semantic_model_id' in sql and 'AND b.id=%s' in sql
        assert params == (81, 205)
        if 'SELECT DISTINCT t.*' in sql:
            return [dict(id=9, name='lines', semantic_model_id=81, data_source_id=7)]
        assert 'f.semantic_model_id=t.semantic_model_id' in sql and 'f.data_source_id=t.data_source_id' in sql
        return [dict(id=11, name='order_id', type='BIGINT', table_id=9, semantic_model_id=81, data_source_id=7)]
    monkeypatch.setattr(mysql_tool, '_query', query)
    result = capture_sql_sources(catalog_scope(81, [205]))
    assert result['tables'][0]['table_name'] == 'lines'
    assert result['tables'][0]['fields'][0]['field_name'] == 'order_id'
