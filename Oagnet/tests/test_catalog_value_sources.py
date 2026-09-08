"""Real publication/read code with fake metadata and business connections only."""
from copy import deepcopy
import json

import pytest

import catalog_value_sources as values
import mysql_tool as mysql
from catalog_release import CatalogEvidenceError, catalog_scope, capture_catalog, CATALOG_TABLES
from test_catalog_publication import authority, publish, reseal, system
from catalog_generation import build_catalog_records

ATTRIBUTE = "sm81_bd205:attr:hospital.city"
SCOPE = catalog_scope(81, [205])


def attribute_id(pin):
    return next(row.id for row in pin.get_by_where({"type": "attribute"})
                if row.metadata["catalog_logical_id"] == ATTRIBUTE)


def lookup(pin, value, **kwargs):
    return pin.lookup_entity_values(attribute_id(pin), value, **kwargs)


def source(**changes):
    return dict(semantic_model_id=81, business_domain_id=205, entity_id=205,
        entity_code="hospital", attribute_id=1205, attr_code="city", attr_name="城市",
        data_type="varchar", mapping_table="hospitals", mapping_column="city",
        vectorization=0, is_main_attribute=1, table_id=1, field_id=2,
        data_source_id=7, db_type="MySQL", host="fixture.invalid", port=3306,
        db_name="fixture_business", username="fixture_login", password="fixture-secret") | changes


@pytest.fixture
def catalog(monkeypatch):
    current = [source()]
    calls = []
    def definitions(sql, args=None):
        calls.append((sql, args))
        return deepcopy(current)
    monkeypatch.setattr(mysql, "_query", definitions)
    snapshot = authority()
    snapshot["physical_catalog"]["entity_value_sources"] = values.capture_value_sources(SCOPE)
    reseal(snapshot)
    service, store, registry, redis, _, overrides = system()
    overrides[(81, (205,))] = snapshot
    publish(service)
    return service, current, calls, overrides, store, registry, redis


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []
        self.closed = self.rolled_back = 0
        self.error = None
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args=None):
        self.sql.append((sql, args))
        if self.error: raise RuntimeError(self.error)
    def fetchall(self): return [(v,) for v in self.rows]
    def rollback(self): self.rolled_back += 1
    def close(self): self.closed += 1


@pytest.fixture
def business(monkeypatch):
    rows, opened = ["甲城"], []
    def connect(**kwargs):
        conn = Connection(deepcopy(rows))
        opened.append((conn, kwargs))
        return conn
    monkeypatch.setattr(mysql.pymysql, "connect", connect)
    return rows, opened


def test_capture_retains_governed_mapping_and_route_without_credentials(catalog):
    service, _, calls, *_ = catalog
    pin = service.pin(81, [205])
    field = pin.entity_value_source(attribute_id(pin), data_source_id=7)
    assert (field["entity_id"], field["attribute_id"], field["table_id"], field["field_id"]) == ("205", "1205", 1, 2)
    assert field["vectorization"] == 0  # display-main is not search authorization
    encoded = json.dumps(pin.snapshot)
    for private in ("fixture-secret", "fixture_login", "fixture.invalid", "fixture_business"):
        assert private not in encoded
    sql, args = calls[0]
    assert "username" not in sql and "password" not in sql and args == (81, 205)
    assert "a.semantic_model_id=m.id" in sql and "ds.semantic_model_id=m.id" in sql
    assert "f.data_source_id=ds.id" in sql and "f.table_id=t.id" in sql
    assert "AND b.id=%s" in sql and "ds.status=1" in sql and "e.status=1" in sql
    pin.finish()


def test_model_wide_metadata_query_remains_bound_to_its_current_model(monkeypatch):
    calls = []
    monkeypatch.setattr(mysql, "_query", lambda sql, args: calls.append((sql, args)) or [])
    result = values.capture_value_sources(catalog_scope(81, []))
    assert result["scope"] == catalog_scope(81, [])
    assert calls[0][1] == (81,) and "WHERE m.id=%s" in calls[0][0]
    assert "AND b.id=%s" not in calls[0][0]


