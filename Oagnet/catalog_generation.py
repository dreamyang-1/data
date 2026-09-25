"""Deterministic catalog generation using the existing DSL/physical builders.

This boundary consumes a captured snapshot, never rereads MySQL during building,
and refuses missing identities and silently omitted source records. Embedding is
an injected existing transport; preparation does not write a vector store.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import math

from catalog_release import CatalogEvidenceError, digest, prepare_release, validate_snapshot
from dimension_scope import normalize_governed_id, project_dimension_to_domain
from vector_store import VectorRecord, build_records_from_dsl, build_records_from_tables

GENERATION_CONTRACT = "catalog-generation-v1"


def configured_embedding_contract():
    """Bind publisher/query transports to the same configured embedding service."""
    import config
    return digest({"model": config.EMBEDDING_MODEL, "dimension": config.EMBEDDING_DIM,
                   "endpoint_identity_hash": digest(config.BASE_URL)})


def _code(value, key):
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise CatalogEvidenceError("CATALOG_STABLE_ID_REQUIRED")
    return result


def _positive(value):
    if type(value) is not int or value <= 0:
        raise CatalogEvidenceError("CATALOG_GOVERNED_ID_REQUIRED")
    return value


def _dsl_inventory(document):
    """Independent source identities checked against the existing builder output."""
    model = document["semantic_model"]["id"]
    domain = (document.get("business_domain") or {}).get("id", -1)
    prefix = f"sm{model}_bd{domain}" if domain != -1 else f"sm{model}"
    shared = f"sm{model}"
    expected, excluded = set(), Counter()

    def add(identity):
        if identity in expected:
            raise CatalogEvidenceError("CATALOG_SOURCE_ID_COLLISION")
        expected.add(identity)

    for entity in document["entities"]:
        code = _code(entity, "entity_code")
        if normalize_governed_id(entity.get("entity_id")) is None:
            raise CatalogEvidenceError("CATALOG_GOVERNED_ID_REQUIRED")
        if entity.get("business_domain") != domain:
            raise CatalogEvidenceError("CATALOG_ENTITY_OWNERSHIP_INVALID")
        add(f"{prefix}:entity:{code}")
        for attribute in entity.get("attributes") or []:
            if normalize_governed_id(attribute.get("attribute_id")) is None:
                raise CatalogEvidenceError("CATALOG_GOVERNED_ID_REQUIRED")
            add(f"{prefix}:attr:{code}.{_code(attribute, 'attr_code')}")
        for relation in entity.get("relations") or []:
            add(f"{prefix}:relation:{code}.{_code(relation, 'relation_code')}")
    for metric in document["metrics"]:
        add(f"{prefix}:metric:{_code(metric, 'metric_code')}")
        if metric.get("business_domain") != domain and not (domain == -1 and metric.get("business_domain") is None):
            raise CatalogEvidenceError("CATALOG_METRIC_OWNERSHIP_INVALID")
    for dimension in document["dimensions"]:
        code = _code(dimension, "dim_code")
        add(f"{shared}:dim:{code}")
        for enum in dimension.get("enum_list") or []:
            add(f"{shared}:enum:{code}.{_code(enum, 'code')}")
        projected = project_dimension_to_domain(dimension, document)
        if projected is None:
            # A shared definition is not silently deemed an owned projection.
            # Completeness of owned defaults/rules remains a separate gate.
            excluded["NO_OWNERSHIP_PROVEN_DIMENSION_PROJECTION"] += 1
        else:
            add(f"{prefix}:dim:{code}")
            for enum in projected.get("enum_list") or []:
                add(f"{prefix}:enum:{code}.{_code(enum, 'code')}")
    return expected, excluded


def build_catalog_records(snapshot, embed_fn):
    """Build every supported source record, rejecting silent omissions/collisions.

    Publication coverage here means captured semantic/physical source coverage,
    not certification of every business capability or external value dataset.
    """
    validate_snapshot(snapshot)
    scope = snapshot["scope"]
    combined, exclusions, source_counts = {}, Counter(), Counter()

    def add(records, expected):
        if {r.id for r in records} != expected or len(records) != len(expected):
            raise CatalogEvidenceError("CATALOG_SOURCE_RECORD_COVERAGE_MISMATCH")
        for record in records:
            # Repeated shared definitions across domain documents must agree.
            # First-wins deduplication would hide inconsistent authority input.
            material = {"text": record.text, "metadata": record.metadata}
            if record.id in combined and digest(material) != digest({
                "text": combined[record.id].text, "metadata": combined[record.id].metadata,
            }):
                raise CatalogEvidenceError("CATALOG_SHARED_DEFINITION_CONFLICT")
            combined.setdefault(record.id, record)

    for document in snapshot["documents"]:
        expected, excluded = _dsl_inventory(document)
        exclusions.update(excluded)
        add(build_records_from_dsl(deepcopy(document), embed_fn), expected)
        source_counts["domain_documents"] += 1

    by_source = defaultdict(list)
    physical = snapshot["physical_catalog"]
    for table in physical["tables"]:
        _positive(table.get("table_id"))
        if table.get("semantic_model_id") != scope["semantic_model_id"]:
            raise CatalogEvidenceError("CATALOG_PHYSICAL_MODEL_MISMATCH")
        source_id = _positive(table.get("data_source_id"))
        by_source[source_id].append(table)
    for source_id, tables in by_source.items():
        expected = set()
        for table in tables:
            name = _code(table, "table_name")
            identities = [f"ds{source_id}:table:{name}"]
            for field in table.get("fields") or []:
                _positive(field.get("field_id"))
                if field.get("table_id") != table["table_id"]:
                    raise CatalogEvidenceError("CATALOG_FIELD_OWNERSHIP_INVALID")
                if field.get("semantic_model_id") is not None and field["semantic_model_id"] != scope["semantic_model_id"]:
                    raise CatalogEvidenceError("CATALOG_PHYSICAL_MODEL_MISMATCH")
                if field.get("data_source_id") is not None and field["data_source_id"] != source_id:
                    raise CatalogEvidenceError("CATALOG_FIELD_OWNERSHIP_INVALID")
                identities.append(f"ds{source_id}:field:{name}.{_code(field, 'field_name')}")
            for identity in identities:
                if identity in expected:
                    raise CatalogEvidenceError("CATALOG_SOURCE_ID_COLLISION")
                expected.add(identity)
        records = build_records_from_tables({"scope": {"semantic_model_id": scope["semantic_model_id"],
            "data_source_id": source_id}, "tables": deepcopy(tables)}, embed_fn)
        # The capture's existing physical loader proves explicit ownership.
        # The generated rows must carry that exact release scope as well.
        for record in records:
            record.metadata["business_domain_id"] = scope["business_domain_ids"][0] if scope["business_domain_ids"] else -1
        add(records, expected)
        source_counts["physical_data_sources"] += 1

    selected = []
    for record in combined.values():
        if scope["business_domain_ids"] and record.metadata["business_domain_id"] == -1:
            if record.metadata["type"] not in {"dimension", "enum"}:
                raise CatalogEvidenceError("CATALOG_DOMAIN_MISMATCH")
            exclusions["MODEL_WIDE_SHARED_ORIGINAL"] += 1
        else:
            selected.append(record)
    coverage = {"contract": GENERATION_CONTRACT, "catalog_version": snapshot["catalog_version"],
        "source_counts": dict(source_counts), "by_type": dict(Counter(r.metadata["type"] for r in selected)),
        "records_generated": len(selected), "explicit_exclusions": dict(exclusions),
        "capability_coverage_verified": False,
        "excluded_dependencies": ["external_entity_values", "data_source_routing", "SQL_independent_catalog_reads"]}
    if "entity_value_sources" in snapshot["physical_catalog"]:
        coverage["captured_dependencies"] = ["entity_value_source_mappings", "data_source_route_configuration"]
        coverage["excluded_dependencies"] = ["external_entity_values", "data_source_runtime_identity", "SQL_independent_catalog_reads"]
    return sorted(selected, key=lambda r: r.id), coverage


def vector_digest(vector):
    if not isinstance(vector, (list, tuple)) or not vector or any(
        type(v) not in (int, float) or not math.isfinite(v) for v in vector
    ):
        raise CatalogEvidenceError("CATALOG_EMBEDDING_INVALID")
    # Milvus FLOAT_VECTOR stores float32; use that representation before hashing.
    import struct
    try:
        normalized = [struct.unpack("!f", struct.pack("!f", float(v)))[0] for v in vector]
    except (OverflowError, struct.error):
        raise CatalogEvidenceError("CATALOG_EMBEDDING_INVALID") from None
    if not all(math.isfinite(v) for v in normalized):
        raise CatalogEvidenceError("CATALOG_EMBEDDING_INVALID")
    return digest(normalized)


def prepare_catalog_generation(snapshot, embed_fn, *, publication_id, producer_revision,
                               embedding_contract, target_identity_hash):
    """Namespace immutable generation IDs without changing legacy builder IDs."""
    if not isinstance(target_identity_hash, str) or len(target_identity_hash) != 64 or any(c not in "0123456789abcdef" for c in target_identity_hash):
        raise CatalogEvidenceError("CATALOG_TARGET_IDENTITY_REQUIRED")
    scope = snapshot["scope"]
    token = digest({"scope": scope, "publication_id": publication_id, "target": target_identity_hash})
    records, coverage = build_catalog_records(snapshot, embed_fn)
    dimensions = {len(r.vector) for r in records}
    if len(dimensions) != 1:
        raise CatalogEvidenceError("CATALOG_EMBEDDING_DIMENSION_MISMATCH")
    prepared = []
    vectors = {}
    for record in records:
        rid = "catalog:" + token + ":" + digest(record.id)
        metadata = {**record.metadata, "catalog_logical_id": record.id,
            "snapshot_version": "catalog:" + token, "scope_key": "catalog:" + digest(scope),
            "catalog_vector_hash": vector_digest(record.vector)}
        prepared.append({"id": rid, "text": record.text, "metadata": metadata})
        vectors[rid] = record.vector
    manifest, stamped = prepare_release(snapshot, prepared, publication_id=publication_id,
        producer_revision=producer_revision, embedding_contract=embedding_contract)
    manifest.update(generation_contract=GENERATION_CONTRACT, generation_token=token,
                    target_identity_hash=target_identity_hash, coverage=coverage,
                    embedding_dimension=next(iter(dimensions)))
    manifest["vector_index_version"] = digest({k:v for k,v in manifest.items() if k != "vector_index_version"})
    return manifest, [VectorRecord(r["id"], r["text"], vectors[r["id"]], r["metadata"]) for r in stamped]
