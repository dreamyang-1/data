"""Explicit comparison dependencies; calendar facts never come from history text."""
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from . import models as m
from .authorized_contract import contract_digest
from .recognition_client import RecognitionFailure
from .slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint


class ComparisonEditDraft(m.StrictModel):
    operation: Literal['SET', 'REPLACE', 'CLEAR']
    period_rule: m.ComparisonPeriodRule | None = None
    calculation: Literal['ABS_DIFF', 'GROWTH_RATE'] | None = None
    evidence_mention_ids: list[m.Identifier] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def explicit_change(self):
        if self.operation == 'CLEAR':
            if self.period_rule is not None or self.calculation is not None:
                raise ValueError('comparison CLEAR cannot carry a rule or calculation')
        elif self.period_rule is None or self.calculation is None:
            raise ValueError('comparison assignment requires rule and calculation')
        return self


def _localize(value, zone):
    """Reject nonexistent/ambiguous local boundaries; do not choose DST policy."""
    naive = value.replace(tzinfo=None)
    candidates = {naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        for fold in (0, 1) if naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive}
    if len(candidates) != 1:
        raise RecognitionFailure('V2_COMPARISON_LOCAL_TIME_POLICY_REQUIRED')
    return next(iter(candidates))


def _shift(value, zone, *, months=0, delta=None):
    local = value.astimezone(zone).replace(tzinfo=None)
    try:
        if months:
            total = local.year * 12 + local.month - 1 + months
            local = local.replace(year=total // 12, month=total % 12 + 1)
        if delta is not None:
            local += delta
    except (ValueError, OverflowError) as exc:
        raise RecognitionFailure('V2_COMPARISON_CALENDAR_MAPPING_UNDEFINED') from exc
    return _localize(local, zone)


def _baseline(time, rule):
    if time is None or time.range is None:
        raise RecognitionFailure('V2_COMPARISON_BOUNDED_TIME_REQUIRED')
    if time.comparison is not None:
        raise RecognitionFailure('V2_COMPARISON_DUPLICATE_TIME_AUTHORITY')
    if time.calendar != 'NATURAL':
        raise RecognitionFailure('V2_COMPARISON_CALENDAR_POLICY_UNSUPPORTED')
    current = time.range; zone = ZoneInfo(time.timezone)
    if rule.rule_type == 'EXPLICIT':
        return rule.baseline_range, 'CUSTOM_PERIOD'
    if rule.rule_type == 'PREVIOUS_YEAR':
        if time.grain == 'WEEK':
            raise RecognitionFailure('V2_COMPARISON_WEEK_YEAR_POLICY_REQUIRED')
        months, delta, kind = -12, None, 'YOY'
    else:
        unit = time.grain.value if rule.period_unit == 'QUERY_GRAIN' else rule.period_unit
        if unit == 'NONE':
            raise RecognitionFailure('V2_COMPARISON_PERIOD_UNIT_REQUIRED')
        if time.grain != 'NONE' and unit != time.grain:
            raise RecognitionFailure('V2_COMPARISON_PERIOD_GRAIN_CONFLICT')
        months, delta = 0, None
        if unit == 'CURRENT_WINDOW':
            start, end = (v.astimezone(zone).replace(tzinfo=None) for v in (current.start, current.end_exclusive))
            # Whole calendar months retain month boundaries despite unequal month lengths.
            if start.day == end.day == 1 and start.time() == end.time():
                months = -((end.year - start.year) * 12 + end.month - start.month)
            else:
                delta = start - end
            # This endpoint is already an unambiguous instant supplied by the request.
            return m.TimeRange(start=_shift(current.start, zone, months=months, delta=delta),
                end_exclusive=current.start), 'PERIOD_OVER_PERIOD'
        elif unit in {'MONTH', 'QUARTER', 'YEAR'}:
            months = -{'MONTH': 1, 'QUARTER': 3, 'YEAR': 12}[unit]
        elif unit in {'DAY', 'WEEK'}:
            delta = -timedelta(days=1 if unit == 'DAY' else 7)
        else:
            raise RecognitionFailure('V2_COMPARISON_PERIOD_UNIT_REQUIRED')
        kind = 'MOM' if unit == 'MONTH' else 'PERIOD_OVER_PERIOD'
    return m.TimeRange(start=_shift(current.start, zone, months=months, delta=delta),
        end_exclusive=_shift(current.end_exclusive, zone, months=months, delta=delta)), kind


def build_comparison(time, measures, rule, calculation):
    if calculation not in {'ABS_DIFF', 'GROWTH_RATE'}:
        raise RecognitionFailure('V2_COMPARISON_CALCULATION_UNSUPPORTED')
    if any(r.catalog_type != 'METRIC' or r.semantic_role != 'MEASURE' for r in measures):
        raise RecognitionFailure('V2_COMPARISON_MEASURE_ROLE_CONFLICT')
    baseline, kind = _baseline(time, rule)
    return m.TemporalComparisonSpec(period_rule=rule, comparison_type=kind,
        baseline=m.TimeBaseline(range=baseline), current_period=time.range, comparison_period=baseline,
        calculation=calculation, output_metrics=measures)


def complete_comparison_patch(prior, patch, edits=()):
    """Recompute dependent values in the same atomic TaskPatch as their sources."""
    state = apply_task_patch(prior, patch).semantics
    if edits and edits[0].operation == 'CLEAR':
        value, operation, source, reason = None, 'CLEAR', 'CURRENT_EXPLICIT', 'CURRENT_COMPARISON_CLEAR'
    elif edits or isinstance(state.comparison_spec, m.TemporalComparisonSpec):
        request = edits[0] if edits else state.comparison_spec
        value = build_comparison(state.time_spec, state.metrics, request.period_rule, request.calculation)
        if not edits and semantic_fingerprint(value) == semantic_fingerprint(state.comparison_spec):
            return patch
        operation = edits[0].operation if edits else 'REPLACE'
        source = 'CURRENT_EXPLICIT' if edits else 'CURRENT_REFERENCE_RESOLUTION'
        reason = 'CURRENT_COMPARISON_RULE' if edits else 'RECOMPUTED_COMPARISON_DEPENDENCIES'
    else:
        return patch
    operations = [o for phase in ('clears','removes','replacements','sets','adds','inherit_requests') for o in getattr(patch, phase)]
    evidence = edits[0].evidence_mention_ids if edits else sorted({i for o in operations for i in o.evidence_mention_ids})
    operations.append(m.SlotOperation(operation_id='comparison:dependency', slot_path='comparison_spec', operation=operation,
        new_value=value.model_dump(mode='json') if value else None, source=source, reason_code=reason,
        base_task_version=patch.base_task_version, presence='PRESENT' if value else 'EXPLICITLY_CLEARED', evidence_mention_ids=evidence))
    compiled = TaskPatch.compile(operations, base_task_version=patch.base_task_version)
    return TaskPatch.model_validate(dict(compiled.model_dump(), reset=patch.reset))


def validate_temporal_payload(payload):
    if payload.payload_type != 'COMPARISON':
        return
    spec = payload.comparison
    if not isinstance(spec, m.TemporalComparisonSpec):
        if isinstance(spec.baseline, m.TimeBaseline):
            raise RecognitionFailure('V2_COMPARISON_PERIOD_RULE_REQUIRED')
        return
    expected = build_comparison(payload.time, payload.measures, spec.period_rule, spec.calculation)
    if semantic_fingerprint(spec) != semantic_fingerprint(expected):
        raise RecognitionFailure('V2_COMPARISON_DERIVATION_MISMATCH')


def comparison_outputs(spec):
    outputs = []
    for ref in spec.output_metrics:
        for role, label in [('CURRENT','本期'), ('BASELINE','基期'), ('DERIVED','变化值' if spec.calculation == 'ABS_DIFF' else '变化率')]:
            outputs.append(m.TemporalOutputFieldRequirement(
                output_field_id='comparison:' + contract_digest([ref.canonical_id, role]), semantic_ref=ref,
                logical_role='MEASURE', expected_data_type='DECIMAL', display_label=ref.display_name + '（' + label + '）',
                comparison_role=role, calculation=spec.calculation))
    return outputs


def comparison_arithmetic(outputs, bindings, rows):
    groups = {}
    for output in outputs:
        if isinstance(output, m.TemporalOutputFieldRequirement):
            key = semantic_fingerprint(output.semantic_ref)
            group = groups.setdefault(key, {})
            if output.comparison_role in group or output.output_field_id not in bindings:
                return False
            group[output.comparison_role] = output
    def number(value):
        if value is None: return None
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)): raise ValueError('non numeric')
        result = Decimal(str(value))
        if not result.is_finite(): raise ValueError('non finite')
        return result
    try:
        for group in groups.values():
            if set(group) != {'CURRENT','BASELINE','DERIVED'} or len({o.calculation for o in group.values()}) != 1:
                return False
            for row in rows:
                current, baseline, actual = [number(row[bindings[group[role].output_field_id]]) for role in ('CURRENT','BASELINE','DERIVED')]
                expected = None
                if current is not None and baseline is not None:
                    if group['DERIVED'].calculation == 'ABS_DIFF': expected = current - baseline
                    elif baseline != 0: expected = (current - baseline) / abs(baseline)
                if expected is None:
                    if actual is not None: return False
                elif actual is None or abs(actual - expected) > max(Decimal('1e-9'), abs(expected) * Decimal('1e-7')):
                    return False
    except (ArithmeticError, ValueError, KeyError, TypeError):
        return False
    return bool(groups)