def test_capture_uses_one_metadata_transaction_and_does_not_connect_to_business(monkeypatch):
    from test_catalog_release import Connection as MetadataConnection
    connection = MetadataConnection()
    monkeypatch.setattr(mysql, "_get_connection", lambda: connection)
    def query(sql, args=None):
        assert mysql._catalog_snapshot_connection.get() is connection
        if "information_schema" in sql:
            assert "semantic_model_data_source" in args
            return [{"table_name": t, "engine": "InnoDB"} for t in CATALOG_TABLES]
        if "@@server_uuid" in sql: return [{"server_uuid": "fixture", "database_name": "fixture"}]
        assert "ds.password" not in sql
        return [source()]
    monkeypatch.setattr(mysql, "_query", query)
    snapshot = authority()
    monkeypatch.setattr(mysql, "get_dsl_by_scope", lambda *args: deepcopy(snapshot["documents"][0]))
    monkeypatch.setattr(mysql, "get_table_field_by_scope", lambda *args, **kwargs: deepcopy(snapshot["physical_catalog"]))
    monkeypatch.setattr(mysql.pymysql, "connect", lambda **kwargs: pytest.fail("business connection during capture"))
    captured = capture_catalog(81, [205])
    assert len(captured["physical_catalog"]["entity_value_sources"]["fields"]) == 1
    assert connection.closed == connection.rolled_back == 1


@pytest.mark.parametrize("key,new", [("host", "another.invalid"), ("port", 3307),
    ("db_name", "another_fixture"), ("db_type", "MariaDB"), ("data_source_id", 9),
    ("vectorization", 1), ("mapping_column", "other_city"), ("field_id", 3)])
def test_route_mapping_or_policy_change_invalidates_publication(catalog, key, new):
    service, current, _, overrides, *_ = catalog
    pin = service.pin(81, [205])
    before = pin.snapshot["catalog_version"]
    current[0][key] = new
    fresh = deepcopy(overrides[(81, (205,))])
    fresh["physical_catalog"]["entity_value_sources"] = values.capture_value_sources(SCOPE)
    overrides[(81, (205,))] = reseal(fresh)
    assert fresh["catalog_version"] != before
    with pytest.raises(CatalogEvidenceError, match="CATALOG_AUTHORITY_DRIFT"):
        pin.finish()
    with pytest.raises(CatalogEvidenceError, match="CATALOG_AUTHORITY_DRIFT"):
        service.pin(81, [205])


def test_password_rotation_does_not_change_routing_identity(catalog):
    _, current, *_ = catalog
    before = values.capture_value_sources(SCOPE)
    current[0]["password"] = "fixture-rotated"
    assert values.capture_value_sources(SCOPE) == before


@pytest.mark.parametrize("field,value", [("semantic_model_id", 82), ("business_domain_id", 206),
    ("attribute_id", True), ("data_source_id", "7"), ("port", 65536)])
def test_capture_revalidates_metadata_even_after_scoped_query(monkeypatch, field, value):
    monkeypatch.setattr(mysql, "_query", lambda *args: [source(**{field: value})])
    with pytest.raises(CatalogEvidenceError): values.capture_value_sources(SCOPE)


def test_multiple_physical_registrations_are_not_first_wins(monkeypatch):
    monkeypatch.setattr(mysql, "_query", lambda *args: [source(), source(field_id=3)])
    with pytest.raises(CatalogEvidenceError, match="MAPPING_AMBIGUOUS"):
        values.capture_value_sources(SCOPE)


def test_exact_lookup_is_verified_twice_without_an_embedding_or_vector_hit(catalog, business):
    service, _, calls, *_ = catalog
    rows, opened = business
    rows[:] = ["示例医院".encode("utf-8")]
    pin = service.pin(81, [205])
    receipt = lookup(pin, "示例医院", data_source_id=7)
    assert receipt["values"] == ["示例医院"] and receipt["complete"] is True
    assert receipt["source"] == "VERIFIED_SOURCE_EXACT_LOOKUP"
    assert receipt["catalog_pin"]["catalog_version"] == pin.snapshot["catalog_version"]
    assert "示例医院" not in json.dumps(pin.snapshot, ensure_ascii=False)
    pin.finish()
    assert len(opened) == 2
    for conn, options in opened:
        assert options["host"] == "fixture.invalid" and options["autocommit"] is False
        assert conn.closed == conn.rolled_back == 1
        assert conn.sql[0] == ("START TRANSACTION READ ONLY", None)
        statement, params = conn.sql[1]
        assert "FROM `hospitals`" in statement and "`city`" in statement
        assert params == ("示例医院", 9) and "示例医院" not in statement
        assert "CAST(%s AS BINARY)" in statement
    assert all(args == (81, 205, "1205") for sql, args in calls[1:] if "ds.password" in sql)


