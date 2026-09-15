"""Read-only, request-scoped access to the current authorized semantic catalog.

This adapter intentionally has no publication, activation, manifest, vector
refresh or process-wide pin lifecycle.  Each request captures one current
authority snapshot and exposes exact records to the existing V2 recognition
session.  The generated identity is request provenance only.
"""
from __future__ import annotations

from copy import deepcopy
import importlib
from pathlib import Path
import sys
from typing import Any


def _where_matches(metadata: dict[str, Any], where: dict[str, Any]) -> bool:
    if "$and" in where:
        values = where["$and"]
        return isinstance(values, list) and all(
            _where_matches(metadata, item) for item in values
        )
    if "$or" in where:
        values = where["$or"]
        return isinstance(values, list) and any(
            _where_matches(metadata, item) for item in values
        )
    for key, expected in where.items():
        actual = metadata.get(key)
        if isinstance(expected, dict):
            if set(expected) != {"$in"} or not isinstance(
                expected["$in"], (list, tuple, set)
            ):
                raise ValueError("CURRENT_CATALOG_FILTER_UNSUPPORTED")
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class _CurrentCatalogReadSnapshot:
    def __init__(self, *, snapshot: dict[str, Any], records, modules):
        self._snapshot = deepcopy(snapshot)
        self._modules = modules
        digest = modules["catalog_release"].digest
        version = snapshot["catalog_version"]
        self._identity = {
            "scope": deepcopy(snapshot["scope"]),
            "catalog_version": version,
            "vector_index_version": "current-catalog:" + version,
            "catalog_publish_id": "current-request:" + version,
            "activation_id": "current-request:" + digest(snapshot["scope"]),
            "target_identity_hash": snapshot["source_identity_hash"],
            "records_verified": len(records),
        }
        SearchResult = modules["vector_store"].SearchResult
        self._rows = {}
        for record in records:
            metadata = deepcopy(record.metadata)
            logical_id = record.id
            metadata.update(
                catalog_logical_id=logical_id,
                catalog_record_hash=digest(
                    {"id": logical_id, "text": record.text, "metadata": metadata}
                ),
            )
            row_id = "current:" + digest([version, logical_id])
            self._rows[row_id] = SearchResult(
                row_id, 1.0, record.text, metadata
            )
        self._finished = False
        self._value_observations: list[tuple[dict, str, int, str]] = []
        self._empty_value_queries: set[tuple[str, str]] = set()
        self._value_probes: list[tuple[dict, str, int, str]] = []
        self._value_searches: list[tuple[dict, str, int, str]] = []

    def _check(self) -> None:
        if self._finished:
            raise ValueError("CURRENT_CATALOG_SNAPSHOT_ALREADY_FINISHED")

    @property
    def identity(self) -> dict[str, Any]:
        self._check()
        return deepcopy(self._identity)

    def get_by_where(self, where: dict[str, Any]) -> list[Any]:
        self._check()
        return [
            deepcopy(row)
            for row in self._rows.values()
            if _where_matches(row.metadata, where)
        ]

    find_exact = get_by_where

    def entity_value_source(
        self, attribute_record_id: str, *, data_source_id=None
    ) -> dict[str, Any]:
        self._check()
        rows = [
            row
            for row in self.get_by_where({"type": "attribute"})
            if row.id == attribute_record_id
        ]
        if len(rows) != 1:
            raise ValueError("CURRENT_CATALOG_VALUE_ATTRIBUTE_NOT_FOUND")
        return self._modules["catalog_value_sources"].bound_field(
            self._snapshot, rows[0], data_source_id=data_source_id
        )

    def entity_value_lookup_fields(self) -> list[str]:
        self._check()
        physical = self._snapshot.get("physical_catalog", {})
        if "entity_value_sources" not in physical:
            return []
        eligible = []
        decide = self._modules["mysql_tool"].entity_value_vectorization_decision
        for row in self.get_by_where({"type": "attribute"}):
            field = self._modules["catalog_value_sources"].bound_field(
                self._snapshot, row
            )
            flag = field.get("vectorization")
            if flag is not None and (
                type(flag) not in (bool, int) or flag not in (0, 1)
            ):
                raise ValueError("CURRENT_CATALOG_VALUE_SEARCH_POLICY_INVALID")
            if decide({**field, "attr_name": row.metadata.get("attr_name")})[0]:
                eligible.append(row.id)
        return eligible

    def lookup_entity_values(
        self,
        attribute_record_id: str,
        value: str,
        *,
        data_source_id=None,
        limit: int = 8,
        require_implicit_policy: bool = False,
    ) -> dict[str, Any]:
        field = self.entity_value_source(
            attribute_record_id, data_source_id=data_source_id
        )
        if (
            require_implicit_policy
            and attribute_record_id not in self.entity_value_lookup_fields()
        ):
            raise ValueError("CURRENT_CATALOG_IMPLICIT_LOOKUP_NOT_GOVERNED")
        if len(self._value_observations) >= 32:
            raise ValueError("CURRENT_CATALOG_VALUE_QUERY_BUDGET_EXCEEDED")
        observe = self._modules["catalog_value_sources"].observe
        result = observe(self._snapshot["scope"], field, value, limit)
        self._value_observations.append(
            (deepcopy(field), value, limit, result["observation_hash"])
        )
        if result["complete"] is True and result["values"] == []:
            self._empty_value_queries.add((attribute_record_id, value))
        return {
            **deepcopy(result),
            "catalog_pin": self.identity,
            "attribute_record_id": attribute_record_id,
        }

    def probe_entity_values(
        self,
        attribute_record_id: str,
        value: str,
        *,
        data_source_id=None,
        limit: int = 64,
        require_implicit_policy: bool = False,
    ) -> dict[str, Any]:
        field = self.entity_value_source(
            attribute_record_id, data_source_id=data_source_id
        )
        if (attribute_record_id, value) not in self._empty_value_queries:
            raise ValueError("CURRENT_CATALOG_PROBE_REQUIRES_EMPTY_LOOKUP")
        if (
            require_implicit_policy
            and attribute_record_id not in self.entity_value_lookup_fields()
        ):
            raise ValueError("CURRENT_CATALOG_IMPLICIT_LOOKUP_NOT_GOVERNED")
        if len(self._value_probes) >= 3:
            raise ValueError("CURRENT_CATALOG_VALUE_PROBE_BUDGET_EXCEEDED")
        observe = self._modules["catalog_value_sources"].observe_probe
        result = observe(self._snapshot["scope"], field, value, limit)
        self._value_probes.append(
            (deepcopy(field), value, limit, result["observation_hash"])
        )
        return {
            **deepcopy(result),
            "catalog_pin": self.identity,
            "attribute_record_id": attribute_record_id,
        }

    def search_entity_values(
        self,
        attribute_record_id: str,
        value: str,
        *,
        data_source_id=None,
        limit: int = 8,
        require_implicit_policy: bool = False,
    ) -> dict[str, Any]:
        field = self.entity_value_source(
            attribute_record_id, data_source_id=data_source_id
        )
        if (attribute_record_id, value) not in self._empty_value_queries:
            raise ValueError("CURRENT_CATALOG_SEARCH_REQUIRES_EMPTY_LOOKUP")
        if (
            require_implicit_policy
            and attribute_record_id not in self.entity_value_lookup_fields()
        ):
            raise ValueError("CURRENT_CATALOG_IMPLICIT_LOOKUP_NOT_GOVERNED")
        if len(self._value_searches) >= 3:
            raise ValueError("CURRENT_CATALOG_VALUE_SEARCH_BUDGET_EXCEEDED")
        observe = self._modules["catalog_value_candidates"].observe_candidates
        result = observe(self._snapshot["scope"], field, value, limit)
        self._value_searches.append(
            (deepcopy(field), value, limit, result["observation_hash"])
        )
        return {
            **deepcopy(result),
            "catalog_pin": self.identity,
            "attribute_record_id": attribute_record_id,
        }

    def finish(self) -> dict[str, Any]:
        self._check()
        scope = self._snapshot["scope"]
        sources = self._modules["catalog_value_sources"]
        candidates = self._modules["catalog_value_candidates"]
        for field, value, limit, expected in self._value_observations:
            if sources.observe(scope, field, value, limit)["observation_hash"] != expected:
                raise ValueError("CURRENT_CATALOG_ENTITY_VALUES_CHANGED_DURING_READ")
        for field, value, limit, expected in self._value_probes:
            if sources.observe_probe(scope, field, value, limit)["observation_hash"] != expected:
                raise ValueError("CURRENT_CATALOG_ENTITY_VALUES_CHANGED_DURING_READ")
        for field, value, limit, expected in self._value_searches:
            if candidates.observe_candidates(scope, field, value, limit)["observation_hash"] != expected:
                raise ValueError("CURRENT_CATALOG_ENTITY_VALUES_CHANGED_DURING_READ")
        self._finished = True
        return deepcopy(self._identity)


