"""Catalog-backed parameter ownership; neither names nor vector rank are IDs."""
import json
import re
import unicodedata


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def normalized(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def entity_terms(code, meta):
    aliases = decoded(meta.get('entity_alias')) or []
    if isinstance(aliases, str):
        aliases = re.split('[,，;；]', aliases)
    return [code, meta.get('entity_name'), str(meta.get('entity_id') or ''),
            *(aliases if isinstance(aliases, list) else [])]


def resolve_entities(label, entities):
    """Prefer canonical code/ID; an alias shared by two entities is not unique."""
    labels = label if isinstance(label, list) else [label]
    owners = set()
    for value in labels:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return set()
        term = normalized(value)
        if not term:
            return set()
        exact = {code for code, meta in entities.items()
                 if term in {normalized(code), normalized(meta.get('entity_id'))}}
        matches = exact or {code for code, meta in entities.items()
                            if term in {normalized(v) for v in entity_terms(code, meta) if v}}
        if len(matches) != 1:
            return set()
        owners.update(matches)
    return owners


def field_owners(meta):
    owners = set(meta.get('owners') or [])
    owners.update(str(meta[key]) for key in ('owner', 'parent') if meta.get(key))
    return owners


def parameter_owners(original, catalog):
    if not isinstance(original, dict) or not original.get('entity'):
        return set()
    return resolve_entities(original['entity'], catalog['entities'])


def matching_fields(original, catalog):
    label = original.get('name') or original.get('field') if isinstance(original, dict) else original
    if not isinstance(label, str):
        return []
    matched = []
    for field, meta in catalog['fields'].items():
        terms = [field]
        for attr in meta.get('attribute_bindings') or [meta]:
            terms.extend([attr.get('attr_code'), attr.get('attr_name')])
            for owner in field_owners(attr):
                for entity in entity_terms(owner, catalog['entities'].get(owner, {})):
                    if entity and attr.get('attr_name'):
                        terms.extend([str(entity) + str(attr['attr_name']), str(entity) + '.' + str(attr['attr_name'])])
        if normalized(label) in {normalized(term) for term in terms if term}:
            matched.append(field)
    return matched


def parameter_catalog(catalog, original, extraction, target):
    """Explicit slot owners constrain attributes, not the entire query subject.

    Top-level involved entities are only tie-breaking context. Unowned/wrongly
    classified values keep existing bounded cross-field recovery. A shared
    dictionary can still bind through a published edge to the requested owner.
    Global value IDs remain stable; value matching also checks retained fields.
    """
    if target == 'metrics':
        return catalog
    owners = parameter_owners(original, catalog)
    exact = matching_fields(original, catalog)
    if not owners and not exact and target in {'filters', 'display_fields'} and isinstance(original, dict):
        # A whole entity name/alias used as a slot label supplies its parent,
        # not an arbitrary same-named attribute on another entity. Explicit
        # attribute names and explicit slot owners retain their old priority.
        label = normalized(original.get('field') or original.get('name') or '')
        if label:
            entities = catalog['entities']
            canonical = {code for code, meta in entities.items()
                         if label in {normalized(code), normalized(meta.get('entity_id'))}}
            matches = canonical or {code for code, meta in entities.items()
                if label in {normalized(term) for term in entity_terms(code, meta) if term}}
            if len(matches) > 1:
                return dict(catalog, _parameter_owner_candidates=sorted(matches))
            owners = matches
    if not owners and len(exact) > 1:
        hinted = resolve_entities(extraction.get('实体') or [], catalog['entities'])
        matched_owners = {owner for field in exact for owner in field_owners(catalog['fields'][field])}
        if len(hinted & matched_owners) == 1:
            owners = hinted & matched_owners
    if not owners:
        return catalog
    fields = {}
    for key, meta in catalog['fields'].items():
        if not field_owners(meta) & owners:
            continue
        bindings = [attr for attr in meta.get('attribute_bindings') or [] if field_owners(attr) & owners]
        fields[key] = dict(meta, **bindings[0], attribute_bindings=bindings) if len(bindings) == 1 else meta
    direct = [key for key in exact if key in fields]
    # A textual own attribute (e.g. dealer.name/level) cannot be replaced by a
    # same-named attribute on a neighbor. FK dictionary labels are different:
    # a province name can be bound through dealer.province_id, not hospital's.
    identifiers = all(key.rsplit('.', 1)[-1].casefold().endswith(('_id', '_code', '_key')) for key in direct)
    if target == 'filters' and (not direct or identifiers):
        fields.update({key: catalog['fields'][key] for key, candidates in catalog.get('_dictionary_owners', {}).items()
                       if candidates & owners and key in catalog['fields']})
    result = dict(catalog, fields=fields, _parameter_owners=owners)
    return result


def value_allowed(record, catalog):
    """Keep stable global IDs while checking the selected logical parent."""
    owners = catalog.get('_parameter_owners') or set()
    declared = (catalog.get('_value_owners') or {}).get(record.get('id')) or set()
    dictionary = (catalog.get('_dictionary_owners') or {}).get(record.get('field')) or set()
    return record.get('field') in catalog['fields'] and (not owners or not declared or bool(owners & (declared | dictionary)))