@pytest.mark.parametrize("source_id", [9, "7", True, 0])
def test_source_mismatch_rejected_before_any_business_read(catalog, business, source_id):
    service, *_ = catalog
    with pytest.raises(CatalogEvidenceError):
        lookup(service.pin(81, [205]), "甲城", data_source_id=source_id)
    assert not business[1]


@pytest.mark.parametrize("record", ["sm81_bd206:attr:hospital.city", "sm82_bd205:attr:hospital.city",
    "entity-attr-value:legacy-hit", "sm81_bd205:entity:hospital"])
def test_foreign_or_legacy_value_row_cannot_be_a_field_selector(catalog, business, record):
    service, *_ = catalog
    with pytest.raises(CatalogEvidenceError, match="ATTRIBUTE_NOT_PINNED"):
        service.pin(81, [205]).lookup_entity_values(record, "甲城")
    assert not business[1]


def test_old_catalog_can_still_pin_but_cannot_authorize_dynamic_values(business):
    service, *_ = system()
    publish(service)
    pin = service.pin(81, [205])
    with pytest.raises(CatalogEvidenceError, match="SOURCES_NOT_CAPTURED"):
        lookup(pin, "甲城")
    pin.finish()
    assert not business[1]


@pytest.mark.parametrize("change", ["route", "field", "model", "domain", "owner", "deleted", "duplicated"])
def test_live_metadata_mismatch_cannot_select_another_source(catalog, business, change):
    service, current, *_ = catalog
    pin = service.pin(81, [205])
    if change == "deleted": current.clear()
    elif change == "duplicated": current.append(source())
    else:
        key, value = {"route": ("host", "another.invalid"), "field": ("mapping_column", "other"),
            "model": ("semantic_model_id", 82), "domain": ("business_domain_id", 206),
            "owner": ("entity_id", 206)}[change]
        current[0][key] = value
    with pytest.raises(CatalogEvidenceError): lookup(pin, "甲城")
    assert not business[1]


def test_deleted_business_value_invalidates_acceptance_and_new_lookup_observes_absence(catalog, business):
    service, *_ = catalog
    rows, opened = business
    pin = service.pin(81, [205])
    receipt = lookup(pin, "甲城")
    rows.clear()
    with pytest.raises(CatalogEvidenceError, match="ENTITY_VALUES_CHANGED_DURING_READ"):
        pin.finish()
    fresh = service.pin(81, [205])
    renewed = lookup(fresh, "甲城")
    assert renewed["values"] == [] and renewed["complete"] is True
    assert renewed["observation_hash"] != receipt["observation_hash"]
    fresh.finish()


def test_multiple_normalization_matches_remain_distinct_and_truncation_is_explicit(catalog, business):
    service, *_ = catalog
    business[0][:] = ["TDC-3", "TDC－3"]
    pin = service.pin(81, [205])
    receipt = lookup(pin, "TDC-3", limit=1)
    assert receipt["complete"] is False and len(receipt["values"]) == 1
    pin.finish()
    fresh = service.pin(81, [205])
    receipt = lookup(fresh, "TDC-3", limit=2)
    assert receipt["complete"] is True and len(receipt["values"]) == 2
    fresh.finish()


@pytest.mark.parametrize("rows", [["乙城"], [None], [123], ["甲城", "甲城"], ["甲城"] * 10])
def test_wrong_malformed_or_unbounded_source_response_is_rejected(catalog, business, rows):
    service, *_ = catalog
    business[0][:] = rows
    with pytest.raises(CatalogEvidenceError, match="SOURCE_RESULT_INVALID"):
        lookup(service.pin(81, [205]), "甲城")


@pytest.mark.parametrize("query,limit", [("", 8), ("x" * 257, 8), (123, 8), ("甲城", True), ("甲城", 33)])
def test_invalid_query_does_not_read_a_business_source(catalog, business, query, limit):
    service, *_ = catalog
    with pytest.raises(CatalogEvidenceError, match="QUERY_INVALID"):
        lookup(service.pin(81, [205]), query, limit=limit)
    assert not business[1]


