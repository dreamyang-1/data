"""The real catalog validators must report actionable metadata, not secrets."""
from copy import deepcopy
import json

import pytest

from catalog_generation import _dsl_inventory
from catalog_release import CatalogEvidenceError
from catalog_sql_sources import capture_sql_sources
import catalog_value_sources as values
from test_catalog_value_sources import source, SCOPE
from test_catalog_sql_sources import install, rows
from test_catalog_publication import authority


def test_all_missing_entity_and_attribute_codes_are_reported_once(monkeypatch):
    records = [source(entity_code="", entity_name="医院", attr_code=""),
               source(entity_code="", entity_name="医院", attribute_id=1206, attr_code="name")]
    monkeypatch.setattr(values, "_definition_rows", lambda scope: deepcopy(records))
    with pytest.raises(CatalogEvidenceError) as caught:
        values.capture_value_sources(SCOPE)
    exc = caught.value
    assert str(exc) == "CATALOG_VALUE_SOURCE_MAPPING_INVALID"
    assert len(exc.issues) == 2
    assert [v["key"] for v in exc.issues] == ["entity_code", "attr_code"]
    assert exc.issues[1]["attribute_id"] == 1205
    assert exc.issues[1]["mapping_column"] == "city"
    encoded = json.dumps(exc.issues)
    for secret in ("fixture-secret", "fixture_login", "fixture.invalid", "fixture_business"):
        assert secret not in encoded


@pytest.mark.parametrize("key,new", [("entity_code", ""), ("attr_code", None),
    ("mapping_table", ""), ("mapping_column", " "), ("host", None), ("db_name", ""),
    ("db_type", ""), ("port", 0), ("attribute_id", True), ("data_source_id", "7")])
def test_each_configuration_slot_reports_the_actual_key(monkeypatch, key, new):
    monkeypatch.setattr(values, "_definition_rows", lambda scope: [source(**{key: new})])
    with pytest.raises(CatalogEvidenceError) as caught:
        values.capture_value_sources(SCOPE)
    assert any(issue["key"] == key for issue in caught.value.issues)
    assert "password" not in json.dumps(caught.value.issues)


def test_foreign_scope_failure_does_not_disclose_foreign_object(monkeypatch):
    monkeypatch.setattr(values, "_definition_rows", lambda scope: [source(business_domain_id=206, entity_name="foreign")])
    with pytest.raises(CatalogEvidenceError) as caught:
        values.capture_value_sources(SCOPE)
    assert caught.value.issues == ({"key": "scope", "reason": "OWNER_MISMATCH"},)


def test_duplicate_physical_mapping_keeps_error_code_and_object(monkeypatch):
    monkeypatch.setattr(values, "_definition_rows", lambda scope: [source(), source(field_id=3)])
    with pytest.raises(CatalogEvidenceError, match="MAPPING_AMBIGUOUS") as caught:
        values.capture_value_sources(SCOPE)
    assert caught.value.issues[0]["attr_code"] == "city"


@pytest.mark.parametrize("kind,key", [("attribute", "attr_code"), ("relation", "relation_code"), ("metric", "metric_code")])
def test_generation_missing_codes_keep_their_owner(kind, key):
    doc = authority()["documents"][0]
    if kind == "metric":
        target = doc["metrics"][0]
    elif kind == "attribute":
        target = doc["entities"][0]["attributes"][0]
    else:
        target = {"relation_name": "上级医院", "target_entity": "hospital"}
        doc["entities"][0]["relations"] = [target]
    target[key] = ""
    with pytest.raises(CatalogEvidenceError, match="STABLE_ID_REQUIRED") as caught:
        _dsl_inventory(doc)
    issue = caught.value.issues[0]
    assert issue["key"] == key
    if kind != "metric":
        assert issue["entity_code"] == "hospital"


@pytest.mark.parametrize("fault,key", [("join", "sub_join_column"), ("metric", "indicator_code"),
    ("dependency", "dependence_atomic_indicator"), ("table", "main_table_name")])
def test_sql_metadata_errors_name_the_failed_configuration(monkeypatch, fault, key):
    data = rows()
    if fault == "join": data[1][0][key] = ""
    elif fault == "metric": data[2].append(deepcopy(data[2][0]))
    elif fault == "dependency": data[2][0][key] = '{"invalid": 1}'
    else: data[0][0][key] = ""
    install(monkeypatch, data)
    with pytest.raises(CatalogEvidenceError) as caught:
        capture_sql_sources(SCOPE)
    assert caught.value.issues[0]["key"] == key
    assert "invalid" not in json.dumps(caught.value.issues)
