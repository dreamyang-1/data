"""Current-source values carried separately from static catalog membership."""
from copy import deepcopy

from .authorized_contract import (CatalogPinIdentity, SourceValueBindingEvidence,
    contract_digest, source_value_id)
from .enums import CatalogType
from .models import BoundSemanticRef


def lookup_values(session, attribute_id, text, *, implicit=False):
    session._check()
    row = session._rows.get(attribute_id)
    if row is None or row.metadata.get('type') != 'attribute':
        raise ValueError('V2_SOURCE_VALUE_ATTRIBUTE_NOT_OFFERED')
    scope = session.context.authorized_scope
    data_source = None
    if scope.database_id is not None:
        from app.tools.database_load import build_database_load_tool
        data_source = int(build_database_load_tool().load(scope.database_id).data_source_id)
    expected_field = session._pin.entity_value_source(attribute_id, data_source_id=data_source)
    key = (attribute_id, text, implicit)
    receipt = session._source_observations.get(key)
    if receipt is None:
        receipt = session._pin.lookup_entity_values(attribute_id, text,
            data_source_id=data_source, require_implicit_policy=implicit)
    expected_scope = dict(semantic_model_id=scope.semantic_model_id,
        business_domain_ids=list(scope.business_domain_ids), scope_mode=scope.scope_mode)
    field = receipt.get('field', {})
    if (receipt.get('source') != 'VERIFIED_SOURCE_EXACT_LOOKUP'
            or receipt.get('match_mode') != 'EXACT_NORMALIZED'
            or receipt.get('scope') != expected_scope
            or receipt.get('catalog_pin', {}).get('scope') != expected_scope
            or receipt.get('attribute_record_id') != attribute_id
            or field != expected_field
            or any(receipt.get('catalog_pin', {}).get(k) != getattr(session.context.catalog_pin, k)
                   for k in CatalogPinIdentity.model_fields)
            or field.get('semantic_model_id') != scope.semantic_model_id
            or field.get('business_domain_id') != row.metadata.get('business_domain_id')
            or field.get('entity_code') != row.metadata.get('parent')
            or field.get('attr_code') != row.metadata.get('attr_code')
            or f"{field.get('mapping_table')}.{field.get('mapping_column')}" != row.metadata.get('field_mapping')
            or (data_source is not None and field.get('data_source_id') != data_source)):
        raise ValueError('V2_SOURCE_VALUE_RECEIPT_MISMATCH')
    if field.get('mapping_hash') != contract_digest({k: v for k, v in field.items() if k != 'mapping_hash'}):
        raise ValueError('V2_SOURCE_VALUE_MAPPING_HASH_MISMATCH')
    material = {k: receipt.get(k) for k in ('source','scope','field','match_mode','query_hash','values','complete')}
    if receipt.get('observation_hash') != contract_digest(material):
        raise ValueError('V2_SOURCE_VALUE_OBSERVATION_HASH_MISMATCH')
    if receipt.get('complete') is not True:
        raise ValueError('V2_SOURCE_VALUE_CANDIDATES_INCOMPLETE')
    values = receipt.get('values')
    if (not isinstance(values, list) or len(values) > 8
            or any(not isinstance(v, str) or not 1 <= len(v) <= 500 for v in values)
            or len(values) != len(set(values))):
        raise ValueError('V2_SOURCE_VALUE_CANDIDATES_INVALID')
    field_ref = session.bind(attribute_id, 'FILTER_FIELD')
    # Only within this live pin. finish reobserves the source; no cross-turn
    # cache can turn these candidates into current authority.
    for surface in [text, *values]:
        session._source_observations[(attribute_id, surface, implicit)] = deepcopy(receipt)
    result = []
    for value in values:
        canonical = source_value_id(field_ref, field['mapping_hash'], value, field['data_source_id'])
        candidate_id = 'source-candidate:' + contract_digest([session.context.fingerprint(), canonical])
        session._value_candidates[candidate_id] = dict(attribute_id=attribute_id,
            canonical_value=value, field=deepcopy(field), receipt=deepcopy(receipt))
        result.append(dict(candidate_id=candidate_id, catalog_type='ENTITY_VALUE',
            canonical_code=canonical, display_name=value, supported_roles=['FILTER_VALUE']))
    session._check()
    return result