def test_query_failure_rolls_back_closes_and_redacts_source_error(catalog, monkeypatch):
    service, *_ = catalog
    conn = Connection([])
    conn.error = "fixture-secret fixture.invalid internal failure"
    monkeypatch.setattr(mysql.pymysql, "connect", lambda **kwargs: conn)
    with pytest.raises(CatalogEvidenceError) as caught:
        lookup(service.pin(81, [205]), "甲城")
    assert str(caught.value) == "CATALOG_VALUE_LOOKUP_FAILED"
    assert conn.closed == conn.rolled_back == 1


def test_scope_change_requires_a_new_field_and_new_observation(catalog, business):
    service, *_ = catalog
    receipt = lookup(service.pin(81, [205]), "甲城")
    publish(service, publication_id="model-wide", domains=())
    pin = service.pin(81, [])
    with pytest.raises(CatalogEvidenceError, match="ATTRIBUTE_NOT_PINNED"):
        pin.lookup_entity_values(receipt["attribute_record_id"], "甲城")


def test_finished_pin_cannot_reuse_or_query_values(catalog, business):
    service, *_ = catalog
    pin = service.pin(81, [205])
    lookup(pin, "甲城")
    pin.finish()
    with pytest.raises(CatalogEvidenceError, match="ALREADY_FINISHED"):
        pin.lookup_entity_values(ATTRIBUTE, "甲城")


def test_real_catalog_varchar_uuid_ids_and_port_are_preserved(monkeypatch, business):
    entity = "10000000-0000-0000-0000-000000000205"
    attribute = "20000000-0000-0000-0000-000000001205"
    row = source(entity_id=entity, attribute_id=attribute, port="3306")
    monkeypatch.setattr(mysql, "_query", lambda *args: [deepcopy(row)])
    snapshot = authority()
    owner = snapshot["documents"][0]["entities"][0]
    owner["entity_id"] = entity
    owner["attributes"][0]["attribute_id"] = attribute
    snapshot["physical_catalog"]["entity_value_sources"] = values.capture_value_sources(SCOPE)
    service, _, _, _, _, overrides = system()
    overrides[(81, (205,))] = reseal(snapshot)
    publish(service)
    pin = service.pin(81, [205])
    result = lookup(pin, "甲城")
    assert result["field"]["entity_id"] == entity and result["field"]["attribute_id"] == attribute
    assert business[1][0][1]["port"] == 3306
    pin.finish()


@pytest.mark.parametrize("port", [True, "0", "65536", "3306x", " 3306", "3.306e3"])
def test_bad_metadata_port_is_not_silently_coerced(port):
    with pytest.raises(CatalogEvidenceError, match="SOURCE_ROUTE_INVALID"):
        values.route_identity(source(port=port))


def test_catalog_attribute_and_source_mapping_must_agree(catalog, business):
    service, _, _, overrides, *_ = catalog
    current = deepcopy(overrides[(81, (205,))])
    current["documents"][0]["entities"][0]["attributes"][0]["field_mapping"] = "hospitals.other"
    overrides[(81, (205,))] = reseal(current)
    publish(service, publication_id="remapped-fixture")
    with pytest.raises(CatalogEvidenceError, match="FIELD_UNVERIFIED"):
        lookup(service.pin(81, [205]), "甲城")
    assert not business[1]


def test_owner_code_match_cannot_hide_a_changed_owner_id(catalog, business):
    service, _, _, overrides, *_ = catalog
    current = deepcopy(overrides[(81, (205,))])
    current["documents"][0]["entities"][0]["entity_id"] = 999
    overrides[(81, (205,))] = reseal(current)
    publish(service, publication_id="new-owner-fixture")
    with pytest.raises(CatalogEvidenceError, match="OWNER_UNVERIFIED"):
        lookup(service.pin(81, [205]), "甲城")
    assert not business[1]


def test_caller_mutation_of_receipt_does_not_change_acceptance_evidence(catalog, business):
    service, *_ = catalog
    pin = service.pin(81, [205])
    result = lookup(pin, "甲城")
    result["values"].clear()
    result["field"]["route"]["locator_hash"] = "forged"
    business[0].clear()
    with pytest.raises(CatalogEvidenceError, match="VALUES_CHANGED_DURING_READ"):
        pin.finish()


