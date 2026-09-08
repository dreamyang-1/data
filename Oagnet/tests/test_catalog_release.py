"""Cutover evidence must detect drift and never manufacture live publication."""
import copy

import pytest

from catalog_release import (
    CATALOG_TABLES, CatalogEvidenceError, capture_catalog, catalog_scope,
    digest, prepare_release, snapshot_from_documents, verify_release,
)


def snapshot(domains=(205,), *, formula="SUM(amount)", source="fixture-db"):
    scope = catalog_scope(81, domains)
    docs = [{"semantic_model": {"id": 81}, "business_domain": {"id": domain},
             "entities": [{"entity_id": 1, "entity_code": "sales"}],
             "metrics": [{"metric_code": "amount", "formula": formula}], "dimensions": []}
            for domain in (domains or [205, 206])]
    return snapshot_from_documents(scope, {"server_uuid": source, "database_name": "fixture"}, docs,
                                   {"scope": {"semantic_model_id": 81}, "tables": []})


def record(domain=205, *, record_id="record-1"):
    return {"id": record_id, "text": "fixture metric", "metadata": {
        "semantic_model_id": 81, "business_domain_id": domain, "type": "metric", "formula": "SUM(amount)"}}


def release(current=None, records=None):
    current = snapshot() if current is None else current
    manifest, rows = prepare_release(current, records or [record()], publication_id="test-release",
                                     producer_revision="fixture-revision", embedding_contract="fixture-embedding-v1")
    marker = {"state": "PUBLISHED", "vector_index_version": manifest["vector_index_version"]}
    return current, manifest, rows, marker


def test_supplied_matching_evidence_is_not_claimed_as_live_runtime_proof():
    current, manifest, rows, marker = release()
    result = verify_release(current, manifest, rows, published_marker=marker)
    assert result["records_verified"] == 1 and result["status"] == "VERIFIED_SUPPLIED_EVIDENCE"
    assert result["live_runtime_verified"] is False
    assert all(value not in str(result) for value in ("SUM(amount)", "fixture-db", "fixture metric"))


@pytest.mark.parametrize("value", [None, True, 0, -1, "81", 81.0])
def test_model_identity_is_required_strict_positive(value):
    with pytest.raises(CatalogEvidenceError, match="REQUEST_SCOPE_INVALID"):
        catalog_scope(value)


@pytest.mark.parametrize("domains", [[True], [0], [-1], ["205"], None])
def test_invalid_explicit_scope_is_not_model_wide(domains):
    with pytest.raises(CatalogEvidenceError, match="REQUEST_SCOPE_INVALID"):
        catalog_scope(81, domains)


def test_multiple_domains_fail_closed_and_single_alias_duplicates_normalize():
    assert catalog_scope(81, [205, 205])["business_domain_ids"] == [205]
    assert catalog_scope(81)["scope_mode"] == "MODEL_WIDE"
    with pytest.raises(CatalogEvidenceError, match="EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED"):
        catalog_scope(81, [205, 206])


@pytest.mark.parametrize("change", ["formula", "source", "physical", "scope"])
def test_authority_or_scope_drift_invalidates_an_existing_release(change):
    current, manifest, rows, marker = release()
    if change == "formula": current = snapshot(formula="SUM(quantity)")
    elif change == "source": current = snapshot(source="different-db")
    elif change == "scope": current = snapshot(domains=())
    else:
        current["physical_catalog"]["tables"] = [{"table_id": 1, "fields": [{"field_id": 2, "data_type": "decimal"}]}]
        current["catalog_version"] = digest({k: v for k, v in current.items() if k != "catalog_version"})
    with pytest.raises(CatalogEvidenceError, match="CATALOG_(AUTHORITY_DRIFT|SCOPE_MISMATCH)"):
        verify_release(current, manifest, rows, published_marker=marker)


