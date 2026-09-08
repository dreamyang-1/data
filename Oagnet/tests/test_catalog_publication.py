"""Publication, drift, scope and rollback tests through existing record builders."""
from copy import deepcopy
import json

import pytest
from redis.exceptions import ConnectionError, WatchError

from catalog_generation import build_catalog_records, prepare_catalog_generation
from catalog_publication import CatalogPublication, generation_filter
from catalog_registry import RedisCatalogReleaseRegistry
from catalog_release import CatalogEvidenceError, catalog_scope, digest, snapshot_from_documents
from catalog_store import MilvusCatalogStore
from vector_store import SearchResult


def authority(domains=(205,), model=81, *, formula="SUM(amount)"):
    selected = list(domains or [205, 206])
    dimension = {"dim_code": "city", "dim_name": "城市", "bind_entities": [
        {"entity": str(d), "attr": str(d + 1000), "businessDomain": str(d)} for d in [205, 206]],
        "enum_list": [{"code": "city-a", "name": "甲城"}]}
    documents = [{"semantic_model": {"id": model, "name": "fixture model"},
        "business_domain": {"id": d, "name": f"fixture domain {d}"},
        "entities": [{"entity_id": d, "entity_code": "hospital", "entity_name": "医院", "business_domain": d,
            "attributes": [{"attribute_id": d + 1000, "attr_code": "city", "attr_name": "城市", "field_mapping": "hospitals.city"}],
            "relations": [{"relation_code": "orders", "relation_name": "订单关系"}]}],
        "metrics": [{"metric_code": "amount", "metric_name": "销售额", "business_domain": d, "formula": formula}],
        "dimensions": [deepcopy(dimension)]} for d in selected]
    physical = {"scope": {"semantic_model_id": model, "data_source_id": None}, "tables": [
        {"table_id": 1, "table_name": "hospitals", "data_source_id": 7, "semantic_model_id": model,
         "fields": [{"field_id": 2, "field_name": "city", "table_id": 1}]}]}
    return snapshot_from_documents(catalog_scope(model, list(domains)), {"server_uuid": "fixture", "database_name": "fixture"}, documents, physical)


def reseal(snapshot):
    snapshot["catalog_version"] = digest({k:v for k,v in snapshot.items() if k != "catalog_version"})
    return snapshot


def embed(texts):
    return [[0.1, 0.2] for _ in texts]


class FakeRedis:
    """Transaction double: commit checks all watched key revisions before writes."""
    def __init__(self):
        self.values = {}; self.revisions = {}; self.before_execute = None; self.after_execute = None; self.fail = False

    def get(self, key):
        if self.fail: raise ConnectionError("fixture offline")
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        self.revisions[key] = self.revisions.get(key, 0) + 1

    def pipeline(self):
        client = self
        class Pipeline:
            def __enter__(self): self.watched={}; self.commands=[]; return self
            def __exit__(self, *args): pass
            def watch(self, *keys): self.watched={k:client.revisions.get(k,0) for k in keys}
            def get(self, key): return client.get(key)
            def multi(self): pass
            def set(self, key, value): self.commands.append((key,value))
            def execute(self):
                if client.before_execute:
                    callback=client.before_execute;client.before_execute=None;callback()
                if any(client.revisions.get(k,0)!=v for k,v in self.watched.items()):raise WatchError()
                for k,v in self.commands:client.set(k,v)
                if client.after_execute:
                    callback=client.after_execute;client.after_execute=None;callback()
        return Pipeline()


def matches(metadata, where):
    if "$and" in where: return all(matches(metadata,c) for c in where["$and"])
    if "$or" in where: return any(matches(metadata,c) for c in where["$or"])
    return all(metadata.get(k) in v["$in"] if isinstance(v,dict) else metadata.get(k)==v for k,v in where.items())