class CurrentAuthorizedCatalog:
    """Capture the latest authorized catalog for each V2 context request."""

    def __init__(self, oagnet_root: Path):
        root = Path(oagnet_root).resolve()
        if not root.is_dir():
            raise RuntimeError("V2_CONTEXT_CATALOG_ROOT_MISSING")
        root_text = str(root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        names = (
            "catalog_release",
            "catalog_generation",
            "catalog_value_sources",
            "catalog_value_candidates",
            "vector_store",
            "mysql_tool",
        )
        modules = {}
        for name in names:
            module = importlib.import_module(name)
            origin = Path(module.__file__).resolve()
            if origin != (root / f"{name}.py").resolve():
                raise RuntimeError(f"V2_CONTEXT_CATALOG_MODULE_ORIGIN_MISMATCH:{name}")
            modules[name] = module
        self._modules = modules
        self.module_origins = {
            name: str(Path(module.__file__).resolve())
            for name, module in modules.items()
        }

    def for_request(self, semantic_model_id: int, business_domain_ids=()):
        requested_domains = tuple(business_domain_ids)
        domains = requested_domains
        if not domains:
            rows = self._modules["mysql_tool"].get_business_domains(
                semantic_model_id
            )
            values = [row.get("id") for row in rows]
            if (
                not values
                or any(type(value) is not int or value <= 0 for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError("CURRENT_CATALOG_MODEL_WIDE_DOMAIN_INVALID")
            domains = tuple(sorted(values))
            if len(domains) != 1:
                raise ValueError("CURRENT_CATALOG_MODEL_WIDE_MULTI_DOMAIN_UNSUPPORTED")
        snapshot = self._modules["catalog_release"].capture_catalog(
            semantic_model_id, domains
        )
        expected_scope = self._modules["catalog_release"].catalog_scope(
            semantic_model_id, domains
        )
        if snapshot.get("scope") != expected_scope:
            raise ValueError("CURRENT_CATALOG_SCOPE_MISMATCH")
        records, _coverage = self._modules[
            "catalog_generation"
        ].build_catalog_records(snapshot, lambda texts: [[0.1, 0.2] for _ in texts])
        domain_labels_by_id = {}
        for document in snapshot.get("documents") or []:
            domain = document.get("business_domain")
            if not isinstance(domain, dict):
                continue
            domain_id = domain.get("id")
            domain_name = domain.get("name")
            if (
                type(domain_id) is int
                and domain_id in domains
                and isinstance(domain_name, str)
                and domain_name.strip()
            ):
                domain_labels_by_id[domain_id] = domain_name.strip()
        return _CurrentCatalogSnapshotProvider(
            snapshot=snapshot,
            records=records,
            modules=self._modules,
            requested_business_domain_ids=requested_domains,
            resolved_business_domain_ids=domains,
            business_domain_labels=tuple(
                domain_labels_by_id[domain_id]
                for domain_id in domains
                if domain_id in domain_labels_by_id
            ),
        )

    def pin(self, semantic_model_id: int, business_domain_ids=()):
        current = self.for_request(semantic_model_id, business_domain_ids)
        return current.pin(
            semantic_model_id, current.resolved_business_domain_ids
        )


class _CurrentCatalogSnapshotProvider:
    def __init__(
        self,
        *,
        snapshot,
        records,
        modules,
        requested_business_domain_ids=(),
        resolved_business_domain_ids=(),
        business_domain_labels=(),
    ):
        self._snapshot = deepcopy(snapshot)
        self._records = deepcopy(records)
        self._modules = modules
        self.requested_business_domain_ids = tuple(requested_business_domain_ids)
        self.resolved_business_domain_ids = tuple(resolved_business_domain_ids)
        self.business_domain_labels = tuple(business_domain_labels)

    def pin(self, semantic_model_id: int, business_domain_ids=()):
        expected = self._modules["catalog_release"].catalog_scope(
            semantic_model_id, tuple(business_domain_ids)
        )
        if expected != self._snapshot["scope"]:
            raise ValueError("CURRENT_CATALOG_REQUEST_SCOPE_MISMATCH")
        return _CurrentCatalogReadSnapshot(
            snapshot=self._snapshot,
            records=self._records,
            modules=self._modules,
        )
