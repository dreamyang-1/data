"""Synthetic UUID/decimal catalog IDs; no private catalog rows or live clients."""
from copy import deepcopy

import pytest

from catalog_generation import build_catalog_records
from catalog_release import CatalogEvidenceError, catalog_scope
from dimension_scope import project_dimension_to_domain
from test_catalog_publication import authority, embed, publish, reseal, system

ENTITY = '12345678-1234-4567-89ab-123456789abc'
ATTRIBUTE = 'abcdef0123456789abcdef0123456789'


def uuid_snapshot(entity_id=ENTITY, attribute_id=ATTRIBUTE):
    snapshot = authority()
    doc = snapshot['documents'][0]
    doc['entities'][0]['entity_id'] = entity_id
    doc['entities'][0]['attributes'][0]['attribute_id'] = attribute_id
    doc['dimensions'][0]['bind_entities'] = [
        {'entity': entity_id, 'attr': attribute_id, 'businessDomain': '205'}]
    return reseal(snapshot)


@pytest.mark.parametrize('entity_id,attribute_id', [
    (ENTITY, ATTRIBUTE), (ENTITY, '1234567890123456789'),
    (ENTITY.upper(), ATTRIBUTE.upper()),
    ('00000000000000000000000000000001', '00000000000000000000000000000002'),
    (205, 1205), ('00205', '01205'),
])
def test_governed_identity_generation_and_pinned_acceptance(entity_id, attribute_id):
    snapshot = uuid_snapshot(entity_id, attribute_id)
    original = deepcopy(snapshot)
    rows, coverage = build_catalog_records(snapshot, embed)
    dimension = next(r for r in rows if r.metadata['type'] == 'scoped_dimension')
    assert dimension.metadata['business_domain_id'] == 205
    assert len(dimension.metadata['bind_entities']) == 1
    assert coverage['explicit_exclusions'].get('NO_OWNERSHIP_PROVEN_DIMENSION_PROJECTION', 0) == 0
    assert not any(r.metadata['business_domain_id'] != 205 for r in rows)
    service, _, _, _, _, overrides = system()
    overrides[(81, (205,))] = snapshot
    receipt = publish(service)
    view = service.pin(81, [205])
    assert len(view.get_by_where({'type': 'scoped_dimension'})) == 1
    assert view.finish() == receipt
    assert snapshot == original


@pytest.mark.parametrize('fault', ['foreign_entity', 'foreign_attribute', 'foreign_domain',
    'different_case', 'different_uuid_spelling', 'duplicate_entity', 'duplicate_attribute',
    'opaque_mapping', 'opaque_rules'])
def test_uuid_cannot_substitute_ownership_evidence(fault):
    snapshot = uuid_snapshot()
    doc = snapshot['documents'][0]
    entity, dimension = doc['entities'][0], doc['dimensions'][0]
    binding = dimension['bind_entities'][0]
    if fault == 'foreign_entity': binding['entity'] = '87654321-4321-4567-89ab-123456789abc'
    if fault == 'foreign_attribute': binding['attr'] = 'bcdefa0123456789abcdef0123456789'
    if fault == 'foreign_domain': binding['businessDomain'] = '206'
    if fault == 'different_case': binding['entity'] = ENTITY.upper()
    if fault == 'different_uuid_spelling': binding['entity'] = ENTITY.replace('-', '')
    if fault == 'duplicate_entity': doc['entities'].append(deepcopy(entity))
    if fault == 'duplicate_attribute': entity['attributes'].append(deepcopy(entity['attributes'][0]))
    if fault == 'opaque_mapping': dimension['field_mapping'] = {'field': 'foreign.secret'}
    if fault == 'opaque_rules': dimension['special_rules'] = ['foreign.secret']
    assert project_dimension_to_domain(dimension, doc) is None


@pytest.mark.parametrize('invalid', [None, True, False, 0, -1, 1.5, '', '0', '-1',
    ' 123 ', '１２３', 'not-governed', '{'+ENTITY+'}', ENTITY+' ',
    '00000000-0000-0000-0000-000000000000', 'g'*32,
    '123456781234-4567-89ab-123456789abc', '1'*129, 10**128])
@pytest.mark.parametrize('field', ['entity_id', 'attribute_id'])
def test_invalid_catalog_identity_cannot_generate_or_project(invalid, field):
    snapshot = uuid_snapshot(**{field: invalid})
    doc = snapshot['documents'][0]
    assert project_dimension_to_domain(doc['dimensions'][0], doc) is None
    with pytest.raises(CatalogEvidenceError, match='CATALOG_GOVERNED_ID_REQUIRED'):
        build_catalog_records(snapshot, embed)


@pytest.mark.parametrize('invalid', [ENTITY, ATTRIBUTE, '81', True, 0])
def test_governed_uuid_support_does_not_relax_request_scope(invalid):
    with pytest.raises(CatalogEvidenceError, match='REQUEST_SCOPE_INVALID'):
        catalog_scope(invalid, [205])
    with pytest.raises(CatalogEvidenceError, match='REQUEST_SCOPE_INVALID'):
        catalog_scope(81, [invalid])
