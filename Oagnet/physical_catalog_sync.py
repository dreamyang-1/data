"""Publish registered physical metadata through the existing model rebuild.

This module neither reads business rows nor rebuilds indexes during a query.
Data sources and enum mappings come only from the current SQL catalog scope.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import math

from mysql_tool import get_registered_entity_attributes, get_table_field_by_scope
from scope_contract import normalize_domains, require_model_id
from vector_store import PHYSICAL_RECORD_TYPES, build_records_from_tables


class PhysicalCatalogPublicationError(Exception):
    """A failed write/read-back must not be reported as a successful publish."""


@dataclass
class PhysicalCatalogPlan:
    model: int
    domain: int | None
    records: list
    table_keys: set
    attribute_enums: dict
    vectorizer: object = None


def _attribute_enums(values):
    """Accept the usual JSON list/mapping forms without inventing any value."""
    if isinstance(values, dict):
        if any(key in values for key in ('code', 'value', 'name', 'label')):
            values = [values]
        else:
            values = [{'code': code, 'name': label} for code, label in values.items()]
    if not isinstance(values, list):
        return []
    return [deepcopy(value) if isinstance(value, dict) else {'value': value, 'name': str(value)}
            for value in values if isinstance(value, (dict, str, int, float, bool))]


def prepare_physical_catalog(model, domain, embed_fn, *, vectorizer=None):
    """Build the entire scoped snapshot before touching any vector record."""
    require_model_id(model)
    if domain is not None:
        normalize_domains(business_domain_ids=[domain])
    doc = get_table_field_by_scope(semantic_model_id=model, business_domain_id=domain)
    by_source, field_enums, table_keys = {}, {}, set()
    for table in doc.get('tables') or []:
        # The legacy loader can include unowned (NULL-model) registrations.
        # They are not sufficient authority for publishing a model's index.
        if table.get('semantic_model_id') != model:
            continue
        ds = table.get('data_source_id')
        name = table.get('table_name')
        if type(ds) is not int or ds <= 0 or not isinstance(name, str) or not name:
            raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: table ownership is incomplete')
        if (ds, name) in table_keys:
            raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: duplicate table registration')
        table_keys.add((ds, name))
        for field in table.get('fields') or []:
            if (field.get('semantic_model_id') not in (None, model)
                    or field.get('data_source_id') != ds
                    or field.get('table_id') != table.get('table_id')):
                raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: field ownership differs from its table')
            column = field.get('field_name')
            if not isinstance(column, str) or not column or (ds, f'{name}.{column}') in field_enums:
                raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: missing or duplicate field name')
            field_enums[(ds, f'{name}.{column}')] = deepcopy(field.get('enum_values') or [])
        by_source.setdefault(ds, []).append(table)

    records = []
    for ds, tables in sorted(by_source.items()):
        records.extend(build_records_from_tables(
            {'scope': {'semantic_model_id': model, 'data_source_id': ds}, 'tables': tables}, embed_fn,
            vectorizer=vectorizer))
    if any(not record.vector or any(not math.isfinite(float(value)) for value in record.vector)
           for record in records):
        raise ValueError('PHYSICAL_CATALOG_EMBEDDING_INVALID: empty or non-finite embedding')

    enums = {}
    for attr in get_registered_entity_attributes(model, domain):
        bd = attr.get('business_domain_id')
        if domain is not None and bd != domain:
            raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: attribute is outside the requested domain')
        source = (attr.get('data_source_id'), attr.get('field_mapping'))
        if source not in field_enums:
            continue
        key = (bd, attr.get('entity_code'), attr.get('attr_code'), attr.get('field_mapping'))
        values = _attribute_enums(field_enums[source])
        if key in enums and enums[key] != values:
            raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: ambiguous attribute data source')
        enums[key] = deepcopy(values)
    return PhysicalCatalogPlan(model, domain, records, table_keys, enums, vectorizer)


def publish_physical_catalog(store, plan):
    """Upsert, verify the stored snapshot, then remove scoped obsolete records."""
    scope = {'$and': [
        {'type': {'$in': sorted(PHYSICAL_RECORD_TYPES)}}, {'semantic_model_id': plan.model},
    ]}
    previous = store.get_by_where(scope)
    if plan.domain is not None:
        # Physical IDs are shared by domains using the same table. An explicit
        # domain publish cannot delete other domains' registered tables.
        previous = [record for record in previous
                    if (record.metadata.get('data_source_id'),
                        record.metadata.get('table_name') if record.metadata.get('type') == 'table'
                        else record.metadata.get('parent')) in plan.table_keys]
    current = {record.id: record for record in plan.records}
    if current:
        identity_filter = {'$and': [
            {'type': {'$in': sorted(PHYSICAL_RECORD_TYPES)}}, {'record_id': {'$in': sorted(current)}},
        ]}
        # Existing IDs encode the data source, not model. Never overwrite a
        # foreign model in the unlikely event of inconsistent registrations.
        if any(record.metadata.get('semantic_model_id') != plan.model
               for record in store.get_by_where(identity_filter)):
            raise ValueError('PHYSICAL_CATALOG_SOURCE_INVALID: data-source index belongs to another model')
        if plan.vectorizer is not None:
            plan.vectorizer.write(plan.records)
        else:
            store.add(plan.records)
        inventory = getattr(store, 'get_catalog_inventory', None)
        readback = inventory(identity_filter) if callable(inventory) else store.get_by_where(identity_filter)
        actual = {record.id: record for record in readback}
        if len(actual) != len(readback) or set(actual) != set(current):
            raise PhysicalCatalogPublicationError('PHYSICAL_CATALOG_READBACK_FAILED: record set differs')
        for key, expected in current.items():
            record = actual[key]
            if record.text != expected.text or record.metadata != expected.metadata:
                raise PhysicalCatalogPublicationError('PHYSICAL_CATALOG_READBACK_FAILED: stored content differs')
            if callable(inventory) and (len(record.vector) != len(expected.vector)
                    or not record.vector or any(not math.isfinite(float(v)) for v in record.vector)):
                raise PhysicalCatalogPublicationError('PHYSICAL_CATALOG_READBACK_FAILED: stored embedding is invalid')
    stale = sorted({record.id for record in previous} - set(current))
    if stale:
        store.delete_by_ids(stale)
    return {'total': len(current), 'by_type': dict(Counter(r.metadata['type'] for r in plan.records)),
            'deleted': len(stale)}
