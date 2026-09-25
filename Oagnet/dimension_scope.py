"""Publish explicit dimension projections from governed ownership, never names.

The shared index stays model-wide. Old snapshots without entity/attribute IDs
cannot prove ownership and need a controlled republish; there is no fallback to
shared retrieval. Complex hierarchy/rule mappings require a separate contract.
"""
from copy import deepcopy
from uuid import UUID


def normalize_governed_id(value):
    """Recognize governed catalog keys without granting model/domain access.

    Current entity/attribute PKs are VARCHAR: UUIDs (36/32 chars) and decimal
    legacy IDs. Preserve UUID spelling/case exactly; matching different forms
    would invent an alias not established by the captured owner rows.
    """
    if type(value) is int and value > 0:
        # Source VARCHAR keys are bounded; avoid unbounded integer conversion.
        return str(value) if value < 10**128 else None
    if isinstance(value, str) and len(value) > 128:
        return None
    if isinstance(value, str) and len(value) in (32, 36):
        try:
            identity = UUID(value)
        except ValueError:
            return None
        exact_form = identity.hex if len(value) == 32 else str(identity)
        return value if identity.int > 0 and value.lower() == exact_form else None
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
        return str(int(value))
    return None


def project_dimension_to_domain(dimension, document):
    domain = (document.get('business_domain') or {}).get('id')
    if type(domain) is not int or domain <= 0:
        return None
    # Do not carry opaque physical expressions across the domain boundary.
    if any(dimension.get(key) for key in ('field_mapping', 'special_rules', 'dim_hierarchy')):
        return None
    owners = {}
    for entity in document.get('entities') or []:
        ident = normalize_governed_id(entity.get('entity_id'))
        if ident is None or entity.get('business_domain') != domain:
            continue
        if ident in owners:
            return None
        attributes = {}
        for attribute in entity.get('attributes') or []:
            attr_id = normalize_governed_id(attribute.get('attribute_id'))
            if attr_id is None:
                continue
            if attr_id in attributes:
                return None
            attributes[attr_id] = attribute
        owners[ident] = (entity, attributes)
    bindings = dimension.get('bind_entities') or []
    if not isinstance(bindings, list):
        return None
    kept = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        entity, attribute = normalize_governed_id(binding.get('entity')), normalize_governed_id(binding.get('attr'))
        if entity not in owners or attribute not in owners[entity][1]:
            continue
        claimed_domain = binding.get('businessDomain')
        if claimed_domain is not None and normalize_governed_id(claimed_domain) != str(domain):
            continue
        # Denormalized binding mappings can be stale. Rehydrate only from the
        # entity/attribute rows whose ownership was just proven.
        owner, attributes = owners[entity]
        attr = attributes[attribute]
        mapping = str(attr.get('field_mapping') or '').split('.')
        if len(mapping) != 2 or not all(p.isascii() and p.isidentifier() for p in mapping):
            continue
        kept.append({'entity': entity, 'attr': attribute, 'businessDomain': str(domain),
            'entityName': owner.get('entity_name'), 'attrName': attr.get('attr_name'),
            'mappingTable': mapping[0], 'mappingColumn': mapping[1]})
    if not kept:
        return None
    result = deepcopy(dimension)
    result['bind_entities'] = kept
    result['bind_metrics'] = []
    return result
