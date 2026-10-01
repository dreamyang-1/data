"""Governed display and single declared relationship construction over a pin.

No live database reads, model-generated policies or inferred relationship keys.
"""
from typing import Literal

from pydantic import Field, model_validator

from . import models as m
from .authorized_contract import contract_digest
from .enums import CatalogType
from .recognition_client import RecognitionFailure
from .slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint


class RelationshipHopDraft(m.StrictModel):
    binding_handle: m.Identifier
    direction: Literal['FORWARD', 'REVERSE'] = 'FORWARD'


class RelationshipEditDraft(m.StrictModel):
    operation: Literal['SET', 'REPLACE', 'CLEAR']
    binding_handle: m.Identifier | None = None
    direction: Literal['FORWARD', 'REVERSE'] = 'FORWARD'
    evidence_mention_ids: list[m.Identifier] = Field(min_length=1, max_length=50)
    hops: list[RelationshipHopDraft] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def exclusive_path(self):
        if self.hops and (self.binding_handle is not None or self.direction != 'FORWARD' or self.operation == 'CLEAR'):
            raise ValueError('path hops cannot mix with a single edge or CLEAR')
        return self


def cardinality(value):
    # Same declared spellings as SQL Translator's _canonical_cardinality,
    # plus its canonical output tokens. No data statistics or name heuristics.
    key = str(value or '').strip().upper().replace(' ', '').replace('-', '').replace('_', '')
    return {'1:1': 'ONE_TO_ONE', 'ONETOONE': 'ONE_TO_ONE',
        '1:N': 'ONE_TO_MANY', '1:M': 'ONE_TO_MANY', '1:*': 'ONE_TO_MANY', 'ONETOMANY': 'ONE_TO_MANY',
        'N:1': 'MANY_TO_ONE', 'M:1': 'MANY_TO_ONE', '*:1': 'MANY_TO_ONE', 'MANYTOONE': 'MANY_TO_ONE',
        'N:M': 'MANY_TO_MANY', 'M:N': 'MANY_TO_MANY', '*:*': 'MANY_TO_MANY', 'MANYTOMANY': 'MANY_TO_MANY'}.get(key)


def _row(session, ref):
    proof = session._require_refs([ref])[0]
    return session._rows[proof.record_id].metadata


def _entity(session, code, domain, role, mentions):
    candidates = session.candidates(CatalogType.ENTITY)
    matching = [c for c in candidates if c['canonical_code'] == code
        and session._rows[c['candidate_id']].metadata['business_domain_id'] == domain]
    if len(matching) != 1:
        raise RecognitionFailure('CATALOG_RELATION_ENDPOINT_UNRESOLVED')
    return session.bind(matching[0]['candidate_id'], role, mentions)


def relationship(session, relation_ref, direction='FORWARD'):
    meta = _row(session, relation_ref)
    if relation_ref.catalog_type != 'RELATION' or relation_ref.semantic_role != 'RELATIONSHIP':
        raise RecognitionFailure('V2_RELATION_BINDING_ROLE_CONFLICT')
    declared = cardinality(meta.get('relation_type'))
    if declared is None:
        raise RecognitionFailure('CATALOG_RELATION_CARDINALITY_MISSING')
    join = meta.get('join_key')
    if not isinstance(join, dict) or not all(isinstance(join.get(k), str) and join[k].strip()
            for k in ('source_field', 'target_field')):
        raise RecognitionFailure('CATALOG_RELATION_JOIN_EVIDENCE_MISSING')
    source, target = meta.get('parent'), meta.get('target_entity')
    if not source or not target:
        raise RecognitionFailure('CATALOG_RELATION_ENDPOINT_UNRESOLVED')
    if direction == 'REVERSE':
        source, target = target, source
        declared = {'ONE_TO_MANY': 'MANY_TO_ONE', 'MANY_TO_ONE': 'ONE_TO_MANY'}.get(declared, declared)
    elif direction != 'FORWARD':
        raise RecognitionFailure('V2_RELATION_DIRECTION_INVALID')
    return m.RelationshipSpec(relation_ref=relation_ref,
        source_ref=_entity(session, source, meta['business_domain_id'], 'SOURCE_ENTITY', relation_ref.source_mention_ids),
        target_ref=_entity(session, target, meta['business_domain_id'], 'TARGET_ENTITY', relation_ref.source_mention_ids),
        cardinality=declared)