@pytest.mark.parametrize("change", ["formula", "text", "model", "domain", "version", "release", "hash", "legacy"])
def test_index_metadata_and_stamps_cannot_silently_drift(change):
    current, manifest, rows, marker = release()
    if change == "text": rows[0]["text"] = "different text"
    elif change == "legacy": rows[0]["metadata"] = record()["metadata"]
    else:
        key = {"formula": "formula", "model": "semantic_model_id", "domain": "business_domain_id",
               "version": "catalog_version", "release": "catalog_publish_id", "hash": "catalog_record_hash"}[change]
        rows[0]["metadata"][key] = "changed"
    with pytest.raises(CatalogEvidenceError, match="CATALOG_INDEX_RECORD_DRIFT"):
        verify_release(current, manifest, rows, published_marker=marker)


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate"])
def test_complete_inventory_is_required_instead_of_top_k_or_row_count(case):
    current, manifest, rows, marker = release(records=[record(), record(record_id="record-2")])
    if case == "missing": rows.pop()
    elif case == "duplicate": rows.append(copy.deepcopy(rows[0]))
    else:
        rows.append(copy.deepcopy(rows[0])); rows[-1]["id"] = "unpublished"
    with pytest.raises(CatalogEvidenceError, match="CATALOG_(INDEX_INVENTORY_DRIFT|DUPLICATE_RECORD_ID|INDEX_RECORD_DRIFT)"):
        verify_release(current, manifest, rows, published_marker=marker)


@pytest.mark.parametrize("marker", [None, {}, {"state": "PREPARED"}, {"state": "PUBLISHED", "vector_index_version": "old"}])
def test_missing_or_staging_publication_is_never_accepted(marker):
    current, manifest, rows, _ = release()
    with pytest.raises(CatalogEvidenceError, match="CATALOG_PUBLICATION_NOT_PROVEN"):
        verify_release(current, manifest, rows, published_marker=marker)


@pytest.mark.parametrize("domain", [-1, 206, True, None])
def test_explicit_release_rejects_shared_foreign_and_invalid_records(domain):
    with pytest.raises(CatalogEvidenceError, match="CATALOG_DOMAIN_MISMATCH"):
        release(records=[record(domain)])


def test_model_wide_manifest_remains_bound_to_the_models_captured_domains():
    current, manifest, rows, marker = release(snapshot(()), [record(-1), record(206, record_id="domain206")])
    assert verify_release(current, manifest, rows, published_marker=marker)["records_verified"] == 2
    with pytest.raises(CatalogEvidenceError, match="CATALOG_DOMAIN_MISMATCH"):
        release(snapshot(()), [record(999)])


def test_snapshot_input_order_and_mutation_are_controlled():
    initial = snapshot(())
    docs = copy.deepcopy(initial["documents"])
    again = snapshot_from_documents(initial["scope"], {"database_name": "fixture", "server_uuid": "fixture-db"},
                                    list(reversed(docs)), initial["physical_catalog"])
    assert again["catalog_version"] == initial["catalog_version"]
    docs[0]["metrics"].clear()
    assert initial["documents"][0]["metrics"]


class Cursor:
    def __init__(self, connection): self.connection = connection
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args=None):
        self.connection.queries.append((sql, args))
        if self.connection.fail and "START TRANSACTION" in sql: raise RuntimeError("fixture transaction failure")
        self.sql = sql
    def fetchall(self): return []


class Connection:
    def __init__(self, fail=False): self.queries=[]; self.closed=0; self.rolled_back=0; self.fail=fail
    def cursor(self, *args): return Cursor(self)
    def close(self): self.closed += 1
    def rollback(self): self.rolled_back += 1


def test_existing_catalog_loaders_share_one_read_only_transaction(monkeypatch):
    import mysql_tool as mysql
    opened=[]
    def connect():
        conn=Connection(); opened.append(conn); return conn
    monkeypatch.setattr(mysql, "_get_connection", connect)
    with mysql.consistent_catalog_read():
        mysql._query("SELECT fixture")
        mysql.get_entity(205)
        mysql.get_table_field_by_scope(81, business_domain_id=205)
        assert len(opened)==1 and opened[0].closed==0
        with pytest.raises(ValueError, match="CATALOG_SNAPSHOT_NESTING_NOT_SUPPORTED"):
            with mysql.consistent_catalog_read(): pass
    conn=opened[0]
    assert conn.closed==1 and conn.rolled_back==1
    assert [q[0] for q in conn.queries[:3]] == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "SET TRANSACTION READ ONLY", "START TRANSACTION WITH CONSISTENT SNAPSHOT"]
    assert all(q[0].lstrip().upper().startswith(("SELECT", "SET TRANSACTION", "START TRANSACTION")) for q in conn.queries)
    mysql._query("SELECT later")
    assert len(opened)==2 and opened[1].closed==1


