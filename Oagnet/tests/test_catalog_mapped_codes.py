"""Read-time code projection uses existing metadata, never UI/database writes."""
from copy import deepcopy

import pytest

import mysql_tool as mysql
import catalog_value_sources as values
from catalog_release import CatalogEvidenceError, catalog_scope
from catalog_generation import build_catalog_records
from catalog_sql_sources import capture_sql_sources
from test_catalog_model_isolation import metadata_db, ENTITY
from test_catalog_publication import authority, reseal
from test_catalog_sql_sources import rows, install
from test_catalog_value_sources import source, Connection

SCOPE = catalog_scope(81, [205])


@pytest.fixture
def blank_metadata(metadata_db):
    mysql._query("UPDATE semantic_model_entity_type SET code='',main_table_name='hospitals' WHERE semantic_model_id=%s", (81,))
    mysql._query("UPDATE semantic_model_attribute_config SET code='' WHERE semantic_model_id=%s", (81,))
    mysql._query("UPDATE semantic_model_relation_config SET code='' WHERE semantic_model_id=%s", (81,))
    return metadata_db


@pytest.mark.parametrize("declared", ["canonical_code", "legacy.alias", " legacy_code "])
def test_existing_codes_are_not_renamed(declared):
    assert mysql._catalog_code(declared, "physical_column") == declared


@pytest.mark.parametrize("absent", [None, "", "  "])
def test_absent_codes_project_safe_governed_mapping(absent):
    assert mysql._catalog_code(absent, "physical_column") == "physical_column"


@pytest.mark.parametrize("mapping", [None, "", "bad;column", "table.column", "中文显示名"])
def test_unusable_mappings_cannot_invent_a_code(mapping):
    assert mysql._catalog_code("", mapping) == ""


@pytest.mark.parametrize("invalid", [True, 7, ["code"]])
def test_nonempty_invalid_code_is_not_hidden_by_fallback(invalid):
    assert mysql._catalog_code(invalid, "physical_column") is invalid


def test_mysql_dsl_discovery_and_value_sources_agree_without_metadata_writes(blank_metadata):
    before = deepcopy(mysql._query("SELECT code FROM semantic_model_attribute_config WHERE semantic_model_id=%s", (81,)))
    entity = mysql.get_entity(205)[0]
    registered = mysql.get_registered_entity_attributes(81, 205)[0]
    field = values.capture_value_sources(SCOPE)["fields"][0]
    assert entity["entity_code"] == registered["entity_code"] == field["entity_code"] == "hospitals"
    assert entity["attributes"][0]["attr_code"] == registered["attr_code"] == field["attr_code"] == "name"
    assert entity["entity_id"] == field["entity_id"] == ENTITY
    relation = entity["relations"][0]
    assert relation["relation_code"] == "relation_81"
    assert relation["target_entity"] == "hospitals"
    assert relation["join_key"]["source_field"] == "hospitals.name"
    assert relation["relation_type"] is None  # Missing cardinality is not invented.
    assert mysql.get_entity(206)[0]["entity_code"] == "hospital"
    assert mysql.get_entity(206)[0]["attributes"][0]["field_mapping"] == "hospitals.secret"
    assert mysql._query("SELECT code FROM semantic_model_attribute_config WHERE semantic_model_id=%s", (81,)) == before
    assert mysql._query("SELECT code FROM semantic_model_entity_type WHERE semantic_model_id=%s", (81,))[0]["code"] == ""


def test_projected_codes_survive_generation_and_exact_source_binding(blank_metadata):
    snapshot = authority()
    snapshot["documents"][0]["entities"] = mysql.get_entity(205)
    snapshot["documents"][0]["metrics"] = []
    snapshot["documents"][0]["dimensions"] = []
    snapshot["physical_catalog"]["entity_value_sources"] = values.capture_value_sources(SCOPE)
    records, _ = build_catalog_records(reseal(snapshot), lambda texts: [[.1, .2] for _ in texts])
    attribute = next(r for r in records if r.metadata["type"] == "attribute")
    field = values.bound_field(snapshot, attribute)
    assert field["entity_code"] == "hospitals" and field["attr_code"] == "name"
    assert len([r for r in records if r.metadata["type"] == "relation"]) == 1