def same_entity(left, right):
    return left is not None and right is not None and (left.canonical_id, left.catalog_type,
        left.catalog_version, left.semantic_model_id, tuple(left.business_domain_ids)) == (
        right.canonical_id, right.catalog_type, right.catalog_version, right.semantic_model_id, tuple(right.business_domain_ids))


def default_projection(session, entity):
    if entity is None or entity.catalog_type != 'ENTITY':
        raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_ENTITY_REQUIRED')
    meta = _row(session, entity)
    owned = meta.get('attributes')
    if not isinstance(owned, list):
        raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_MISSING')
    declared = [a for a in owned if isinstance(a, dict) and a.get('is_main_attribute') is True]
    if not declared:
        raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_MISSING')
    if len({a.get('attr_code') for a in declared}) != len(declared):
        raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_CONFLICT')
    candidates = session.candidates(CatalogType.ATTRIBUTE)
    items, hashes = [], []
    for attribute in sorted(declared, key=lambda a: str(a.get('attr_code') or '')):
        matching = []
        for c in candidates:
            row = session._rows[c['candidate_id']].metadata
            if row.get('parent') == meta['entity_code'] and row['business_domain_id'] == meta['business_domain_id'] and row.get('attr_code') == attribute.get('attr_code'):
                matching.append((c, row))
        if len(matching) != 1:
            raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_BINDING_MISSING')
        candidate, row = matching[0]
        if (row.get('attribute_id') != attribute.get('attribute_id') or row.get('is_main_attribute') is not True
                or not isinstance(row.get('field_mapping'), str) or not row['field_mapping'].strip()
                or row.get('field_mapping') != attribute.get('field_mapping')):
            raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_CONFLICT')
        ref = session.bind(candidate['candidate_id'], 'PROJECTION_FIELD', entity.source_mention_ids)
        items.append(m.ProjectionItem(output_field_id='projection:' + ref.canonical_id,
            ref=ref, role='PROJECTION_FIELD', position=len(items)))
        hashes.append(row['catalog_record_hash'])
    policy = 'catalog-display:' + contract_digest(['catalog-main-attributes-v1',
        session.context.catalog_pin.model_dump(mode='json'), meta['catalog_record_hash'], hashes])
    return m.ProjectionSpec(mode='SEMANTIC_DEFAULT', items=items, default_display_policy_id=policy)