@pytest.mark.parametrize("stage", ["begin", "body"])
def test_capture_failure_releases_transaction_and_context(monkeypatch, stage):
    import mysql_tool as mysql
    conn=Connection(fail=stage=="begin")
    monkeypatch.setattr(mysql, "_get_connection", lambda: conn)
    with pytest.raises(RuntimeError):
        with mysql.consistent_catalog_read(): raise RuntimeError("fixture body failure")
    assert conn.closed==1 and conn.rolled_back==1
    assert mysql._catalog_snapshot_connection.get() is None


@pytest.mark.parametrize("domains", [[], [205]])
def test_capture_covers_model_wide_domains_or_the_exact_explicit_domain(monkeypatch, domains):
    import mysql_tool as mysql
    conn=Connection(); calls=[]
    monkeypatch.setattr(mysql, "_get_connection", lambda: conn)
    def query(sql, args=None):
        assert mysql._catalog_snapshot_connection.get() is conn
        if "information_schema" in sql: return [{"table_name": name, "engine": "InnoDB"} for name in CATALOG_TABLES]
        return [{"server_uuid": "fixture-db", "database_name": "fixture"}]
    monkeypatch.setattr(mysql, "_query", query)
    monkeypatch.setattr(mysql, "get_business_domains", lambda model: [{"id": 205}, {"id": 206}])
    def dsl(model, domain):
        calls.append((model,domain)); return {"semantic_model": {"id": model}, "business_domain": {"id": domain}, "entities": [], "metrics": [], "dimensions": []}
    monkeypatch.setattr(mysql, "get_dsl_by_scope", dsl)
    monkeypatch.setattr(mysql, "get_table_field_by_scope", lambda model, **kw: {"scope": {"semantic_model_id": model}, "tables": []})
    result=capture_catalog(81, domains)
    assert calls==([(81,205)] if domains else [(81,205),(81,206)])
    assert result["scope"]==catalog_scope(81, domains)
    assert conn.closed==1 and conn.rolled_back==1


@pytest.mark.parametrize("engines", [[], [{"table_name": name, "engine": "MyISAM"} for name in CATALOG_TABLES]])
def test_missing_or_nontransactional_catalog_cannot_claim_snapshot_consistency(monkeypatch, engines):
    import mysql_tool as mysql
    conn=Connection(); monkeypatch.setattr(mysql, "_get_connection", lambda: conn)
    monkeypatch.setattr(mysql, "_query", lambda *args: engines)
    with pytest.raises(CatalogEvidenceError, match="CATALOG_CONSISTENT_SNAPSHOT_UNSUPPORTED"):
        capture_catalog(81, [205])
    assert conn.closed==1 and conn.rolled_back==1


@pytest.mark.parametrize("fault", [None, "marker_changed", "incomplete", "legacy"])
def test_operator_receipt_is_bounded_and_fails_closed(tmp_path, capsys, fault):
    import json
    from scripts.check_catalog_release import main
    current, manifest, rows, marker=release()
    bundle={"current_snapshot": current, "manifest": manifest, "index_records": rows,
            "marker_before": marker, "marker_after": marker, "inventory_complete": True}
    if fault=="marker_changed": bundle["marker_after"]={"state": "PUBLISHED", "vector_index_version": "new"}
    elif fault=="incomplete": bundle["inventory_complete"]=False
    elif fault=="legacy": bundle["index_records"]=[record()]
    private=tmp_path/'private.json'; public=tmp_path/'public.json'
    private.write_text(json.dumps(bundle),encoding='utf-8')
    assert main(['--private-bundle',str(private),'--report',str(public)])==(0 if fault is None else 2)
    output=public.read_text(encoding='utf-8')+capsys.readouterr().out
    assert 'SUM(amount)' not in output and 'fixture metric' not in output and 'fixture-db' not in output
    assert json.loads(public.read_text(encoding='utf-8'))['live_runtime_verified'] is False


def test_operator_tool_does_not_overwrite_its_private_input(tmp_path):
    from scripts.check_catalog_release import main
    path=tmp_path/'private.json'; path.write_text('{}',encoding='utf-8')
    with pytest.raises(SystemExit):
        main(['--private-bundle',str(path),'--report',str(path)])
    assert path.read_text(encoding='utf-8')=='{}'
