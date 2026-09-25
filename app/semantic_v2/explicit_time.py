"""Program-owned current time ranges and governed metric default anchors.

Uses the existing business clock/calendar rules with full-mention consumption.
No prompt, source SQL, guessed field, watermark or history-derived default range.
Explicit field choices still go through the original binding/role guards.
"""
from datetime import datetime, time
from types import SimpleNamespace
from pydantic import TypeAdapter

from app.intent.classifier import RuleBasedIntentClassifier, _SHANGHAI_TZ
from . import models as m
from .catalog_plans import _row
from .enums import CatalogType
from .recognition_client import RecognitionFailure


def normalize_range(surface, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise RecognitionFailure('V2_CLOCK_MUST_BE_AWARE')
    try:
        span = RuleBasedIntentClassifier._time_range(surface,
            reference_date=now.astimezone(_SHANGHAI_TZ).date(), whole_expression=True)
    except (ValueError, OverflowError):
        span = None
    if span is None:
        raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_NOT_NORMALIZABLE')
    return m.TimeRange(start=datetime.combine(span.start,time.min,tzinfo=_SHANGHAI_TZ),
        end_exclusive=datetime.combine(span.end_exclusive,time.min,tzinfo=_SHANGHAI_TZ))


def metric_anchor(session, metrics, mentions):
    if not metrics:
        raise RecognitionFailure('CATALOG_METRIC_TIME_ANCHOR_REQUIRED')
    attributes = session.candidates(CatalogType.ATTRIBUTE)
    common = None
    for metric in metrics:
        if metric.catalog_type != 'METRIC' or metric.semantic_role != 'MEASURE':
            raise RecognitionFailure('V2_SLOT_ROLE_CONFLICT')
        meta = _row(session,metric)
        caliber = meta.get('time_caliber')
        if not isinstance(caliber,dict) or not isinstance(caliber.get('time_anchor'),str) or not caliber['time_anchor'].strip():
            raise RecognitionFailure('CATALOG_METRIC_TIME_ANCHOR_MISSING')
        if caliber.get('special_rule') or caliber.get('stat_cycle'):
            raise RecognitionFailure('CATALOG_METRIC_TIME_RULE_UNSUPPORTED')
        matches = {a['candidate_id'] for a in attributes
            if session._rows[a['candidate_id']].metadata.get('field_mapping') == caliber['time_anchor']
            and session._rows[a['candidate_id']].metadata.get('business_domain_id') == meta['business_domain_id']}
        if len(matches) != 1:
            raise RecognitionFailure('CATALOG_METRIC_TIME_ANCHOR_NOT_UNIQUE')
        common = matches if common is None else common & matches
    if not common or len(common) != 1:
        raise RecognitionFailure('CATALOG_METRIC_TIME_ANCHOR_CONFLICT')
    return session.bind(next(iter(common)), 'TIME_FIELD', mentions)


def normalize_grain(mentions):
    # These are calendar units, not catalog fields or business keyword guesses.
    units = {'年':'YEAR','年度':'YEAR','季度':'QUARTER','季':'QUARTER','月':'MONTH',
        '月份':'MONTH','周':'WEEK','星期':'WEEK','日':'DAY','天':'DAY'}
    values = set()
    for mention in mentions:
        text = ''.join(mention.surface.split())
        for prefix in ('按每','按','每'):
            if text.startswith(prefix): text=text[len(prefix):];break
        if text not in units:
            raise RecognitionFailure('V2_EXPLICIT_TIME_GRAIN_NOT_NORMALIZABLE')
        values.add(units[text])
    if len(values) > 1:
        raise RecognitionFailure('V2_EXPLICIT_TIME_GRAIN_CONFLICT')
    return next(iter(values),'NONE')


def normalize_combined_surface(surface, now):
    """Consume a legacy whole time mention, keeping its range/grain separate."""
    compact = ''.join(surface.split())
    pieces = compact.split('按')
    if len(pieces) > 2 or not pieces[0]:
        raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_NOT_NORMALIZABLE')
    grain = normalize_grain([SimpleNamespace(surface='按'+pieces[1])]) if len(pieces)==2 else 'NONE'
    expression = pieces[0]
    suffix = '（不含结束时刻）'
    if expression.endswith(suffix):
        bounds = expression[:-len(suffix)].split('至')
        try:
            if len(bounds) != 2:
                raise ValueError('two literal endpoints required')
            span = m.TimeRange(start=datetime.fromisoformat(bounds[0]),end_exclusive=datetime.fromisoformat(bounds[1]))
        except ValueError:
            raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_NOT_NORMALIZABLE') from None
    else:
        span = normalize_range(expression,now)
    return span,grain


def normalize_initial_assignment(session, parse, edit, value, metrics, now):
    """Return (typed value, reason) when the edit has an explicit range mention.

    Raw model dates/grain/zone are hypotheses, not authority. The caller has
    already hydrated opaque handles and rejected invented authority fields.
    A time-range mention alone does not explicitly choose a time field.
    """
    evidence = set(edit.evidence_mention_ids)
    ranges = [mention for mention in parse.mentions if mention.mention_id in evidence
        and 'TIME_RANGE' in mention.candidate_roles]
    compatibility = not ranges
    combined = []
    if not ranges:
        anchor = m.BoundSemanticRef.model_validate(value.get('anchor'))
        if value.get('comparison') is not None:
            raise RecognitionFailure('V2_TEMPORAL_COMPARISON_DEPENDENCY')
        for mention in parse.mentions:
            if mention.mention_id not in evidence or 'TIME_FIELD' not in mention.candidate_roles:
                continue
            # A literal field may accompany a separately expressed date; it
            # cannot itself prove the model-supplied interval.
            if mention.surface in {anchor.display_name, anchor.canonical_code}:
                continue
            combined.append(normalize_combined_surface(mention.surface,now))
        if not combined:
            raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_EVIDENCE_REQUIRED')
    # Do not erase additional model policy/comparison instructions to make a
    # simpler natural-calendar range pass. Those need their governed contracts.
    allowed = set(m.TimeSpec.model_fields)
    if set(value) - allowed:
        raise RecognitionFailure('V2_EXPLICIT_TIME_EXTRA_FIELD')
    if (value.get('calendar', 'NATURAL') != 'NATURAL'
            or any(value.get(key) for key in ('fiscal_calendar_id', 'calendar_policy_version',
                'default_policy_id', 'default_policy_version', 'include_incomplete_period', 'comparison'))
            or value.get('missing_period_policy', 'LEAVE_MISSING') != 'LEAVE_MISSING'
            or value.get('boundary', 'LEFT_CLOSED_RIGHT_OPEN') != 'LEFT_CLOSED_RIGHT_OPEN'):
        raise RecognitionFailure('V2_TIME_POLICY_EVIDENCE_REQUIRED')
    normalized = [span for span,_ in combined] if compatibility else [normalize_range(mention.surface,now) for mention in ranges]
    if any(span != normalized[0] for span in normalized[1:]):
        raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_CONFLICT')
    grains = [mention for mention in parse.mentions if mention.mention_id in evidence
        and 'TIME_GRAIN' in mention.candidate_roles]
    explicit_grain = normalize_grain(grains)
    chosen_grains = {grain for _,grain in combined if grain != 'NONE'}
    if grains:
        chosen_grains.add(explicit_grain)
    if len(chosen_grains)>1:
        raise RecognitionFailure('V2_EXPLICIT_TIME_GRAIN_CONFLICT')
    grain = next(iter(chosen_grains),'NONE')
    fields = [mention for mention in parse.mentions if 'TIME_FIELD' in mention.candidate_roles
        and 'TIME_RANGE' not in mention.candidate_roles]
    if any(mention.mention_id not in evidence for mention in fields):
        raise RecognitionFailure('V2_TIME_FIELD_OUTSIDE_EDIT_EVIDENCE')
    explicit_field = bool(fields)
    if explicit_field:
        anchor = m.BoundSemanticRef.model_validate(value.get('anchor'))
        if anchor.semantic_role != 'TIME_FIELD':
            raise RecognitionFailure('V2_SLOT_ROLE_CONFLICT')
        reason = ('CURRENT_LEGACY_TIME_SURFACE_NORMALIZED' if compatibility else
            'CURRENT_RANGE_NORMALIZED_WITH_EXPLICIT_TIME_FIELD')
    else:
        source_mentions = tuple(session._request.message_id+':'+mention.mention_id for mention in ranges)
        anchor = metric_anchor(session,metrics,source_mentions)
        reason = 'CURRENT_RANGE_NORMALIZED_WITH_GOVERNED_METRIC_ANCHOR'
    actual = m.TimeSpec(anchor=anchor,range=normalized[0],grain=grain,
        timezone=_SHANGHAI_TZ.key,calendar='NATURAL',source='USER_EXPLICIT',as_of=now)
    return actual.model_dump(mode='json'),reason


def normalize_component_edits(parse, edits, prior, now, hydrate):
    """Current RANGE/GRAIN evidence owns values; CLEAR/ANCHOR keep native guards.

    Some legacy surface parses label a complete time expression TIME_FIELD.
    Accept that representation only if its whole surface parses deterministically.
    A copied field name or unsupported composite phrase cannot supply dates.
    """
    normalized = []
    for edit in edits:
        if edit.operation == 'CLEAR' or edit.component == 'ANCHOR' or prior.time_spec is None:
            normalized.append(edit)
            continue
        # Even discarded model values must pass the existing authority and
        # handle/evidence checks; normalization is not an input sanitization bypass.
        hydrated = hydrate(edit.value, edit.evidence_mention_ids)
        TypeAdapter(m.TimeRange if edit.component == 'RANGE' else m.TimeGrain).validate_python(hydrated)
        evidence = set(edit.evidence_mention_ids)
        preferred = 'TIME_RANGE' if edit.component == 'RANGE' else 'TIME_GRAIN'
        mentions = [m for m in parse.mentions if m.mention_id in evidence and preferred in m.candidate_roles]
        if not mentions:
            mentions = [m for m in parse.mentions if m.mention_id in evidence and 'TIME_FIELD' in m.candidate_roles]
        if not mentions:
            raise RecognitionFailure('V2_TEMPORAL_EXPRESSION_EVIDENCE_REQUIRED')
        surfaces = []
        for mention in mentions:
            surface = ''.join(mention.surface.split())
            # Bounded edit grammar, not entity/metric keyword classification.
            # Strip only one leading assignment cue, never an embedded token.
            for prefix in ('设置为', '换成', '改为', '改成', '设为'):
                if surface.startswith(prefix):
                    surface = surface[len(prefix):]
                    break
            surfaces.append(surface)
        if edit.component == 'RANGE':
            if prior.time_spec.timezone != _SHANGHAI_TZ.key or prior.time_spec.calendar != 'NATURAL':
                raise RecognitionFailure('V2_TIME_POLICY_EVIDENCE_REQUIRED')
            ranges = [normalize_range(surface, now) for surface in surfaces]
            if any(value != ranges[0] for value in ranges[1:]):
                raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_CONFLICT')
            value = ranges[0].model_dump(mode='json')
        else:
            value = normalize_grain([SimpleNamespace(surface=surface) for surface in surfaces])
        normalized.append(edit.model_copy(update={'value': value}))
    return normalized


def normalize_comparison_edits(parse, edits, now):
    """Only current literal/calendar evidence may supply an explicit baseline."""
    result = []
    for edit in edits:
        if edit.period_rule is None or edit.period_rule.rule_type != 'EXPLICIT':
            result.append(edit)
            continue
        mentions = [m for m in parse.mentions if m.mention_id in edit.evidence_mention_ids
            and 'COMPARISON_BASELINE' in m.candidate_roles]
        if not mentions:
            raise RecognitionFailure('V2_COMPARISON_RANGE_EVIDENCE_REQUIRED')
        ranges = []
        for mention in mentions:
            surface = ''.join(mention.surface.split())
            if surface.startswith('与'):
                surface = surface[1:]
            for suffix in ('比较变化率', '比较差值', '比较'):
                if surface.endswith(suffix):
                    surface = surface[:-len(suffix)]
                    break
            span, grain = normalize_combined_surface(surface, now)
            if grain != 'NONE':
                raise RecognitionFailure('V2_COMPARISON_RANGE_GRAIN_CONFLICT')
            ranges.append(span)
        if any(span != ranges[0] for span in ranges[1:]):
            raise RecognitionFailure('V2_EXPLICIT_TIME_RANGE_CONFLICT')
        rule = edit.period_rule.model_copy(update={'baseline_range': ranges[0]})
        result.append(edit.model_copy(update={'period_rule': rule}))
    return result