def validate_catalog_payload(session, payload):
    """Compile-time receipt checks also cover restored/default policy claims."""
    from .catalog_paths import validate_path, validate_occurrence_fields, path_projection
    validate_occurrence_fields(session, payload)
    if payload.payload_type == 'RELATION_LIST':
        spec = payload.relationship_spec
        if spec is None:
            raise RecognitionFailure('CATALOG_RELATIONSHIP_REQUIRED')
        if isinstance(spec, m.RelationshipPathSpec):
            validate_path(session, spec)
        else:
            variants = [relationship(session, spec.relation_ref, direction) for direction in ('FORWARD', 'REVERSE')]
            if same_entity(spec.source_ref, spec.target_ref):
                raise RecognitionFailure('CATALOG_SELF_RELATION_REQUIRES_PATH')
            if not any(semantic_fingerprint(spec) == semantic_fingerprint(v) for v in variants):
                raise RecognitionFailure('CATALOG_RELATIONSHIP_MISMATCH')
        if (not same_entity(payload.source_entity, spec.source_ref) or not same_entity(payload.target_entity, spec.target_ref)
                or payload.relation_target is not None):
            raise RecognitionFailure('CATALOG_RELATIONSHIP_ENDPOINT_MISMATCH')
    if payload.payload_type in {'DETAIL_ROWS', 'RELATION_LIST'}:
        from .pipeline import collect_bound_refs
        endpoints = [payload.source_entity]
        if payload.payload_type == 'RELATION_LIST':
            endpoints = ([n.entity_ref for n in payload.relationship_spec.nodes]
                if isinstance(payload.relationship_spec, m.RelationshipPathSpec) else [*endpoints, payload.target_entity])
        owners = {(_row(session, e).get('entity_code'), _row(session, e)['business_domain_id']) for e in endpoints}
        for ref in collect_bound_refs([payload.filters, payload.projection_spec]):
            if ref.catalog_type == 'ATTRIBUTE':
                meta = _row(session, ref)
                if (meta.get('parent'), meta['business_domain_id']) not in owners:
                    raise RecognitionFailure('CATALOG_QUERY_ATTRIBUTE_OUTSIDE_DECLARED_ENTITIES')
    projection = getattr(payload, 'projection_spec', None)
    if projection and projection.mode == 'SEMANTIC_DEFAULT':
        entity = payload.target_entity if payload.payload_type == 'RELATION_LIST' else getattr(payload, 'source_entity', None)
        expected = default_projection(session, entity)
        if isinstance(getattr(payload, 'relationship_spec', None), m.RelationshipPathSpec):
            expected = path_projection(expected, payload.relationship_spec)
        if semantic_fingerprint(projection) != semantic_fingerprint(expected):
            raise RecognitionFailure('CATALOG_DEFAULT_DISPLAY_POLICY_MISMATCH')


def complete_catalog_defaults(session, kind, prior, patch, barriers=()):
    reduced = apply_task_patch(prior, patch, clear_barriers=barriers)
    state = reduced.semantics
    if kind not in {'DETAIL_ROWS', 'RELATION_LIST'}:
        return patch, reduced
    if kind == 'RELATION_LIST':
        if state.relationship_spec is None:
            raise RecognitionFailure('CATALOG_RELATIONSHIP_REQUIRED')
        if state.subject and not same_entity(state.subject, state.relationship_spec.source_ref):
            raise RecognitionFailure('V2_RELATION_SUBJECT_CONFLICT')
        entity = state.relationship_spec.target_ref
    else:
        entity = state.subject
    if state.projection_spec.items and state.projection_spec.mode == 'EXPLICIT':
        return patch, reduced
    if 'projection_spec' in reduced.clear_barriers:
        raise RecognitionFailure('V2_PROJECTION_EXPLICITLY_CLEARED')
    projection = default_projection(session, entity)
    if kind == 'RELATION_LIST' and isinstance(state.relationship_spec, m.RelationshipPathSpec):
        from .catalog_paths import path_projection
        projection = path_projection(projection, state.relationship_spec)
    if semantic_fingerprint(projection) == semantic_fingerprint(state.projection_spec):
        return patch, reduced
    operations = [o for phase in ('clears', 'removes', 'replacements', 'sets', 'adds', 'inherit_requests') for o in getattr(patch, phase)]
    operations.append(m.SlotOperation(operation_id='catalog:display', slot_path='projection_spec', operation='SET',
        new_value=projection.model_dump(mode='json'), source='CURRENT_REFERENCE_RESOLUTION', reason_code='PINNED_CATALOG_MAIN_ATTRIBUTES',
        base_task_version=patch.base_task_version, presence='PRESENT', evidence_mention_ids=[]))
    compiled = TaskPatch.compile(operations, base_task_version=patch.base_task_version)
    patch = TaskPatch.model_validate(dict(compiled.model_dump(), reset=patch.reset))
    return patch, apply_task_patch(prior, patch, clear_barriers=barriers)
