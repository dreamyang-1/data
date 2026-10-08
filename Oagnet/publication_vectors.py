"""Bounded, ownership-aware vector reuse for the existing publication paths.

No global text cache, new collections, query-time rebuilding or API changes.
Legacy records without an embedding signature are regenerated once.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from numbers import Real
import os
from time import monotonic

from config import BASE_URL, EMBEDDING_DIM, EMBEDDING_MODEL
from logger import logger


SIGNATURE_KEY = "publication_embedding_signature"
_OWNER_KEYS = (
    "type", "semantic_model_id", "business_domain_id", "data_source_id",
    "entity_code", "entity_id", "parent", "attr_code", "attribute_id",
    "field_mapping", "table_id", "source_table", "source_field",
)


@contextmanager
def publication_stage(name, *, model, domain=None):
    """Log durations and failures without logging credentials or business values."""
    started = monotonic()
    outcome = "SUCCEEDED"
    try:
        yield
    except BaseException:
        outcome = "FAILED"
        raise
    finally:
        logger.info("publication stage=%s model=%s domain=%s status=%s elapsed_ms=%s",
                    name, model, domain, outcome, round((monotonic() - started) * 1000))


def _signature():
    # Include endpoint and dimension, not credentials. Change the format version
    # here when the embedding protocol changes; provider/model changes invalidate
    # prior vectors automatically.
    return hashlib.sha256(json.dumps(
        ["publication-v1", BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM,
         os.getenv("OAGNET_EMBEDDING_REVISION", "1")],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _valid_vector(vector, dimension):
    try:
        return len(vector) > 0 and (dimension is None or len(vector) == dimension) and all(
            isinstance(value, Real) and not isinstance(value, bool)
            and math.isfinite(float(value)) for value in vector)
    except (TypeError, ValueError, OverflowError):
        return False


def _same_owner(left, right):
    return all(left.get(key) == right.get(key) for key in _OWNER_KEYS)


class PublicationVectors:
    """Per-publication reuse; only bounded inventories are held while preparing."""

    def __init__(self, store, *, signature=None, dimension=None, batch_size=256):
        self.store = store
        self.signature = signature or _signature()
        self.dimension = dimension if dimension is not None else getattr(store, "embedding_dim", None)
        if batch_size <= 0:
            raise ValueError("publication batch_size must be positive")
        self.batch_size = batch_size
        self.prepared = {}
        self.unchanged_ids = set()
        self.embedded = 0
        self.reused = 0

    def _previous(self, batch):
        inventory = getattr(self.store, "get_catalog_inventory", None)
        if not callable(inventory):
            return {}  # Compatibility stores regenerate, never reuse guesses.
        # IDs are not sufficient for physical records: copied models can share
        # datasource-prefixed IDs. Always filter by the current model as well.
        models = sorted({r.metadata["semantic_model_id"] for r in batch})
        kinds = sorted({r.metadata["type"] for r in batch})
        where = {"$and": [
            {"record_id": {"$in": [r.id for r in batch]}},
            {"semantic_model_id": {"$in": models}},
            {"type": {"$in": kinds}},
        ]}
        if all("business_domain_id" in r.metadata for r in batch):
            where["$and"].append({"business_domain_id": {"$in": sorted(
                {r.metadata["business_domain_id"] for r in batch})}})
        rows = inventory(where)
        result, duplicates = {}, set()
        for row in rows:
            if row.id in result:
                duplicates.add(row.id)
            result[row.id] = row
        return {key: row for key, row in result.items() if key not in duplicates}

    def assign(self, records, embed_fn):
        for start in range(0, len(records), self.batch_size):
            batch = records[start:start + self.batch_size]
            previous = self._previous(batch)
            missing = []
            for record in batch:
                record.metadata[SIGNATURE_KEY] = self.signature
                old = self.prepared.get(record.id) or previous.get(record.id)
                if (self.dimension is not None and old is not None and old.text == record.text
                        and _same_owner(old.metadata, record.metadata)
                        and old.metadata.get(SIGNATURE_KEY) == self.signature
                        and _valid_vector(old.vector, self.dimension)):
                    record.vector = list(old.vector)
                    self.reused += 1
                else:
                    missing.append(record)
                stored = previous.get(record.id)
                if (record.vector and stored is not None
                        and stored.text == record.text and stored.metadata == record.metadata
                        and list(stored.vector) == record.vector):
                    self.unchanged_ids.add(record.id)
                else:
                    self.unchanged_ids.discard(record.id)
            if missing:
                vectors = embed_fn([r.text for r in missing])
                if len(vectors) != len(missing):
                    raise RuntimeError("Embedding 返回数量不一致")
                for record, vector in zip(missing, vectors):
                    if not _valid_vector(vector, self.dimension):
                        raise ValueError("PUBLICATION_EMBEDDING_INVALID: dimension or values differ")
                    record.vector = list(vector)
                self.embedded += len(missing)
            self.prepared.update((r.id, r) for r in batch)
        logger.info("publication vectors records=%s embedded=%s reused=%s unchanged=%s",
                    len(records), self.embedded, self.reused, len(self.unchanged_ids))
        return records

    def write(self, records):
        changed = [r for r in records if r.id not in self.unchanged_ids]
        self.store.add(changed)
        logger.info("publication write records=%s written=%s unchanged=%s",
                    len(records), len(changed), len(records) - len(changed))


def assign_vectors(records, embed_fn, vectorizer=None):
    if vectorizer is not None:
        return vectorizer.assign(records, embed_fn)
    vectors = embed_fn([r.text for r in records]) if records else []
    if len(vectors) != len(records):
        raise RuntimeError(f"Embedding 返回数量不一致: expected={len(records)}, actual={len(vectors)}")
    for record, vector in zip(records, vectors):
        record.vector = vector
    return records
