"""Absent semantic codes do not invalidate existing scoped physical mappings."""
import pytest

from semantic_scope import ScopedTranslator
from sql_translator_prod import _mapping_backed_code
from test_single_domain_execution import catalog_db, backend, grant, asl


@pytest.fixture
def mapped(backend, catalog_db):
    catalog_db.execute("UPDATE semantic_model_entity_type SET code='' WHERE semantic_model_id=81 AND business_domain_id=205")
    catalog_db.execute('ALTER TABLE semantic_model_attribute_config ADD COLUMN code TEXT')
    return backend[0], catalog_db


@pytest.mark.parametrize('domains', [(), (205,)])
def test_existing_mapping_supports_translation_without_metadata_writes(mapped, domains):
    base, db = mapped
    translator = ScopedTranslator(grant(domains), base) if domains else base
    plan = translator.translate_only(asl(), '81')
    assert plan['success'], plan
    assert 'SUM(sales.amount)' in plan['sql']
    assert str(plan['data_source_id']) == '10'
    assert db.execute("SELECT code FROM semantic_model_entity_type WHERE id='sales'").fetchone()[0] == ''
    assert db.execute("SELECT code FROM semantic_model_attribute_config WHERE entity_type_id='sales'").fetchone()[0] is None


def test_loader_and_scoped_graph_keep_the_same_code_and_source(mapped):
    base, _ = mapped
    translator = ScopedTranslator(grant(), base)
    entity = translator.loader.get_entity('sales', '81')
    graph = translator.catalog.entity_relationship_metadata(81)
    assert entity['entity_code'] == 'sales'
    assert entity['attributes'][0]['attr_code'] == 'amount'
    assert graph['sales']['data_source_id'] == entity['data_source_id'] == 10
    assert graph['sales']['relations'] == []  # A foreign relation stays unavailable.


def test_metric_binding_resolves_existing_entity_id_with_blank_code(mapped):
    base, _ = mapped
    definition = ScopedTranslator(grant(), base).catalog.definition('81:sales_amount', 'current')
    assert definition['bound_entities'][0]['resolved_entity_code'] == 'sales'
    assert definition['bound_entities'][0]['resolved_entity_id'] == 'sales'


@pytest.mark.parametrize('foreign', ['inventory', 'foreign_sales', 'shared'])
def test_fallback_never_admits_another_domain_or_model(mapped, foreign):
    base, _ = mapped
    result = ScopedTranslator(grant(), base).translate_only(asl(foreign, foreign+'_amount'), '81')
    assert result['success'] is False


@pytest.mark.parametrize('mapping', [None, '', 'sales;drop'])
def test_missing_or_unsafe_mapping_still_fails_closed(mapped, mapping):
    base, db = mapped
    db.execute("UPDATE semantic_model_entity_type SET main_table_name=? WHERE id='sales'", (mapping,))
    with pytest.raises(ValueError, match='空编码'):
        base.loader.get_entity('sales', '81')


def test_duplicate_derived_code_is_not_first_wins(mapped):
    base, db = mapped
    db.execute("UPDATE semantic_model_entity_type SET code='',main_table_name='sales' WHERE id='inventory'")
    with pytest.raises(ValueError, match='重复编码'):
        base.loader.get_entity('sales', '81')


def test_explicit_codes_are_not_replaced_by_physical_names():
    assert _mapping_backed_code('canonical_sales', 'physical_sales') == 'canonical_sales'
    assert _mapping_backed_code('', 'physical_sales') == 'physical_sales'