class MemoryStore:
    catalog_target_identity = {"backend": "offline fixture", "collections": ["catalog semantic", "catalog physical"]}
    embedding_dim = 2
    def __init__(self):self.records={};self.fail_add=False;self.after_add=None;self.after_inventory=None;self.reads=[]
    def add(self, records):
        for i,r in enumerate(records):
            self.records[r.id]=deepcopy(r)
            if self.fail_add and i==0:raise RuntimeError("fixture partial upsert failure")
        if self.after_add:self.after_add()
    def get_catalog_inventory(self, where):
        rows=[deepcopy(r) for r in self.records.values() if matches(r.metadata,where)]
        if self.after_inventory:
            callback=self.after_inventory;self.after_inventory=None;callback()
        return rows
    def get_by_where(self, where):
        self.reads.append(deepcopy(where))
        return [SearchResult(r.id,1.0,r.text,deepcopy(r.metadata)) for r in self.records.values() if matches(r.metadata,where)]
    find_exact=get_by_where
    def search(self, query_vector, top_k=8, where=None):return self.get_by_where(where)[:top_k]


def system():
    redis=FakeRedis();store=MemoryStore();registry=RedisCatalogReleaseRegistry(store.catalog_target_identity,redis)
    captures=[];overrides={}
    def capture(model, domains):
        captures.append((model,list(domains)))
        return deepcopy(overrides.get((model,tuple(domains)),authority(domains,model)))
    publication=CatalogPublication(store,registry,capture)
    return publication,store,registry,redis,captures,overrides


def publish(service, publication_id="release-1", model=81, domains=(205,)):
    return service.publish(model,list(domains),embed_fn=embed,publication_id=publication_id,
                           producer_revision="fixture-revision",embedding_contract="fixture-float32-2")


def test_generation_covers_existing_semantic_and_physical_builders():
    records,coverage=build_catalog_records(authority(),embed)
    assert {r.id for r in records}=={
        "sm81_bd205:entity:hospital","sm81_bd205:attr:hospital.city","sm81_bd205:relation:hospital.orders",
        "sm81_bd205:metric:amount","sm81_bd205:dim:city","sm81_bd205:enum:city.city-a",
        "ds7:table:hospitals","ds7:field:hospitals.city"}
    assert {r.metadata["business_domain_id"] for r in records}=={205}
    assert coverage["explicit_exclusions"]=={"MODEL_WIDE_SHARED_ORIGINAL":2}
    assert coverage["capability_coverage_verified"] is False


def test_model_wide_generation_deduplicates_only_identical_shared_definitions():
    records,coverage=build_catalog_records(authority(()),embed)
    assert len(records)==16
    assert {r.metadata["business_domain_id"] for r in records}=={-1,205,206}
    assert sum(r.metadata["type"]=="dimension" for r in records)==1
    assert sum(r.metadata["type"]=="scoped_dimension" for r in records)==2
    assert coverage["source_counts"]["domain_documents"]==2


@pytest.mark.parametrize("fault",["entity_code","attribute_code","relation_code","metric_code","dimension_code","enum_code","table_name","field_name","entity_id","attribute_id","table_id","field_id","field_owner","metric_owner","entity_owner","duplicate"])
def test_source_identity_and_ownership_fail_before_publication(fault):
    current=authority();doc=current["documents"][0];entity=doc["entities"][0];table=current["physical_catalog"]["tables"][0]
    target,key={
        "entity_code":(entity,"entity_code"),"attribute_code":(entity["attributes"][0],"attr_code"),
        "relation_code":(entity["relations"][0],"relation_code"),"metric_code":(doc["metrics"][0],"metric_code"),
        "dimension_code":(doc["dimensions"][0],"dim_code"),"enum_code":(doc["dimensions"][0]["enum_list"][0],"code"),
        "table_name":(table,"table_name"),"field_name":(table["fields"][0],"field_name"),
        "entity_id":(entity,"entity_id"),"attribute_id":(entity["attributes"][0],"attribute_id"),
        "table_id":(table,"table_id"),"field_id":(table["fields"][0],"field_id"),
        "field_owner":(table["fields"][0],"table_id"),"metric_owner":(doc["metrics"][0],"business_domain"),
        "entity_owner":(entity,"business_domain"),"duplicate":(entity,"entity_code"),
    }[fault]
    if fault=="duplicate":doc["entities"].append(deepcopy(entity))
    else:target[key]=None
    with pytest.raises(CatalogEvidenceError):build_catalog_records(reseal(current),embed)


