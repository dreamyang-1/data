"""Publish explicit dimension projections from governed ownership, never names.

The shared index stays model-wide. Old snapshots without entity/attribute IDs
cannot prove ownership and need a controlled republish; there is no fallback to
shared retrieval. Complex hierarchy/rule mappings require a separate contract.
"""
from copy import deepcopy


def _catalog_id(value):
    # Catalog JSON stores BIGINT references as decimal strings. This is catalog
    # identity normalization, not the stricter public request-ID parser.
    if type(value) is int and value > 0:
        return str(value)
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
        ident = _catalog_id(entity.get('entity_id'))
        if ident is None or entity.get('business_domain') != domain:
            continue
        if ident in owners:
            return None
        attributes = {}
        for attribute in entity.get('attributes') or []:
            attr_id = _catalog_id(attribute.get('attribute_id'))
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
        entity, attribute = _catalog_id(binding.get('entity')), _catalog_id(binding.get('attr'))
        if entity not in owners or attribute not in owners[entity][1]:
            continue
        claimed_domain = binding.get('businessDomain')
        if claimed_domain is not None and _catalog_id(claimed_domain) != str(domain):
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
