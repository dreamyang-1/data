"""Trusted Redis release registry using the project's existing Redis connection.

Markers have no TTL. Deployment must configure durable Redis and the same target
identity across publisher/readers. Missing/reset Redis always fails closed.
"""
from copy import deepcopy
import json
import uuid

from redis.exceptions import RedisError, WatchError

from catalog_release import CatalogEvidenceError, digest


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decode(value):
    if value is None:
        return None
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise CatalogEvidenceError("CATALOG_REGISTRY_INVALID") from None


class RedisCatalogReleaseRegistry:
    """Separate immutable manifests and atomic scope-specific activation markers."""

    def __init__(self, target_identity, client=None):
        if not isinstance(target_identity, dict) or not target_identity:
            raise CatalogEvidenceError("CATALOG_TARGET_IDENTITY_REQUIRED")
        self.target_identity_hash = digest(target_identity)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from daily_job_store import RedisDailyJobStore
            self._client = RedisDailyJobStore().client
        return self._client

    def _key(self, scope, suffix):
        # Same hash tag keeps transactions in one Redis Cluster slot.
        return "oagnet:catalog:{" + digest({"target": self.target_identity_hash, "scope": scope}) + "}:" + suffix

    def _manifest_key(self, scope, version):
        return self._key(scope, "manifest:" + version)

    def active(self, scope):
        try:
            value = _decode(self.client.get(self._key(scope, "active")))
        except (RedisError, OSError):
            raise CatalogEvidenceError("CATALOG_REGISTRY_UNAVAILABLE") from None
        if value is not None and (set(value) != {"state", "vector_index_version", "activation_id"}
                or value["state"] != "PUBLISHED" or not value["activation_id"]):
            raise CatalogEvidenceError("CATALOG_REGISTRY_INVALID")
        return value

    def reserve(self, manifest):
        """Reserve a publication exactly once before any row write; no retry upsert."""
        if manifest.get("target_identity_hash") != self.target_identity_hash:
            raise CatalogEvidenceError("CATALOG_TARGET_MISMATCH")
        scope = manifest["scope"]
        publication_key = self._key(scope, "publication:" + digest(manifest["catalog_publish_id"]))
        manifest_key = self._manifest_key(scope, manifest["vector_index_version"])
        try:
            with self.client.pipeline() as pipe:
                pipe.watch(publication_key, manifest_key)
                if pipe.get(publication_key) is not None or pipe.get(manifest_key) is not None:
                    raise CatalogEvidenceError("CATALOG_PUBLICATION_ALREADY_RESERVED")
                pipe.multi()
                pipe.set(publication_key, manifest["vector_index_version"])
                pipe.set(manifest_key, _encode({"state": "PREPARED", "manifest": manifest}))
                pipe.execute()
        except WatchError:
            raise CatalogEvidenceError("CATALOG_PUBLICATION_CONFLICT") from None
        except (RedisError, OSError):
            raise CatalogEvidenceError("CATALOG_REGISTRY_UNAVAILABLE") from None

    def manifest(self, scope, version):
        try:
            value = _decode(self.client.get(self._manifest_key(scope, version)))
        except (RedisError, OSError):
            raise CatalogEvidenceError("CATALOG_REGISTRY_UNAVAILABLE") from None
        if not value or value.get("state") != "PUBLISHED":
            raise CatalogEvidenceError("CATALOG_PUBLICATION_NOT_PROVEN")
        result = value.get("manifest", {})
        if result.get("scope") != scope or result.get("vector_index_version") != version:
            raise CatalogEvidenceError("CATALOG_REGISTRY_INVALID")
        if result.get("target_identity_hash") != self.target_identity_hash:
            raise CatalogEvidenceError("CATALOG_TARGET_MISMATCH")
        return deepcopy(result)

    def activate_verified(self, manifest, expected_active):
        """Internal publisher boundary; caller must finish full read-back first.

        PUBLISHED generations can be reactivated only by the same verified
        publisher path. A fresh activation ID rejects ABA at existing readers.
        """
        scope = manifest["scope"]
        if manifest.get("target_identity_hash") != self.target_identity_hash:
            raise CatalogEvidenceError("CATALOG_TARGET_MISMATCH")
        active_key = self._key(scope, "active")
        manifest_key = self._manifest_key(scope, manifest["vector_index_version"])
        marker = {"state": "PUBLISHED", "vector_index_version": manifest["vector_index_version"],
                  "activation_id": uuid.uuid4().hex}
        try:
            with self.client.pipeline() as pipe:
                pipe.watch(active_key, manifest_key)
                if _decode(pipe.get(active_key)) != expected_active:
                    raise CatalogEvidenceError("CATALOG_ACTIVATION_CONFLICT")
                stored = _decode(pipe.get(manifest_key))
                if not stored or stored.get("state") not in {"PREPARED", "PUBLISHED"} or stored.get("manifest") != manifest:
                    raise CatalogEvidenceError("CATALOG_PREPARATION_MISMATCH")
                pipe.multi()
                pipe.set(manifest_key, _encode({"state": "PUBLISHED", "manifest": manifest}))
                pipe.set(active_key, _encode(marker))
                pipe.execute()
        except WatchError:
            raise CatalogEvidenceError("CATALOG_ACTIVATION_CONFLICT") from None
        except (RedisError, OSError):
            # A lost acknowledgement can occur after an atomic commit. Never
            # claim the old marker is still active or blindly retry a write.
            raise CatalogEvidenceError("CATALOG_ACTIVATION_OUTCOME_UNKNOWN") from None
        return marker