def test_conflicting_shared_source_is_not_first_wins():
    current=authority(())
    current["documents"][1]["dimensions"][0]["dim_name"]="conflicting label"
    with pytest.raises(CatalogEvidenceError,match="CATALOG_SHARED_DEFINITION_CONFLICT"):
        build_catalog_records(reseal(current),embed)


def test_unowned_or_opaque_dimension_is_explicitly_excluded_not_published_as_owned():
    current=authority();current["documents"][0]["dimensions"][0]["special_rules"]={"unverified":"rule"}
    records,coverage=build_catalog_records(reseal(current),embed)
    assert not any(r.metadata["type"]=="scoped_dimension" for r in records)
    assert coverage["explicit_exclusions"]["NO_OWNERSHIP_PROVEN_DIMENSION_PROJECTION"]==1
    assert coverage["capability_coverage_verified"] is False


@pytest.mark.parametrize("vector",[[],[True,1],[float('nan'),0],[float('inf'),0],[1e99,0],["x",0]])
def test_invalid_embedding_cannot_be_published(vector):
    service,store,_,_,_,_=system()
    with pytest.raises(CatalogEvidenceError):
        service.publish(81,[205],embed_fn=lambda texts:[vector for _ in texts],publication_id="bad-vector",
                        producer_revision="fixture",embedding_contract="fixture")
    assert not store.records


def test_publication_pins_complete_index_and_persists_a_marker_with_no_ttl():
    service,store,registry,client,captures,_=system()
    receipt=publish(service)
    assert receipt["records_verified"]==8
    assert captures==[(81,[205])]*3
    restored=RedisCatalogReleaseRegistry(store.catalog_target_identity,client)
    assert restored.active(catalog_scope(81,[205]))==registry.active(catalog_scope(81,[205]))
    pin=service.pin(81,[205]);rows=pin.find_exact({"type":"metric"})
    assert len(rows)==1 and rows[0].metadata["metric_code"]=="amount"
    assert pin.finish()==receipt
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PIN_ALREADY_FINISHED"):pin.search([1,2])
    assert len(store.records)==8


@pytest.mark.parametrize("domains",[[],[205]])
def test_scope_at_every_capture_and_read_is_current_upstream_scope(domains):
    service,store,_,_,captures,_=system();publish(service,domains=domains)
    pin=service.pin(81,domains)
    foreign=pin.find_exact({"business_domain_id":206})
    if domains:assert foreign==[]
    else:assert len(foreign)==6
    if domains:assert pin.find_exact({"business_domain_id":-1})==[]
    pin.finish()
    assert all(model==81 and d==domains for model,d in captures)


@pytest.mark.parametrize("model,domains",[(82,[205]),(81,[]),(81,[206])])
def test_no_release_scope_inheritance(model,domains):
    service,*_=system();publish(service)
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_NOT_PROVEN"):service.pin(model,domains)


def test_multi_domain_fails_closed_before_reads_or_writes():
    service,store,registry,client,captures,_=system()
    with pytest.raises(CatalogEvidenceError,match="EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED"):publish(service,domains=[205,206])
    assert not store.records and not client.values and not captures


