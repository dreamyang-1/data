"""Minimal catalog capture and release-evidence contract for cutover preflight.

No HTTP endpoint, embedding call, index mutation or production routing is added.
Private snapshots contain governed metadata; public reports expose hashes only.
Evidence must come from the trusted operator/service boundary, never an LLM.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

CONTRACT_VERSION = "catalog-release-v1"
CATALOG_TABLES = (
    "semantic_model", "semantic_model_business_domain", "semantic_model_entity_type",
    "semantic_model_attribute_config", "semantic_model_relation_config",
    "semantic_model_entity_bind_indicator", "semantic_model_indicator",
    "semantic_model_dimension", "semantic_model_table", "semantic_model_field",
)
STAMP_FIELDS = {"catalog_version", "catalog_publish_id", "catalog_scope_fingerprint", "catalog_record_hash"}


class CatalogEvidenceError(ValueError):
    """Stable bounded code only; do not expose raw catalog content in errors."""


def digest(value) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise CatalogEvidenceError("CATALOG_DOCUMENT_INVALID") from None
    return hashlib.sha256(encoded).hexdigest()


def catalog_scope(semantic_model_id: int, business_domain_ids=()) -> dict:
    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        raise CatalogEvidenceError("REQUEST_SCOPE_INVALID")
    if not isinstance(business_domain_ids, (list, tuple)) or any(type(v) is not int or v <= 0 for v in business_domain_ids):
        raise CatalogEvidenceError("REQUEST_SCOPE_INVALID")
    domains = sorted(set(business_domain_ids))
    if len(domains) > 1:
        raise CatalogEvidenceError("EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED")
    return {"semantic_model_id": semantic_model_id, "business_domain_ids": domains,
            "scope_mode": "EXPLICIT_DOMAINS" if domains else "MODEL_WIDE"}


def snapshot_from_documents(scope: dict, source_identity: dict, documents: list, physical_catalog: dict) -> dict:
    """Construct a content identity, not proof that these documents were published.

    The live capture below establishes transaction provenance. This pure seam
    also permits deterministic fixtures, which are never labeled live evidence.
    Ordered semantic arrays are preserved; only domain-document order is normalized.
    """
    if scope != catalog_scope(scope.get("semantic_model_id"), scope.get("business_domain_ids")):
        raise CatalogEvidenceError("REQUEST_SCOPE_INVALID")
    if not source_identity or not documents:
        raise CatalogEvidenceError("CATALOG_SOURCE_IDENTITY_MISSING")
    seen = set()
    for doc in documents:
        if doc.get("semantic_model", {}).get("id") != scope["semantic_model_id"]:
            raise CatalogEvidenceError("CATALOG_MODEL_MISMATCH")
        domain = (doc.get("business_domain") or {}).get("id")
        if domain is not None and (type(domain) is not int or domain <= 0):
            raise CatalogEvidenceError("CATALOG_DOMAIN_MISMATCH")
        if domain in seen or (scope["business_domain_ids"] and domain not in scope["business_domain_ids"]):
            raise CatalogEvidenceError("CATALOG_DOMAIN_MISMATCH")
        seen.add(domain)
    if scope["business_domain_ids"] and seen != set(scope["business_domain_ids"]):
        raise CatalogEvidenceError("CATALOG_DOMAIN_MISMATCH")
    if physical_catalog.get("scope", {}).get("semantic_model_id") != scope["semantic_model_id"]:
        raise CatalogEvidenceError("CATALOG_PHYSICAL_MODEL_MISMATCH")
    material = {"contract_version": CONTRACT_VERSION, "scope": scope,
                "source_identity_hash": digest(source_identity),
                "documents": sorted(documents, key=lambda d: (d.get("business_domain") or {}).get("id") or 0),
                "physical_catalog": physical_catalog}
    # JSON round-trip prevents a caller mutating captured inputs by reference.
    return json.loads(json.dumps({**material, "catalog_version": digest(material)}, ensure_ascii=False))


def validate_snapshot(snapshot: dict) -> None:
    version = snapshot.get("catalog_version")
    material = {k: v for k, v in snapshot.items() if k != "catalog_version"}
    if material.get("contract_version") != CONTRACT_VERSION or version != digest(material):
        raise CatalogEvidenceError("CATALOG_SNAPSHOT_DIGEST_MISMATCH")
    scope = snapshot.get("scope", {})
    if scope != catalog_scope(scope.get("semantic_model_id"), scope.get("business_domain_ids")):
        raise CatalogEvidenceError("REQUEST_SCOPE_INVALID")


def capture_catalog(semantic_model_id: int, business_domain_ids=()) -> dict:
    """Read authoritative semantic/physical metadata on one MySQL snapshot.

    Fail closed when tables are missing/nontransactional or capture fails.
    No source business values, data-source credentials or embeddings are read.
    """
    import mysql_tool as mysql

    scope = catalog_scope(semantic_model_id, business_domain_ids)
    with mysql.consistent_catalog_read():
        placeholders = ",".join(["%s"] * len(CATALOG_TABLES))
        engines = mysql._query(
            "SELECT TABLE_NAME AS table_name, ENGINE AS engine FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (" + placeholders + ")", CATALOG_TABLES)
        if {r["table_name"] for r in engines} != set(CATALOG_TABLES) or any(str(r["engine"]).upper() != "INNODB" for r in engines):
            raise CatalogEvidenceError("CATALOG_CONSISTENT_SNAPSHOT_UNSUPPORTED")
        identity = mysql._query("SELECT @@server_uuid AS server_uuid, DATABASE() AS database_name")
        if len(identity) != 1 or not all(identity[0].get(k) for k in ("server_uuid", "database_name")):
            raise CatalogEvidenceError("CATALOG_SOURCE_IDENTITY_MISSING")
        domains = scope["business_domain_ids"] or [r["id"] for r in mysql.get_business_domains(semantic_model_id)]
        documents = [mysql.get_dsl_by_scope(semantic_model_id, domain) for domain in domains or [None]]
        physical = mysql.get_table_field_by_scope(semantic_model_id, business_domain_id=scope["business_domain_ids"][0] if scope["business_domain_ids"] else None)
        return snapshot_from_documents(scope, identity[0], documents, physical)


def record_hash(record: dict) -> str:
    """Hash complete retrievable text/metadata; vector quality is a separate gate."""
    if not isinstance(record.get("id"), str) or not record["id"] or not isinstance(record.get("text"), str) or not isinstance(record.get("metadata"), dict):
        raise CatalogEvidenceError("CATALOG_RECORD_INVALID")
    return digest({"id": record["id"], "text": record["text"],
                   "metadata": {k: v for k, v in record["metadata"].items() if k not in STAMP_FIELDS}})


def prepare_release(snapshot: dict, records: Iterable[dict], *, publication_id: str, producer_revision: str, embedding_contract: str) -> tuple[dict, list[dict]]:
    """Prepare an immutable manifest and stamped rows; never mark publication PASS."""
    validate_snapshot(snapshot)
    if any(not isinstance(v, str) or not v.strip() for v in (publication_id, producer_revision, embedding_contract)):
        raise CatalogEvidenceError("CATALOG_RELEASE_IDENTITY_MISSING")
    scope = snapshot["scope"]
    stamped, hashes = [], {}
    for record in records:
        value = json.loads(json.dumps(record, ensure_ascii=False))
        metadata = value.get("metadata", {})
        if type(metadata.get("semantic_model_id")) is not int or metadata["semantic_model_id"] != scope["semantic_model_id"]:
            raise CatalogEvidenceError("CATALOG_MODEL_MISMATCH")
        # An explicit release includes only owned projections, not shared -1
        # originals. The existing full publisher must separate these families.
        domain = metadata.get("business_domain_id")
        allowed_domains = scope["business_domain_ids"] or [
            -1, *((doc.get("business_domain") or {}).get("id") for doc in snapshot["documents"])
        ]
        if type(domain) is not int or domain not in allowed_domains:
            raise CatalogEvidenceError("CATALOG_DOMAIN_MISMATCH")
        if any(k in metadata for k in STAMP_FIELDS):
            raise CatalogEvidenceError("CATALOG_RECORD_ALREADY_STAMPED")
        content_hash = record_hash(value)
        if value["id"] in hashes:
            raise CatalogEvidenceError("CATALOG_DUPLICATE_RECORD_ID")
        hashes[value["id"]] = content_hash
        value["metadata"].update(catalog_version=snapshot["catalog_version"], catalog_publish_id=publication_id,
                                 catalog_scope_fingerprint=digest(scope), catalog_record_hash=content_hash)
        stamped.append(value)
    if not hashes:
        raise CatalogEvidenceError("CATALOG_EMPTY_PUBLICATION")
    manifest = {"contract_version": CONTRACT_VERSION, "scope": scope, "catalog_version": snapshot["catalog_version"],
                "catalog_publish_id": publication_id, "producer_revision": producer_revision,
                "embedding_contract": embedding_contract, "source_identity_hash": snapshot["source_identity_hash"],
                "record_hashes": dict(sorted(hashes.items()))}
    manifest["vector_index_version"] = digest(manifest)
    return manifest, stamped


def verify_release(current_snapshot: dict, manifest: dict, records: Iterable[dict], *, published_marker: dict | None) -> dict:
    """Check trusted complete inventory and marker against a current authority read.

    Caller must supply a complete export and stable before/after publication
    marker. A retrieval top-k or this pure function alone is not live readiness.
    """
    validate_snapshot(current_snapshot)
    material = {k: v for k, v in manifest.items() if k != "vector_index_version"}
    version = digest(material)
    if manifest.get("contract_version") != CONTRACT_VERSION or manifest.get("vector_index_version") != version:
        raise CatalogEvidenceError("CATALOG_MANIFEST_DIGEST_MISMATCH")
    if manifest.get("scope") != current_snapshot["scope"]:
        raise CatalogEvidenceError("CATALOG_SCOPE_MISMATCH")
    if manifest.get("catalog_version") != current_snapshot["catalog_version"] or manifest.get("source_identity_hash") != current_snapshot["source_identity_hash"]:
        raise CatalogEvidenceError("CATALOG_AUTHORITY_DRIFT")
    if published_marker != {"state": "PUBLISHED", "vector_index_version": version}:
        raise CatalogEvidenceError("CATALOG_PUBLICATION_NOT_PROVEN")
    actual = {}
    for record in records:
        content = record_hash(record)
        metadata = record["metadata"]
        expected_stamp = {"catalog_version": current_snapshot["catalog_version"],
                          "catalog_publish_id": manifest["catalog_publish_id"],
                          "catalog_scope_fingerprint": digest(manifest["scope"]), "catalog_record_hash": content}
        if any(metadata.get(k) != v for k, v in expected_stamp.items()):
            raise CatalogEvidenceError("CATALOG_INDEX_RECORD_DRIFT")
        if record["id"] in actual:
            raise CatalogEvidenceError("CATALOG_DUPLICATE_RECORD_ID")
        actual[record["id"]] = content
    if not actual or actual != manifest.get("record_hashes"):
        raise CatalogEvidenceError("CATALOG_INDEX_INVENTORY_DRIFT")
    return {"status": "VERIFIED_SUPPLIED_EVIDENCE", "scope": manifest["scope"],
            "catalog_version": manifest["catalog_version"], "catalog_publish_id": manifest["catalog_publish_id"],
            "vector_index_version": version, "records_verified": len(actual),
            "live_runtime_verified": False}
