"""Trusted upstream scope extension; no role-based grants or synthetic ACLs."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from app.domain.semantic_scope import AuthorizedSemanticScope
from .models import BoundSemanticRef, Identifier, SnapshotContext, StrictModel, VersionMetadata


def contract_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class CatalogPinIdentity(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    catalog_version: Identifier
    vector_index_version: Identifier
    catalog_publish_id: Identifier
    activation_id: Identifier
    target_identity_hash: Identifier


class AuthorizedScopeContext(StrictModel):
    """Construct only from the current trusted transport at the service boundary."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    authority: Literal['TRUSTED_UPSTREAM_BACKEND'] = 'TRUSTED_UPSTREAM_BACKEND'
    authorized_scope: AuthorizedSemanticScope
    state_namespace: Identifier
    catalog_pin: CatalogPinIdentity

    def fingerprint(self):
        # Catalog generation and identity isolation augment the complete scope.
        return contract_digest(self.model_dump(mode='json'))


class CatalogBindingEvidence(StrictModel):
    """Membership receipt from a pinned catalog, not a user permission decision."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['PINNED_CATALOG'] = 'PINNED_CATALOG'
    ref: BoundSemanticRef
    record_id: Identifier
    record_hash: Identifier
    context_fingerprint: Identifier


class DatasetBindingEvidence(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['SCOPED_DATASET_STATE'] = 'SCOPED_DATASET_STATE'
    dataset_id: Identifier
    source_task_id: Identifier
    source_task_version: int = Field(ge=1)
    # DatasetState alone has no execution snapshot. A missing receipt must not
    # authorize a caller-supplied SourceDatasetRef.snapshot_id.
    snapshot_id: Identifier | None = None
    artifact_digest: Identifier
    context_fingerprint: Identifier


class TaskBindingEvidence(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['SCOPED_TASK_STATE'] = 'SCOPED_TASK_STATE'
    task_id: Identifier
    task_version: int = Field(ge=1)
    artifact_digest: Identifier
    context_fingerprint: Identifier


ArtifactKind = Literal['PENDING', 'TASK_FRAME', 'LAST_REQUEST', 'DAG_RESUME',
    'RESPONSE_CACHE', 'DATASET', 'RESULT_ARTIFACT', 'SEMANTIC_BINDINGS', 'CONVERSATION']


class ScopedArtifact(StrictModel):
    """Internal trusted-store envelope. Digest detects corruption, not provenance."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: ArtifactKind
    context: AuthorizedScopeContext
    payload: JsonValue
    payload_digest: Identifier

    @model_validator(mode='after')
    def check_integrity(self):
        from .models import freeze_contract
        if contract_digest(self.payload) != self.payload_digest:
            raise ValueError('SCOPED_ARTIFACT_CORRUPT')
        object.__setattr__(self, 'payload', freeze_contract(self.payload))
        return self


class AuthorizedVersionMetadata(VersionMetadata):
    plan_schema_version: Literal['0.2.2'] = '0.2.2'
    schema_version: Literal['0.2.2'] = '0.2.2'
    logical_plan_compiler_version: Identifier = 'authorized-logical-compiler-v1'