@pytest.mark.parametrize("failure",["partial_write","missing_row","extra_row","metadata","vector","authority_drift","registry_conflict"])
def test_failed_generation_never_replaces_existing_active_release(failure):
    service,store,registry,client,_,overrides=system();original=publish(service);scope=catalog_scope(81,[205]);marker=registry.active(scope)
    original_ids=set(store.records)
    if failure=="partial_write":store.fail_add=True
    def after_add():
        new_ids=set(store.records)-original_ids
        row=store.records[sorted(new_ids)[0]]
        if failure=="missing_row":store.records.pop(row.id)
        elif failure=="extra_row":
            extra=deepcopy(row);extra.id="extra";store.records[extra.id]=extra
        elif failure=="metadata":row.metadata["name"]="drift"
        elif failure=="vector":row.vector=[0.9,0.8]
        elif failure=="authority_drift":overrides[(81,(205,))]=authority(formula="SUM(quantity)")
        elif failure=="registry_conflict":
            key=registry._key(scope,"active")
            client.before_execute=lambda:client.set(key,json.dumps(marker))
    store.after_add=after_add
    with pytest.raises((CatalogEvidenceError,RuntimeError)):publish(service,"release-2")
    assert registry.active(scope)==marker
    assert original_ids.issubset(store.records)
    assert registry.manifest(scope,original["vector_index_version"])["catalog_publish_id"]=="release-1"


def test_reserved_publication_id_is_not_rewritten_even_after_failure():
    service,store,_,_,_,_=system();publish(service)
    existing=deepcopy(store.records)
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_ALREADY_RESERVED"):publish(service)
    assert store.records==existing


def test_rollback_retains_rows_and_reactivation_rejects_aba_on_an_old_pin():
    service,store,_,_,_,_=system();first=publish(service)
    pin=service.pin(81,[205]);fingerprint=pin.cache_fingerprint("request")
    second=publish(service,"release-2")
    assert first["vector_index_version"]!=second["vector_index_version"] and len(store.records)==16
    rolled=service.reactivate(81,[205],first["vector_index_version"])
    assert rolled["vector_index_version"]==first["vector_index_version"] and rolled["activation_id"]!=first["activation_id"]
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_CHANGED_DURING_READ"):pin.finish()
    fresh=service.pin(81,[205]);assert fresh.cache_fingerprint("request")!=fingerprint;fresh.finish()


@pytest.mark.parametrize("change",["source","activation","metadata","deleted_record","vector"])
def test_plan_acceptance_revalidates_authority_marker_and_entire_generation(change):
    service,store,_,_,_,overrides=system();publish(service);pin=service.pin(81,[205],query_embedding_contract="fixture-float32-2");pin.search([1,2])
    if change=="source":overrides[(81,(205,))]=authority(formula="SUM(quantity)")
    elif change=="activation":publish(service,"release-2")
    else:
        key=next(iter(store.records));row=store.records[key]
        if change=="metadata":row.metadata["name"]="changed"
        elif change=="deleted_record":store.records.pop(key)
        else:row.vector=[0.9,0.8]
    with pytest.raises(CatalogEvidenceError):pin.finish()


@pytest.mark.parametrize("stamp",["catalog_version","catalog_publish_id","catalog_scope_fingerprint","catalog_record_hash"])
def test_retrieved_rows_recheck_every_release_stamp(stamp):
    service,store,_,_,_,_=system();publish(service);pin=service.pin(81,[205])
    next(iter(store.records.values())).metadata[stamp]="drift"
    with pytest.raises(CatalogEvidenceError,match="CATALOG_INDEX_RECORD_DRIFT"):pin.get_by_where({})


def test_cache_fingerprint_includes_current_scope_and_release():
    service,*_=system();publish(service);publish(service,"wide",domains=[]);publish(service,"other-model",model=82)
    pins=[service.pin(81,[205]),service.pin(81,[]),service.pin(82,[205])]
    assert len({p.cache_fingerprint("same request") for p in pins})==3
    for p in pins:p.finish()


def test_missing_redis_and_wrong_index_target_never_fall_back():
    service,store,registry,client,_,_=system();publish(service);client.fail=True
    with pytest.raises(CatalogEvidenceError,match="CATALOG_REGISTRY_UNAVAILABLE"):service.pin(81,[205])
    with pytest.raises(CatalogEvidenceError,match="CATALOG_TARGET_MISMATCH"):
        CatalogPublication(store,RedisCatalogReleaseRegistry({"different":"target"},FakeRedis()))