@pytest.mark.parametrize("absent", [None, "", " \t"])
def test_sql_capture_uses_the_same_effective_entity_code(monkeypatch, absent):
    data = rows()
    data[0][0]["entity_code"] = absent
    install(monkeypatch, data)
    assert capture_sql_sources(SCOPE)["entities"][0]["entity_code"] == "orders"


def test_value_lookup_rechecks_the_same_projected_mapping(monkeypatch):
    row = source(entity_code="", main_table_name="hospitals", attr_code="")
    field = values.field_identity(row, SCOPE)
    monkeypatch.setattr(values, "_definition_rows", lambda *a, **kw: [deepcopy(row)])
    connection = Connection(["example"])
    monkeypatch.setattr(mysql.pymysql, "connect", lambda **kw: connection)
    assert values.query_values(SCOPE, field, "example", 8) == ["example"]
    assert connection.sql[1][1] == ("example", 9)
    assert connection.closed == connection.rolled_back == 1


def test_attribute_fallback_collision_reports_exact_field(blank_metadata):
    blank_metadata('semantic_model_attribute_config', id='abcdef0123456789abcdef0123456788',
        semantic_model_id=81, entity_type_id=ENTITY, code='name', attr_name='Duplicate',
        mapping_table='hospitals', mapping_column='name', is_deleted=0)
    with pytest.raises(CatalogEvidenceError, match="SOURCE_ID_COLLISION") as caught:
        mysql.get_entity(205)
    assert caught.value.issues[0]["key"] == "attr_code"
    assert caught.value.issues[0]["mapping_column"] == "name"


def test_entity_fallback_collision_is_not_silently_merged(blank_metadata):
    blank_metadata('semantic_model_entity_type', id='12345678-1234-4567-89ab-123456789abd',
        semantic_model_id=81, business_domain_id=205, code='hospitals', name='Duplicate',
        main_table_name='hospitals', is_deleted=0, status=1, data_source_id=7)
    with pytest.raises(CatalogEvidenceError, match="SOURCE_ID_COLLISION"):
        mysql.get_entity(205)


def test_value_source_collision_is_not_first_wins(monkeypatch):
    row = source(entity_code="", main_table_name="hospitals", attr_code="")
    monkeypatch.setattr(values, "_definition_rows", lambda scope: [row, {**row, "attribute_id": 1206}])
    with pytest.raises(CatalogEvidenceError, match="MAPPING_AMBIGUOUS") as caught:
        values.capture_value_sources(SCOPE)
    assert caught.value.issues[0]["key"] == "attr_code"


def test_metric_formula_dependencies_include_mapping_backed_entities(monkeypatch):
    def query(sql, args=None):
        if "SELECT id, project_id" in sql:
            return [dict(indicator_code='total', indicator_name='Total', business_domain_id=205,
                calculation_formula='SUM(orders.amount)')]
        if "SELECT e.code, e.main_table_name" in sql:
            assert "e.code<>''" not in sql
            assert "e.semantic_model_id=b.semantic_model_id" in sql
            return [dict(code='', main_table_name='orders', business_domain_id=205)]
        return []
    monkeypatch.setattr(mysql, "_query", query)
    assert mysql.get_metric(81, 205)[0]["source_dependency"]["bind_entity"] == ['orders']


def test_relation_fallback_preserves_declared_keys_and_uuid():
    row = dict(id=ENTITY, code='', type='1:N', target_entity_code='orders',
        source_table_column_name='departments-code', target_table_column_name='orders.department')
    relation = mysql._row_to_relation_dict(row)
    assert relation['relation_code'] == 'relation_' + ENTITY.replace('-', '_')
    assert relation['relation_type'] == '1:N'
    assert relation['join_key'] == dict(source_field='departments.code', target_field='orders.department')
    assert row['code'] == ''
