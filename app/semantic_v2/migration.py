"""Explicit 0.2 fixture migration. Unsupported data is preserved, never guessed."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter

from .models import PlanEnvelope, PlanPayload, StrictModel


class SchemaMigrationResult(StrictModel):
    source_version: str
    target_version: Literal['0.2.1'] = '0.2.1'
    status: Literal['LOSSLESS', 'LOSSY', 'UNSUPPORTED']
    migrated: JsonValue = None
    source_fixture: JsonValue
    changes: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def _migrate_payload(payload):
    result = deepcopy(payload)

    def walk(value):
        if isinstance(value, dict):
            if 'resolution_status' in value and 'catalog_type' in value:
                if value.pop('resolution_status') != 'ACCEPTED':
                    raise ValueError('unaccepted legacy reference cannot become bound')
                mention = value.pop('mention_id', None)
                value['source_mention_ids'] = [mention] if mention else []
            for child in list(value.values()):
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(result)
    projections = result.pop('projections', [])
    fields = result.pop('fields', [])
    if projections and fields and projections != fields:
        raise ValueError('legacy fields/projections conflict')
    if projections or fields:
        result['projection_spec'] = {'mode': 'EXPLICIT', 'items': [
            dict(output_field_id='projection:' + str(i), ref=ref, role=ref['semantic_role'], position=i)
            for i, ref in enumerate(projections or fields)]}
    kind = result['payload_type']
    if kind == 'REPORT':
        result['payload_type'] = 'REPORT_COMPOSITION'
    if kind == 'DATASET_TRANSFORM':
        op = result['operation']
        if op == 'LIMIT':
            limit = result.pop('limit')
            result['operation'] = dict(operation_type='LIMIT', **limit)
        elif op == 'FILTER':
            result['operation'] = dict(operation_type='FILTER', filter_expression=result.pop('filters'))
        elif op == 'PROJECT':
            result['operation'] = dict(operation_type='PROJECT', projection_spec=result.pop('projection_spec'))
        else:
            raise ValueError('legacy operation has no lossless typed migration')
        for name in ('filters', 'ranking', 'limit'):
            if result.get(name) is None:
                result.pop(name, None)
    if kind == 'LINEAGE':
        ref = result['lineage_target']
        kinds = {'METRIC': 'METRIC', 'ATTRIBUTE': 'FIELD', 'DIMENSION': 'FIELD', 'PHYSICAL_COLUMN': 'COLUMN',
                 'PHYSICAL_TABLE': 'TABLE', 'ENTITY': 'ENTITY', 'DATASET': 'DATASET', 'REPORT': 'REPORT'}
        result['lineage_target'] = dict(target_type=kinds[ref['catalog_type']], ref=ref)
    if kind in {'FORECAST', 'ANOMALY'}:
        raise ValueError('legacy algorithm/horizon requires explicit governed policy; migration cannot invent it')
    return result


class SchemaMigrationRegistry:
    """Compatibility façade for a single pure migration, not a third extensible core registry."""
    @staticmethod
    def migrate(source: dict) -> SchemaMigrationResult:
        original = deepcopy(source)
        version = source.get('schema_version', '0.2')
        try:
            if version not in {'0.2', '0.2.1'}:
                raise ValueError('unsupported source schema version')
            result = deepcopy(source)
            if version == '0.2':
                result['schema_version'] = '0.2.1'
                result['payload'] = _migrate_payload(result['payload'])
                if 'version_metadata' in result:
                    result['version_metadata']['plan_schema_version'] = '0.2.1'
            if 'plan_id' in result:
                parsed = PlanEnvelope.model_validate(result)
            else:
                parsed = TypeAdapter(PlanPayload).validate_python(result['payload'])
                result = dict(schema_version='0.2.1', payload=parsed.model_dump(mode='json'))
            if isinstance(parsed, PlanEnvelope):
                result = parsed.model_dump(mode='json')
            return SchemaMigrationResult(source_version=version, status='LOSSLESS', migrated=result,
                                         source_fixture=original, changes=['explicit 0.2 -> 0.2.1 compatibility mapping'] if version == '0.2' else [])
        except (ValueError, KeyError, TypeError) as exc:
            return SchemaMigrationResult(source_version=version, status='UNSUPPORTED',
                                         source_fixture=original, errors=[str(exc)])