def test_pin_rejects_marker_changed_during_full_inventory_export():
    service,store,*_=system();publish(service)
    store.after_inventory=lambda:publish(service,"release-2")
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_CHANGED_DURING_READ"):service.pin(81,[205])


class SchemaClient:
    def __init__(self):self.mutations=[];self.iterator=None
    def has_collection(self, **kw):return True
    def describe_collection(self, **kw):
        return {"fields":[{"name":name,"params":{"dim":2} if name=="embedding" else {}} for name in
            ["record_id","text","metadata","embedding","scope_key","snapshot_version","semantic_model_id","business_domain_id"]]}
    def create_collection(self, **kw):self.mutations.append(kw)
    def load_collection(self, **kw):self.mutations.append(kw)
    def query(self, **kw):raise AssertionError("Capped legacy query is not full inventory")


def catalog_store(client, collections=None):
    return MilvusCatalogStore(client,source_identity={"database":"fixture"},
        collections=collections or {"semantic":"catalog_semantic","physical":"catalog_physical"},
        legacy_collections={"legacy_semantic","legacy_physical","legacy_values","legacy_daily"},embedding_dim=2)


def test_reader_open_does_not_create_load_or_modify_collections():
    client=SchemaClient();store=catalog_store(client)
    assert not client.mutations
    with pytest.raises(CatalogEvidenceError,match="CATALOG_FULL_INVENTORY_REQUIRED"):
        store.get_catalog_inventory({"type":"metric"})
    with pytest.raises(CatalogEvidenceError,match="CATALOG_FULL_INVENTORY_REQUIRED"):
        store.find_exact({"type":"metric"})


@pytest.mark.parametrize("collections",[
    {"semantic":"legacy_semantic","physical":"catalog_physical"},
    {"semantic":"same","physical":"same"},
    {"semantic":"bad collection","physical":"catalog_physical"},
])
def test_generation_collections_cannot_overlap_or_alias_v1(collections):
    with pytest.raises(CatalogEvidenceError,match="CATALOG_COLLECTION_ISOLATION_REQUIRED"):catalog_store(SchemaClient(),collections)


def test_milvus_full_inventory_uses_complete_strong_iterator_and_closes_it():
    client=SchemaClient();calls=[]
    class Iterator:
        def __init__(self):self.i=0;self.closed=False
        def next(self):
            self.i+=1
            return [{"record_id":str(self.i),"text":"fixture","metadata":{},"embedding":[0.1,0.2]}] if self.i<4 else []
        def close(self):self.closed=True
    iterator=Iterator()
    def query_iterator(**kw):calls.append(kw);return iterator
    client.query_iterator=query_iterator
    store=catalog_store(client)
    rows=store.get_catalog_inventory({"type":"metric"})
    assert len(rows)==3 and iterator.closed and not client.mutations
    assert calls[0]["consistency_level"]=="Strong" and "embedding" in calls[0]["output_fields"]


def test_unversioned_entity_value_family_is_explicitly_unsupported():
    store=catalog_store(SchemaClient())
    with pytest.raises(CatalogEvidenceError,match="CATALOG_FAMILY_NOT_IN_RELEASE"):
        store.get_catalog_inventory({"type":"entity_attribute_value"})


@pytest.mark.parametrize("fault",["table_model","field_model","field_source"])
def test_physical_sources_cannot_be_relabelled_into_current_scope(fault):
    current=authority();table=current["physical_catalog"]["tables"][0]
    if fault=="table_model":table["semantic_model_id"]=82
    elif fault=="field_model":table["fields"][0]["semantic_model_id"]=82
    else:table["fields"][0]["data_source_id"]=99
    with pytest.raises(CatalogEvidenceError):build_catalog_records(reseal(current),embed)


