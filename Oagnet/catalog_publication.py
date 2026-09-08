"""Versioned publication and pinned plan-only reads; no public route changes.

Only an existing trusted operator/service boundary may construct this object.
It uses the current scope and fresh authoritative capture, never request history
or model-selected scope. Publication is an explicit write operation; constructors
and query sessions do not publish, embed or modify schemas.
"""
from copy import deepcopy

from catalog_generation import prepare_catalog_generation, vector_digest
from catalog_release import CatalogEvidenceError, capture_catalog, catalog_scope, digest, record_hash, verify_prepared_inventory


def generation_filter(manifest):
    conditions = [{"semantic_model_id": manifest["scope"]["semantic_model_id"]},
        {"scope_key": "catalog:" + digest(manifest["scope"])},
        {"snapshot_version": "catalog:" + manifest["generation_token"]}]
    if manifest["scope"]["business_domain_ids"]:
        conditions.append({"business_domain_id": manifest["scope"]["business_domain_ids"][0]})
    return {"$and": conditions}


def _material(record):
    return {"id": record.id, "text": record.text, "metadata": record.metadata}


def verify_generation(snapshot, manifest, store):
    # This API deliberately has no legacy capped-query fallback.
    inventory = store.get_catalog_inventory(generation_filter(manifest))
    for record in inventory:
        if len(record.vector) != manifest["embedding_dimension"] or vector_digest(record.vector) != record.metadata.get("catalog_vector_hash"):
            raise CatalogEvidenceError("CATALOG_INDEX_VECTOR_DRIFT")
    return verify_prepared_inventory(snapshot, manifest, [_material(r) for r in inventory])


class CatalogPublication:
    def __init__(self, store, registry, capture=capture_catalog):
        if digest(store.catalog_target_identity) != registry.target_identity_hash:
            raise CatalogEvidenceError("CATALOG_TARGET_MISMATCH")
        self.store, self.registry, self.capture = store, registry, capture

    def publish(self, semantic_model_id, business_domain_ids=(), *, embed_fn,
                publication_id, producer_revision, embedding_contract, expected_catalog_version=None):
        scope = catalog_scope(semantic_model_id, business_domain_ids)
        expected = self.registry.active(scope)
        snapshot = self.capture(semantic_model_id, scope["business_domain_ids"])
        if snapshot["scope"] != scope:
            raise CatalogEvidenceError("CATALOG_SCOPE_MISMATCH")
        # Bind an operator-reviewed candidate to the actual capture used below;
        # a separate earlier preflight cannot establish this identity.
        if expected_catalog_version is not None and snapshot["catalog_version"] != expected_catalog_version:
            raise CatalogEvidenceError("CATALOG_OPERATION_SOURCE_CHANGED")
        manifest, records = prepare_catalog_generation(snapshot, embed_fn,
            publication_id=publication_id, producer_revision=producer_revision,
            embedding_contract=embedding_contract, target_identity_hash=self.registry.target_identity_hash)
        if manifest["embedding_dimension"] != self.store.embedding_dim:
            raise CatalogEvidenceError("CATALOG_EMBEDDING_DIMENSION_MISMATCH")
        self.registry.reserve(manifest)
        # Separate generation IDs preserve every previously published row.
        self.store.add(records)
        current = self.capture(semantic_model_id, scope["business_domain_ids"])
        verify_generation(current, manifest, self.store)
        # Freshness is rechecked after reading the entire index, not just before.
        after = self.capture(semantic_model_id, scope["business_domain_ids"])
        if after["catalog_version"] != current["catalog_version"] or after["scope"] != scope:
            raise CatalogEvidenceError("CATALOG_AUTHORITY_DRIFT")
        marker = self.registry.activate_verified(manifest, expected)
        return self._receipt(manifest, marker)

    def reactivate(self, semantic_model_id, business_domain_ids, vector_index_version):
        """Explicit rollback of index activation, only if current authority agrees."""
        scope = catalog_scope(semantic_model_id, business_domain_ids)
        expected = self.registry.active(scope)
        manifest = self.registry.manifest(scope, vector_index_version)
        current = self.capture(semantic_model_id, scope["business_domain_ids"])
        verify_generation(current, manifest, self.store)
        after = self.capture(semantic_model_id, scope["business_domain_ids"])
        if after["catalog_version"] != current["catalog_version"] or after["scope"] != scope:
            raise CatalogEvidenceError("CATALOG_AUTHORITY_DRIFT")
        marker = self.registry.activate_verified(manifest, expected)
        return self._receipt(manifest, marker)

    def pin(self, semantic_model_id, business_domain_ids=(), *, query_embedding_contract=None):
        scope = catalog_scope(semantic_model_id, business_domain_ids)
        marker = self.registry.active(scope)
        if marker is None:
            raise CatalogEvidenceError("CATALOG_PUBLICATION_NOT_PROVEN")
        manifest = self.registry.manifest(scope, marker["vector_index_version"])
        if query_embedding_contract is not None and query_embedding_contract != manifest["embedding_contract"]:
            raise CatalogEvidenceError("CATALOG_QUERY_EMBEDDING_CONTRACT_MISMATCH")
        snapshot = self.capture(semantic_model_id, scope["business_domain_ids"])
        verify_generation(snapshot, manifest, self.store)
        if self.registry.active(scope) != marker:
            raise CatalogEvidenceError("CATALOG_PUBLICATION_CHANGED_DURING_READ")
        return PinnedCatalog(self, snapshot, manifest, marker, query_embedding_contract)

    def _receipt(self, manifest, marker):
        return {"scope": deepcopy(manifest["scope"]), "catalog_version": manifest["catalog_version"],
                "vector_index_version": manifest["vector_index_version"],
                "catalog_publish_id": manifest["catalog_publish_id"],
                "activation_id": marker["activation_id"],
                "target_identity_hash": self.registry.target_identity_hash,
                "records_verified": len(manifest["record_hashes"])}