def validate_authorized_refs(value, snapshot: SnapshotContext, context: AuthorizedScopeContext,
                             evidence: tuple[CatalogBindingEvidence | DatasetBindingEvidence | TaskBindingEvidence, ...]):
    from .pipeline import collect_bound_refs
    scope = context.authorized_scope
    pin = context.catalog_pin
    if len(scope.business_domain_ids) > 1:
        raise ValueError('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED')
    if (snapshot.semantic_model_id != str(scope.semantic_model_id)
            or snapshot.business_domain_ids != [str(d) for d in scope.business_domain_ids]
            or snapshot.database_id != (str(scope.database_id) if scope.database_id is not None else None)
            or snapshot.knowledge_base_names != list(scope.knowledge_base_names)
            or snapshot.catalog_version != pin.catalog_version
            or snapshot.semantic_model_version != pin.catalog_version
            or snapshot.catalog_publish_id != pin.catalog_publish_id
            or snapshot.vector_index_version != pin.vector_index_version):
        raise ValueError('PLAN_VALIDATION_FAILURE: current authorized scope/pin mismatch')
    if any(not isinstance(item, (CatalogBindingEvidence, DatasetBindingEvidence, TaskBindingEvidence)) or item.context_fingerprint != context.fingerprint()
           for item in evidence):
        raise ValueError('PLAN_VALIDATION_FAILURE: foreign binding evidence')
    for ref in collect_bound_refs(value):
        # Empty owned-domain tuple represents a model-owned global record only.
        # It never changes the current explicit domain grant.
        if (ref.semantic_model_id != str(scope.semantic_model_id) or ref.catalog_version != pin.catalog_version
                or any(not d.isascii() or not d.isdecimal() or int(d) <= 0 or str(int(d)) != d for d in ref.business_domain_ids)
                or (scope.business_domain_ids and (not ref.business_domain_ids
                    or not set(ref.business_domain_ids) <= set(snapshot.business_domain_ids)))):
            raise ValueError('PLAN_VALIDATION_FAILURE: bound reference outside current scope')
        if not any(isinstance(item, CatalogBindingEvidence) and item.ref == ref for item in evidence):
            raise ValueError('PLAN_VALIDATION_FAILURE: pinned catalog membership required')
    for dataset_id in referenced_datasets(value):
        if not any(isinstance(item, DatasetBindingEvidence) and item.dataset_id == dataset_id for item in evidence):
            raise ValueError('PLAN_VALIDATION_FAILURE: scoped dataset membership required')
    from .models import SourceDatasetRef
    for source in contract_objects(value):
        if isinstance(source, SourceDatasetRef) and not any(isinstance(item, DatasetBindingEvidence)
                and item.dataset_id==source.dataset_id and item.source_task_id==source.source_task_id
                and item.source_task_version==source.source_task_version for item in evidence):
            raise ValueError('PLAN_VALIDATION_FAILURE: scoped dataset origin mismatch')
        if isinstance(source, SourceDatasetRef) and not any(isinstance(item, DatasetBindingEvidence)
                and item.dataset_id == source.dataset_id and item.source_task_id == source.source_task_id
                and item.source_task_version == source.source_task_version
                and item.snapshot_id == source.snapshot_id for item in evidence):
            raise ValueError('PLAN_VALIDATION_FAILURE: scoped dataset snapshot evidence required')
    for task_id, task_version in referenced_tasks(value):
        if not any(isinstance(item, TaskBindingEvidence) and (item.task_id, item.task_version)==(task_id, task_version) for item in evidence):
            raise ValueError('PLAN_VALIDATION_FAILURE: scoped task membership required')


def referenced_datasets(value):
    from .models import DatasetBaseline, DatasetTransformPayload, SourceDatasetRef
    if isinstance(value, DatasetTransformPayload):
        return (value.source_dataset_id, *referenced_datasets(value.operation))
    if isinstance(value, (DatasetBaseline, SourceDatasetRef)):
        return (value.dataset_id,)
    if isinstance(value, StrictModel):
        return tuple(d for name in type(value).model_fields for d in referenced_datasets(getattr(value, name)))
    if isinstance(value, dict):
        return tuple(d for item in value.values() for d in referenced_datasets(item))
    if isinstance(value, (tuple, list)):
        return tuple(d for item in value for d in referenced_datasets(item))
    return ()


def contract_objects(value):
    if isinstance(value, StrictModel):
        yield value
        for name in type(value).model_fields:
            yield from contract_objects(getattr(value,name))
    elif isinstance(value, dict):
        for item in value.values():yield from contract_objects(item)
    elif isinstance(value,(list,tuple)):
        for item in value:yield from contract_objects(item)


def referenced_tasks(value):
    from .models import TaskBaseline
    if isinstance(value, TaskBaseline):
        return ((value.task_id, value.task_version),)
    if isinstance(value, StrictModel):
        return tuple(d for name in type(value).model_fields for d in referenced_tasks(getattr(value, name)))
    if isinstance(value, dict):
        return tuple(d for item in value.values() for d in referenced_tasks(item))
    if isinstance(value, (tuple, list)):
        return tuple(d for item in value for d in referenced_tasks(item))
    return ()