def test_staging_checker_never_needs_a_manufactured_published_marker():
    from catalog_publication import verify_generation
    service,store,registry,_,_,_=system()
    current=authority()
    manifest,records=prepare_catalog_generation(current,embed,publication_id="prepared",
        producer_revision="fixture",embedding_contract="fixture",target_identity_hash=registry.target_identity_hash)
    store.add(records)
    receipt=verify_generation(current,manifest,store)
    assert receipt["status"]=="VERIFIED_PREPARED_INVENTORY"
    assert registry.active(current["scope"]) is None


def test_float32_readback_hashes_match_actual_storage_precision():
    import struct
    service,store,*_=system()
    def normalize():
        for row in store.records.values():
            row.vector=[struct.unpack('f',struct.pack('f',v))[0] for v in row.vector]
    store.after_add=normalize
    publish(service)
    assert service.pin(81,[205]).finish()["records_verified"]==8


def test_source_change_after_inventory_prevents_activation():
    service,store,registry,_,_,overrides=system();publish(service);scope=catalog_scope(81,[205]);marker=registry.active(scope)
    store.after_inventory=lambda:overrides.update({(81,(205,)):authority(formula="SUM(quantity)")})
    with pytest.raises(CatalogEvidenceError,match="CATALOG_AUTHORITY_DRIFT"):publish(service,"changed-source")
    assert registry.active(scope)==marker


def test_rollback_cannot_restore_an_index_for_an_obsolete_authority_snapshot():
    service,_,registry,_,_,overrides=system();original=publish(service)
    overrides[(81,(205,))]=authority(formula="SUM(quantity)");new=publish(service,"release-2")
    with pytest.raises(CatalogEvidenceError,match="CATALOG_AUTHORITY_DRIFT"):
        service.reactivate(81,[205],original["vector_index_version"])
    assert registry.active(catalog_scope(81,[205]))["vector_index_version"]==new["vector_index_version"]


def test_backend_ignoring_scope_filter_cannot_supply_an_accepted_foreign_row():
    service,store,*_=system();publish(service);pin=service.pin(81,[205],query_embedding_contract="fixture-float32-2");publish(service,"foreign",domains=[206])
    foreign=next(r for r in store.records.values() if r.metadata["business_domain_id"]==206)
    store.search=lambda *a,**kw:[SearchResult(foreign.id,1.0,foreign.text,foreign.metadata)]
    with pytest.raises(CatalogEvidenceError,match="CATALOG_INDEX_RECORD_DRIFT"):pin.search([1,2])


def test_scope_collision_cannot_steal_another_request_catalog_release():
    service,store,registry,client,_,_=system();publish(service)
    source_scope=catalog_scope(81,[205]);target_scope=catalog_scope(81,[])
    marker=registry.active(source_scope)
    client.set(registry._key(target_scope,"active"),json.dumps(marker))
    client.set(registry._manifest_key(target_scope,marker["vector_index_version"]),
        client.get(registry._manifest_key(source_scope,marker["vector_index_version"])))
    with pytest.raises(CatalogEvidenceError,match="CATALOG_REGISTRY_INVALID"):service.pin(81,[])


def test_registry_compare_and_set_reservation_conflict_writes_no_index_rows():
    service,store,registry,client,_,_=system()
    key=registry._key(catalog_scope(81,[205]),"publication:"+digest("release-1"))
    client.before_execute=lambda:client.set(key,"another owner")
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_CONFLICT"):publish(service)
    assert not store.records and registry.active(catalog_scope(81,[205])) is None


@pytest.mark.parametrize("action",["verify","initialize","publish","reactivate"])
def test_operator_cli_rejects_multi_domain_before_any_client_or_operation(monkeypatch,capsys,action):
    from scripts import manage_catalog_publication as cli
    def forbidden(**kw):raise AssertionError("invalid scope must not open a client")
    monkeypatch.setattr(cli,"open_catalog_store",forbidden)
    assert cli.main([action,"--semantic-model-id","81","--business-domain-id","205","--business-domain-id","206"])==2
    assert json.loads(capsys.readouterr().out)["reason_code"]=="EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED"


