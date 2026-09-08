"""Milvus catalog generations isolated from all existing V1 collections.

Opening a reader validates existing schema without mutating it. Explicit
initialize=True is an operator publication operation and is never used by a
query or at module import.
"""
from catalog_release import CatalogEvidenceError
from vector_store import MilvusVectorStore


class MilvusCatalogStore(MilvusVectorStore):
    _catalog_strong_reads = True

    def __init__(self, client, *, source_identity, collections, legacy_collections,
                 embedding_dim, timeout=30, initialize=False):
        names = set(collections.values())
        legacy_names = {self._safe_collection_name(name) for name in legacy_collections}
        if (set(collections) != {"semantic", "physical"} or len(names) != 2
                or names.intersection(legacy_names)
                or any(self._safe_collection_name(name) != name for name in names)):
            raise CatalogEvidenceError("CATALOG_COLLECTION_ISOLATION_REQUIRED")
        if not isinstance(source_identity, dict) or not source_identity or type(embedding_dim) is not int or embedding_dim <= 0:
            raise CatalogEvidenceError("CATALOG_TARGET_IDENTITY_REQUIRED")
        self._client, self._collections = client, dict(collections)
        self.embedding_dim, self.timeout = embedding_dim, timeout
        self.catalog_target_identity = {"backend": "milvus", "source": dict(source_identity),
                                        "collections": dict(collections)}
        if initialize:
            self.ensure_schema()
        else:
            for name in names:
                if not client.has_collection(collection_name=name):
                    raise CatalogEvidenceError("CATALOG_COLLECTION_NOT_INITIALIZED")
                description = client.describe_collection(collection_name=name)
                fields = {field["name"]: field for field in description.get("fields", [])}
                if not {"record_id", "text", "metadata", "embedding", "scope_key", "snapshot_version", "semantic_model_id", "business_domain_id"}.issubset(fields):
                    raise CatalogEvidenceError("CATALOG_COLLECTION_SCHEMA_MISMATCH")
                if int(fields["embedding"].get("params", {}).get("dim") or 0) != embedding_dim:
                    raise CatalogEvidenceError("CATALOG_COLLECTION_SCHEMA_MISMATCH")

    def _candidate_families(self, where):
        families = super()._candidate_families(where)
        if any(family not in self._collections for family in families):
            raise CatalogEvidenceError("CATALOG_FAMILY_NOT_IN_RELEASE")
        return families


def open_catalog_store(*, initialize=False):
    """Use configured service access; never use model/request-supplied endpoints."""
    from pymilvus import MilvusClient
    import config

    legacy = {"semantic": config.MILVUS_SEMANTIC_COLLECTION, "physical": config.MILVUS_PHYSICAL_COLLECTION,
              "entity_value": config.MILVUS_ENTITY_VALUE_COLLECTION, "daily": config.MILVUS_DAILY_COLLECTION}
    collections = {family: name + "_catalog" for family, name in legacy.items() if family in {"semantic", "physical"}}
    uri = f"http://{config.MILVUS_HOST}:{config.MILVUS_PORT}"
    options = {"uri": uri, "db_name": config.MILVUS_DATABASE}
    if config.MILVUS_USER:
        options.update(user=config.MILVUS_USER, password=config.MILVUS_PASSWORD)
    return MilvusCatalogStore(MilvusClient(**options), source_identity={"uri": uri, "database": config.MILVUS_DATABASE},
        collections=collections, legacy_collections=set(legacy.values()), embedding_dim=config.EMBEDDING_DIM,
        timeout=config.MILVUS_TIMEOUT_SECONDS, initialize=initialize)