def bind_value(session, candidate_id, role, mentions=()):
    if role != 'FILTER_VALUE':
        raise ValueError('V2_SOURCE_VALUE_ROLE_INVALID')
    candidate = session._value_candidates[candidate_id]
    field_ref = session.bind(candidate['attribute_id'], 'FILTER_FIELD', mentions)
    canonical = source_value_id(field_ref, candidate['field']['mapping_hash'], candidate['canonical_value'], candidate['field']['data_source_id'])
    ref = BoundSemanticRef(catalog_type='ENTITY_VALUE', semantic_role='FILTER_VALUE',
        canonical_id=canonical, canonical_code=canonical, display_name=candidate['canonical_value'],
        catalog_version=field_ref.catalog_version, semantic_model_id=field_ref.semantic_model_id,
        business_domain_ids=field_ref.business_domain_ids, source_mention_ids=tuple(mentions),
        resolution_source='VERIFIED_SOURCE_EXACT_LOOKUP')
    if 'removal_proof' in candidate:
        # This is an exact identifier for removing an existing scoped filter
        # member. It is not evidence that the value still exists in the source.
        old = candidate['removal_proof']
        archived = SourceValueBindingEvidence.model_validate(dict(old.model_dump(mode='json'),
            ref=ref.model_dump(mode='json'), field_ref=field_ref.model_dump(mode='json')))
        session._archived_source_bindings[contract_digest(ref.model_dump(mode='json'))] = archived
        return ref
    proof = SourceValueBindingEvidence(ref=ref, field_ref=field_ref,
        attribute_record_id=candidate['attribute_id'], canonical_value=candidate['canonical_value'],
        mapping_hash=candidate['field']['mapping_hash'], data_source_id=candidate['field']['data_source_id'],
        observation_hash=candidate['receipt']['observation_hash'], observed_at=candidate['receipt']['observed_at'],
        context_fingerprint=session.context.fingerprint())
    session._bindings[contract_digest(ref.model_dump(mode='json'))] = proof
    return ref


def restore_values(session, proofs, *, defer=False):
    candidates = session.candidates(CatalogType.ATTRIBUTE) if proofs else []
    for old in proofs:
        if old.context_fingerprint != session.context.fingerprint():
            raise ValueError('V2_SOURCE_VALUE_RESTORE_SCOPE_MISMATCH')
        candidate = next((c for c in candidates if c['candidate_id'] == old.attribute_record_id), None)
        if candidate is None:
            raise ValueError('V2_SOURCE_VALUE_RESTORE_FIELD_MISSING')
        field = session.bind(candidate['candidate_id'], 'FILTER_FIELD', old.field_ref.source_mention_ids)
        if field != old.field_ref:
            raise ValueError('V2_SOURCE_VALUE_RESTORE_FIELD_MISMATCH')
        if defer:
            session._archived_source_bindings[contract_digest(old.ref.model_dump(mode='json'))] = old
            continue
        offered = lookup_values(session, candidate['candidate_id'], old.canonical_value)
        match = next((c for c in offered if c['display_name'] == old.canonical_value), None)
        if match is None or session.bind(match['candidate_id'], 'FILTER_VALUE', old.ref.source_mention_ids) != old.ref:
            raise ValueError('V2_SOURCE_VALUE_RESTORE_NOT_CURRENT')


def source_proofs_for(session, refs):
    result = []
    seen = set()
    for ref in refs:
        key = contract_digest(ref.model_dump(mode='json'))
        proof = session._bindings.get(key) or session._archived_source_bindings.get(key)
        if isinstance(proof, SourceValueBindingEvidence) and key not in seen:
            result.append(proof)
            seen.add(key)
    return tuple(result)