def test_operator_read_only_verification_never_initializes_or_publishes(monkeypatch,capsys):
    from scripts import manage_catalog_publication as cli
    service,store,registry,*_=system();publish(service);opened=[]
    def open_store(**kw):opened.append(kw);return store
    monkeypatch.setattr(cli,"open_catalog_store",open_store)
    monkeypatch.setattr(cli,"RedisCatalogReleaseRegistry",lambda *a:registry)
    monkeypatch.setattr(cli,"CatalogPublication",lambda *a:service)
    assert cli.main(["verify","--semantic-model-id","81","--business-domain-id","205"])==0
    receipt=json.loads(capsys.readouterr().out)
    assert opened==[{"initialize":False}]
    assert receipt["status"]=="VERIFIED_INDEX_GENERATION" and receipt["records_verified"]==8
    assert not any(private in str(receipt) for private in ["SUM(amount)","fixture model","fixture domain"])


def test_operator_cli_errors_do_not_dump_private_connection_material(monkeypatch,capsys):
    from scripts import manage_catalog_publication as cli
    def fail(**kw):raise RuntimeError("private_connection_material")
    monkeypatch.setattr(cli,"open_catalog_store",fail)
    assert cli.main(["verify","--semantic-model-id","81"])==2
    output=capsys.readouterr().out
    assert "private_connection_material" not in output and "CATALOG_OPERATION_FAILED" in output


@pytest.mark.parametrize("owner",[None,-1])
def test_model_owned_metric_without_domain_is_model_wide_only(owner):
    current=authority(())
    current["documents"].append({"semantic_model":{"id":81,"name":"fixture model"},"business_domain":None,
        "entities":[],"dimensions":[],"metrics":[{"metric_code":"global_metric","metric_name":"模型指标","business_domain":owner}]})
    records,_=build_catalog_records(reseal(current),embed)
    global_records=[r for r in records if r.metadata.get("metric_code")=="global_metric"]
    assert len(global_records)==1 and global_records[0].metadata["business_domain_id"]==-1


@pytest.mark.parametrize("fault",[None,"orphan_domain"])
def test_capture_keeps_global_metrics_without_enlarging_explicit_scope(monkeypatch,fault):
    import mysql_tool as mysql
    from catalog_release import CATALOG_TABLES,capture_catalog
    from test_catalog_release import Connection
    conn=Connection();calls=[]
    monkeypatch.setattr(mysql,"_get_connection",lambda:conn)
    def query(sql,args=None):
        if "information_schema" in sql:return [{"table_name":n,"engine":"InnoDB"} for n in CATALOG_TABLES]
        return [{"server_uuid":"fixture","database_name":"fixture"}]
    monkeypatch.setattr(mysql,"_query",query)
    monkeypatch.setattr(mysql,"get_business_domains",lambda model:[{"id":205}])
    monkeypatch.setattr(mysql,"get_dsl_by_scope",lambda model,domain:authority([domain])["documents"][0])
    def metrics(model):
        calls.append(model)
        return [{"metric_code":"global_metric","business_domain":999 if fault else None}]
    monkeypatch.setattr(mysql,"get_metric",metrics)
    monkeypatch.setattr(mysql,"get_table_field_by_scope",lambda *a,**kw:authority()["physical_catalog"])
    if fault:
        with pytest.raises(CatalogEvidenceError,match="CATALOG_ORPHAN_METRIC_DOMAIN"):capture_catalog(81,[])
    else:
        snapshot=capture_catalog(81,[])
        assert len(snapshot["documents"])==2
        assert snapshot["documents"][0]["business_domain"] is None
        assert snapshot["documents"][0]["metrics"][0]["metric_code"]=="global_metric"
    calls.clear()
    explicit=capture_catalog(81,[205])
    assert len(explicit["documents"])==1 and not calls