class PinnedCatalog:
    """A scoped read view for one plan; acceptance rechecks source and activation.

    Readers must call finish before accepting/caching a plan. The view cannot
    write, republish, expand scope or fall back to unstamped legacy records.
    Its receipt binds cache/state comparisons to this exact active release.
    """
    def __init__(self, publication, snapshot, manifest, marker, query_embedding_contract):
        self._publication = publication
        self._snapshot, self._manifest, self._marker = deepcopy((snapshot, manifest, marker))
        self._finished = False
        self._query_embedding_contract = query_embedding_contract
        self._value_observations = []

    @property
    def snapshot(self):
        return deepcopy(self._snapshot)

    @property
    def identity(self):
        """Read-time identity, not a completed acceptance receipt. Must finish."""
        self._check_active()
        return self._publication._receipt(self._manifest, self._marker)

    def _check_active(self):
        if self._finished:
            raise CatalogEvidenceError("CATALOG_PIN_ALREADY_FINISHED")
        if self._publication.registry.active(self._manifest["scope"]) != self._marker:
            raise CatalogEvidenceError("CATALOG_PUBLICATION_CHANGED_DURING_READ")

    def _read(self, method, *args, where=None, **kwargs):
        self._check_active()
        scoped = generation_filter(self._manifest)
        if where:
            scoped = {"$and": [scoped, deepcopy(where)]}
        rows = getattr(self._publication.store, method)(*args, where=scoped, **kwargs)
        for row in rows:
            content_hash = record_hash(_material(row))
            if content_hash != self._manifest["record_hashes"].get(row.id):
                raise CatalogEvidenceError("CATALOG_INDEX_RECORD_DRIFT")
            metadata = row.metadata
            if (metadata.get("catalog_publish_id") != self._manifest["catalog_publish_id"]
                    or metadata.get("catalog_version") != self._manifest["catalog_version"]
                    or metadata.get("catalog_scope_fingerprint") != digest(self._manifest["scope"])
                    or metadata.get("catalog_record_hash") != content_hash):
                raise CatalogEvidenceError("CATALOG_INDEX_RECORD_DRIFT")
        self._check_active()
        return rows

    def search(self, query_vector, top_k=8, where=None):
        self._check_active()
        if self._query_embedding_contract != self._manifest["embedding_contract"]:
            raise CatalogEvidenceError("CATALOG_QUERY_EMBEDDING_CONTRACT_REQUIRED")
        vector_digest(query_vector)
        if len(query_vector) != self._manifest["embedding_dimension"]:
            raise CatalogEvidenceError("CATALOG_EMBEDDING_DIMENSION_MISMATCH")
        return self._read("search", query_vector, top_k=top_k, where=where)

    def find_exact(self, where):
        return self._read("find_exact", where=where)

    def get_by_where(self, where):
        return self._read("get_by_where", where=where)

    def cache_fingerprint(self, request_fingerprint):
        self._check_active()
        return digest({"request": request_fingerprint, "scope": self._manifest["scope"],
            "target": self._manifest["target_identity_hash"], "release": self._marker})

    def entity_value_source(self, attribute_record_id, *, data_source_id=None):
        """Prove a current catalog attribute's source without reading values."""
        from catalog_value_sources import bound_field
        self._check_active()
        rows = [r for r in self.get_by_where({"type": "attribute"}) if r.id == attribute_record_id]
        if len(rows) != 1:
            raise CatalogEvidenceError("CATALOG_VALUE_ATTRIBUTE_NOT_PINNED")
        return bound_field(self._snapshot, rows[0], data_source_id=data_source_id)

    def entity_value_lookup_fields(self):
        """Implicit name lookup uses the existing governed vector/search policy.

        Display defaults do not enable search. Explicitly mentioned fields may
        still use exact lookup, including business identifiers.
        """
        from catalog_value_sources import bound_field
        from mysql_tool import entity_value_vectorization_decision
        self._check_active()
        if 'entity_value_sources' not in self._snapshot['physical_catalog']:
            return []
        eligible = []
        for row in self.get_by_where({'type': 'attribute'}):
            field = bound_field(self._snapshot, row)
            flag = field.get('vectorization')
            if flag is not None and (type(flag) not in (bool, int) or flag not in (0, 1)):
                raise CatalogEvidenceError('CATALOG_VALUE_SEARCH_POLICY_INVALID')
            policy = {**field, 'attr_name': row.metadata.get('attr_name')}
            if entity_value_vectorization_decision(policy)[0]:
                eligible.append(row.id)
        return eligible

    def lookup_entity_values(self, attribute_record_id, value, *, data_source_id=None, limit=8, require_implicit_policy=False):
        """Internal opt-in source read; values are not published catalog members.

        Call finish before using an observation. A restored observation must be
        looked up again on its current request's pin; its digest is not authority.
        Approximate legacy vector hits never bypass this exact source boundary.
        """
        from catalog_value_sources import observe
        field = self.entity_value_source(attribute_record_id, data_source_id=data_source_id)
        if require_implicit_policy and attribute_record_id not in self.entity_value_lookup_fields():
            raise CatalogEvidenceError('CATALOG_VALUE_IMPLICIT_SEARCH_NOT_GOVERNED')
        if len(self._value_observations) >= 32:
            raise CatalogEvidenceError("CATALOG_VALUE_QUERY_BUDGET_EXCEEDED")
        observation = observe(self._manifest["scope"], field, value, limit)
        self._check_active()
        self._value_observations.append((deepcopy(field), value, limit, observation["observation_hash"]))
        return {**deepcopy(observation), "catalog_pin": self.identity,
                "attribute_record_id": attribute_record_id}

    def finish(self):
        self._check_active()
        scope = self._manifest["scope"]
        if self._value_observations:
            from catalog_value_sources import observe
            for field, value, limit, expected in self._value_observations:
                if observe(scope, field, value, limit)["observation_hash"] != expected:
                    raise CatalogEvidenceError("CATALOG_ENTITY_VALUES_CHANGED_DURING_READ")
        current = self._publication.capture(scope["semantic_model_id"], scope["business_domain_ids"])
        if current["scope"] != scope or current["catalog_version"] != self._manifest["catalog_version"]:
            raise CatalogEvidenceError("CATALOG_AUTHORITY_DRIFT")
        verify_generation(current, self._manifest, self._publication.store)
        self._check_active()
        self._finished = True
        return self._publication._receipt(self._manifest, self._marker)
