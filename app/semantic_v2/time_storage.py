"""Explicit, versioned storage evidence for the internal bounded-time compiler.

This is an injected compiler input, never a model field or a catalog default.
An evidence digest is pinned separately by the trusted caller. TEST_ONLY input
requires an explicit opt-in and cannot certify a production field.
"""
from datetime import timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field

from .authorized_contract import AuthorizedScopeContext, contract_digest
from .models import StrictModel, Identifier, TimeRange


class TimeStorageContract(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    contract_version: Literal['bounded-datetime-storage-v1'] = 'bounded-datetime-storage-v1'
    evidence_version: Identifier
    provenance: Literal['DECLARED', 'PROVEN', 'TEST_ONLY']
    evidence_reference: Identifier
    context: AuthorizedScopeContext
    field_canonical_id: Identifier
    field_mapping: Identifier
    physical_field_id: int = Field(strict=True, gt=0)
    table_id: int = Field(strict=True, gt=0)
    data_source_id: int = Field(strict=True, gt=0)
    physical_field_digest: Identifier
    storage_timezone: str
    storage_semantics: Literal['LOCAL_WALL_DATETIME', 'UTC_DATETIME']
    fractional_seconds_precision: int = Field(strict=True, ge=0, le=6)
    applicability: TimeRange

    @property
    def fingerprint(self):
        return contract_digest(self.model_dump(mode='json'))


def bounded_time_predicates(session, plan, field, evidence, expected_digest, *, allow_test_only=False):
    """Convert instants once into declared DATETIME values; keep [start,end).

The limited path deliberately excludes DST zones, pre-2000 regional rules,
fiscal calendars, DATE and TIMESTAMP session conversions. Parameters remain
canonical strings because the existing private PyMySQL contract accepts JSON
scalars. No SQL identifier or expression comes from these values.
    """
    from .asl2 import _require
    time = plan.payload.time
    _require(isinstance(evidence, TimeStorageContract), 'ASL2_TIME_STORAGE_TIMEZONE_UNPROVEN')
    evidence = TimeStorageContract.model_validate_json(evidence.model_dump_json())
    _require(expected_digest == evidence.fingerprint, 'ASL2_TIME_EVIDENCE_VERSION_MISMATCH')
    _require(evidence.provenance != 'TEST_ONLY' or allow_test_only is True,
             'ASL2_TEST_TIME_EVIDENCE_FORBIDDEN')
    _require(evidence.context == session.context and evidence.field_canonical_id == time.anchor.canonical_id
             and evidence.field_mapping == field, 'ASL2_TIME_EVIDENCE_SCOPE_FIELD_MISMATCH')
    physical = session._pin.snapshot['physical_catalog']
    tables = [*physical.get('tables', []), *physical.get('sql_translation_sources', {}).get('tables', [])]
    fields = {contract_digest(f): f for t in tables for f in t.get('fields', [])
              if t['table_name'] + '.' + f['field_name'] == field}
    _require(len(fields) == 1 and evidence.physical_field_digest in fields,
             'ASL2_TIME_PHYSICAL_EVIDENCE_MISMATCH')
    metadata = fields[evidence.physical_field_digest]
    _require(metadata['field_id'] == evidence.physical_field_id and metadata['table_id'] == evidence.table_id
             and metadata['data_source_id'] == evidence.data_source_id
             and metadata['semantic_model_id'] == session.context.authorized_scope.semantic_model_id,
             'ASL2_TIME_PHYSICAL_EVIDENCE_MISMATCH')
    precision = evidence.fractional_seconds_precision
    _require(str(metadata['data_type']).upper() in ({'DATETIME', 'DATETIME(0)'} if precision == 0
             else {'DATETIME(' + str(precision) + ')'}), 'ASL2_TIME_PHYSICAL_TYPE_UNSUPPORTED')
    zones = {'UTC', 'Etc/UTC', 'Asia/Shanghai', 'Asia/Tokyo'}
    _require(time.timezone in zones and evidence.storage_timezone in zones
             and time.calendar == 'NATURAL', 'ASL2_TIME_CALENDAR_ZONE_UNSUPPORTED')
    _require(evidence.storage_semantics != 'UTC_DATETIME' or evidence.storage_timezone in {'UTC', 'Etc/UTC'},
             'ASL2_TIME_STORAGE_CONTRACT_INVALID')
    start, end = time.range.start, time.range.end_exclusive
    _require(evidence.applicability.start <= start < end <= evidence.applicability.end_exclusive,
             'ASL2_TIME_EVIDENCE_PERIOD_MISMATCH')
    _require(2000 <= start.astimezone(timezone.utc).year and end.astimezone(timezone.utc).year < 2100,
             'ASL2_TIME_CALENDAR_ZONE_UNSUPPORTED')
    zone = ZoneInfo(evidence.storage_timezone)
    values = []
    for instant in (start, end):
        local = instant.astimezone(zone).replace(tzinfo=None)
        _require(local.microsecond % (10 ** (6 - precision)) == 0,
                 'ASL2_TIME_PRECISION_LOSS')
        text = local.isoformat(sep=' ', timespec='microseconds' if precision else 'seconds')
        if 0 < precision < 6:
            text = text[:-(6 - precision)]
        values.append(text)
    tree = dict(operator='AND', children=[dict(field=field, operator='>=', value=values[0]),
                                          dict(field=field, operator='<', value=values[1])])
    return tree, dict(evidence_digest=evidence.fingerprint, evidence_version=evidence.evidence_version,
                     provenance=evidence.provenance, field_mapping=field,
                     boundary='LEFT_CLOSED_RIGHT_OPEN', values=values)