def test_lost_activation_acknowledgement_requires_readback_not_a_blind_retry():
    service,store,registry,client,_,_=system();publish(service)
    def lost_ack():raise ConnectionError("fixture lost acknowledgement")
    store.after_add=lambda:setattr(client,"after_execute",lost_ack)
    with pytest.raises(CatalogEvidenceError,match="CATALOG_ACTIVATION_OUTCOME_UNKNOWN"):publish(service,"release-2")
    assert service.pin(81,[205]).finish()["catalog_publish_id"]=="release-2"
    with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_ALREADY_RESERVED"):publish(service,"release-2")


def test_ann_requires_matching_query_embedding_contract_and_dimensions():
    service,store,*_=system();publish(service)
    with pytest.raises(CatalogEvidenceError,match="CATALOG_QUERY_EMBEDDING_CONTRACT_MISMATCH"):
        service.pin(81,[205],query_embedding_contract="different-model")
    pin=service.pin(81,[205])
    with pytest.raises(CatalogEvidenceError,match="CATALOG_QUERY_EMBEDDING_CONTRACT_REQUIRED"):pin.search([1,2])
    checked=service.pin(81,[205],query_embedding_contract="fixture-float32-2")
    with pytest.raises(CatalogEvidenceError,match="CATALOG_EMBEDDING_DIMENSION_MISMATCH"):checked.search([1,2,3])
    assert len(checked.search([1,2],top_k=5))==5
    checked.finish()


def test_index_dimension_mismatch_fails_before_reservation_or_write():
    service,store,_,client,_,_=system();store.embedding_dim=3
    with pytest.raises(CatalogEvidenceError,match="CATALOG_EMBEDDING_DIMENSION_MISMATCH"):publish(service)
    assert not client.values and not store.records


def test_catalog_ann_and_exact_reads_use_strong_consistency():
    client=SchemaClient();calls=[]
    def search(**kw):calls.append(kw);return [[]]
    client.search=search
    store=catalog_store(client)
    assert store.search([0.1,0.2],where={"type":"metric"})==[]
    assert calls[0]["consistency_level"]=="Strong"
    class Iterator:
        def next(self):return []
        def close(self):pass
    def iterator(**kw):calls.append(kw);return Iterator()
    client.query_iterator=iterator
    assert store.find_exact({"type":"metric"})==[]
    assert calls[1]["consistency_level"]=="Strong" and calls[1]["timeout"]==store.timeout


def test_catalog_collection_cannot_alias_a_sanitized_legacy_family_name():
    with pytest.raises(CatalogEvidenceError,match="CATALOG_COLLECTION_ISOLATION_REQUIRED"):
        MilvusCatalogStore(SchemaClient(),source_identity={"database":"fixture"},
            collections={"semantic":"catalog_semantic","physical":"catalog_physical"},
            legacy_collections={"catalog semantic"},embedding_dim=2)


@pytest.mark.parametrize("changed_activation",[False,True])
def test_private_bundle_verifier_accepts_registry_markers_and_rejects_aba(changed_activation):
    from scripts.check_catalog_release import check_bundle
    service,store,registry,_,_,_=system();receipt=publish(service);scope=catalog_scope(81,[205])
    marker=registry.active(scope)
    bundle={"current_snapshot":authority(),"manifest":registry.manifest(scope,receipt["vector_index_version"]),
        "index_records":[{"id":r.id,"text":r.text,"metadata":r.metadata} for r in store.records.values()],
        "marker_before":marker,"marker_after":deepcopy(marker),"inventory_complete":True}
    if changed_activation:
        bundle["marker_after"]["activation_id"]="0"*32
        with pytest.raises(CatalogEvidenceError,match="CATALOG_PUBLICATION_CHANGED_DURING_READ"):check_bundle(bundle)
    else:
        result=check_bundle(bundle)
        assert result["status"]=="VERIFIED_SUPPLIED_EVIDENCE" and result["live_runtime_verified"] is False