def test_activation_change_during_source_read_invalidates_observation(catalog, monkeypatch):
    service, *_ = catalog
    pin = service.pin(81, [205])
    def read(*args):
        publish(service, publication_id="reactivated-fixture")
        return ["甲城"]
    monkeypatch.setattr(values, "query_values", read)
    with pytest.raises(CatalogEvidenceError, match="PUBLICATION_CHANGED_DURING_READ"):
        lookup(pin, "甲城")


def test_unknown_driver_fails_before_connecting(catalog, business):
    service, rows, _, overrides, *_ = catalog
    rows[0]["db_type"] = "unknown"
    snapshot = deepcopy(overrides[(81, (205,))])
    snapshot["physical_catalog"]["entity_value_sources"] = values.capture_value_sources(SCOPE)
    overrides[(81, (205,))] = reseal(snapshot)
    publish(service, publication_id="unknown-driver")
    with pytest.raises(CatalogEvidenceError, match="DRIVER_UNSUPPORTED"):
        lookup(service.pin(81, [205]), "甲城")
    assert not business[1]


def test_lookup_budget_is_bounded_per_pin(catalog, business):
    service, *_ = catalog
    pin = service.pin(81, [205])
    for _ in range(32): lookup(pin, "甲城")
    before = len(business[1])
    with pytest.raises(CatalogEvidenceError, match="QUERY_BUDGET_EXCEEDED"):
        lookup(pin, "甲城")
    assert len(business[1]) == before


def test_query_metacharacters_are_literal_parameters(catalog, business):
    service, *_ = catalog
    text = "x%'_\\; --"
    business[0][:] = [text]
    receipt = lookup(service.pin(81, [205]), text)
    assert receipt["values"] == [text]
    statement, params = business[1][0][0].sql[1]
    assert text not in statement and params == (text, 9)
    assert "LIKE" not in statement


def test_source_mapping_coverage_is_distinct_from_dynamic_value_or_server_proof(catalog):
    service, *_ = catalog
    pin = service.pin(81, [205])
    _, coverage = build_catalog_records(pin.snapshot, lambda texts: [[0.0, 1.0] for _ in texts])
    assert coverage["captured_dependencies"] == ["entity_value_source_mappings", "data_source_route_configuration"]
    assert "external_entity_values" in coverage["excluded_dependencies"]
    assert "data_source_runtime_identity" in coverage["excluded_dependencies"]
    assert coverage["capability_coverage_verified"] is False


def test_display_main_does_not_enable_implicit_source_lookup(catalog, business):
    service, *_ = catalog
    pin = service.pin(81, [205])
    assert pin.entity_value_lookup_fields() == []
    with pytest.raises(CatalogEvidenceError, match='IMPLICIT_SEARCH_NOT_GOVERNED'):
        lookup(pin, '甲城', require_implicit_policy=True)
    assert not business[1]
    assert lookup(pin, '甲城')['values'] == ['甲城']


@pytest.mark.parametrize('flag',[1,True,'1'])
def test_governed_enabled_source_field_can_be_used_for_implicit_lookup(catalog,business,flag):
    service, rows, _, overrides, *_ = catalog
    rows[0]['vectorization']=flag
    snapshot=deepcopy(overrides[(81,(205,))])
    snapshot['physical_catalog']['entity_value_sources']=values.capture_value_sources(SCOPE)
    overrides[(81,(205,))]=reseal(snapshot)
    publish(service,publication_id='enabled-lookup')
    pin=service.pin(81,[205])
    assert pin.entity_value_lookup_fields()==[attribute_id(pin)]
    assert lookup(pin,'甲城',require_implicit_policy=True)['values']==['甲城']
    pin.finish()


@pytest.mark.parametrize('flag',[2,'not-a-policy',[],{}])
def test_unknown_search_policy_cannot_become_enabled_by_truthiness(catalog,flag):
    service, rows, _, overrides, *_ = catalog
    rows[0]['vectorization']=flag
    snapshot=deepcopy(overrides[(81,(205,))])
    snapshot['physical_catalog']['entity_value_sources']=values.capture_value_sources(SCOPE)
    overrides[(81,(205,))]=reseal(snapshot)
    publish(service,publication_id='invalid-lookup-policy')
    with pytest.raises(CatalogEvidenceError,match='SEARCH_POLICY_INVALID'):
        service.pin(81,[205]).entity_value_lookup_fields()
