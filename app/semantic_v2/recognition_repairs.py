"""Lossless representation repair of model facts before strict V2 validation.

Only exact current-turn evidence can relocate a span or resolve a surface token
to an already declared negated/temporal mention. No roles, edits, scope, history or business
facts are added. The frozen CurrentTurnParser still validates the final result.
"""
from __future__ import annotations

from .pipeline import CurrentTurnSemanticParse


def repair_model_parse(parsed: CurrentTurnSemanticParse, *, text: str, turn_id: str):
    repaired = parsed.model_copy(deep=True)
    trace = []
    # A foreign-turn fact must never be made current by finding its text here.
    if any(m.source_turn_id != turn_id for m in repaired.mentions):
        return repaired, trace
    for mention in repaired.mentions:
        if text[mention.start_char:mention.end_char] == mention.surface and mention.end_char <= len(text):
            continue
        first = text.find(mention.surface)
        if first < 0 or text.find(mention.surface, first + 1) >= 0:
            continue
        old = (mention.start_char, mention.end_char)
        mention.start_char, mention.end_char = first, first + len(mention.surface)
        trace.append({'reason_code': 'EXACT_UNIQUE_CURRENT_SURFACE_SPAN',
            'mention_id': mention.mention_id, 'field': 'mention.span',
            'before': list(old), 'after': [mention.start_char, mention.end_char]})
    ids = {m.mention_id for m in repaired.mentions}
    for index, reference in enumerate(repaired.negations):
        if reference in ids:
            continue
        start = text.find(reference)
        if start < 0 or text.find(reference, start + 1) >= 0:
            continue
        end = start + len(reference)
        candidates = [m for m in repaired.mentions if m.negated
            and m.start_char <= start < end <= m.end_char
            and text[m.start_char:m.end_char] == m.surface]
        if len(candidates) != 1:
            continue
        mention = candidates[0]
        repaired.negations[index] = mention.mention_id
        trace.append({'reason_code': 'EXACT_TOKEN_IN_UNIQUE_DECLARED_NEGATED_MENTION',
            'mention_id': mention.mention_id, 'field': f'negations[{index}]'})
    for index, reference in enumerate(repaired.temporal_expressions):
        if reference in ids:
            continue
        start = text.find(reference)
        if start < 0 or text.find(reference, start + 1) >= 0:
            continue
        end = start + len(reference)
        candidates = [m for m in repaired.mentions
            if set(m.candidate_roles) & {'TIME_RANGE', 'TIME_GRAIN', 'TIME_FIELD', 'COMPARISON_BASELINE'}
            and m.start_char <= start < end <= m.end_char
            and text[m.start_char:m.end_char] == m.surface]
        if len(candidates) != 1:
            continue
        mention = candidates[0]
        repaired.temporal_expressions[index] = mention.mention_id
        trace.append({'reason_code': 'EXACT_TOKEN_IN_UNIQUE_DECLARED_TEMPORAL_MENTION',
            'mention_id': mention.mention_id, 'field': f'temporal_expressions[{index}]'})
    return repaired, trace


def repair_pure_historical_reference(parsed: CurrentTurnSemanticParse, *, context_trace, task_context):
    """Remove a task-description-only parse after the target is validated.

    The context model remains responsible for RETURN_TO_TOPIC and target
    selection.  This repair applies only when every current mention is an
    unmodified description already present in that exact, scope-checked task.
    Any operation, negation, temporal edit signal, new semantic value or mixed
    current requirement preserves the original parse and its strict guards.
    """
    if (context_trace.get('FINAL_RELATION') != 'RETURN_TO_TOPIC'
            or not context_trace.get('FINAL_TARGET')
            or not set(parsed.dialogue_act_candidates) <= {'RETURN_TO_TOPIC'}
            or parsed.operation_markers or parsed.followup_signals or parsed.topic_shift_signals
            or parsed.negations or parsed.coordination_groups or parsed.temporal_expressions
            or not parsed.mentions):
        return parsed, []
    explicit = {mention_id for values in parsed.explicit_slot_mentions.values() for mention_id in values}
    mention_ids = {mention.mention_id for mention in parsed.mentions}
    if explicit != mention_ids:
        return parsed, []
    target = next((task for task in task_context.get('candidate_tasks', [])
        if task.get('task_id') == context_trace['FINAL_TARGET']), None)
    if target is None:
        return parsed, []

    labels = set()
    def collect_names(value):
        if isinstance(value, dict):
            name = value.get('name')
            if isinstance(name, str) and name.strip():
                labels.add(name.strip().casefold())
            for child in value.values():
                collect_names(child)
        elif isinstance(value, list):
            for child in value:
                collect_names(child)
    for field in ('metrics', 'dimensions', 'subject', 'filters', 'time'):
        collect_names(target.get(field))

    def is_existing_description(mention):
        forms = {value.strip().casefold() for value in
            (mention.surface, mention.normalized_surface) if value and value.strip()}
        return any(form == label or (len(form) >= 2 and form in label)
            for form in forms for label in labels)
    if not labels or not all(is_existing_description(mention) for mention in parsed.mentions):
        return parsed, []

    repaired = parsed.model_copy(deep=True)
    removed = sorted(mention_ids)
    repaired.mentions = []
    repaired.explicit_slot_mentions = {}
    return repaired, [{
        'reason_code': 'VALIDATED_HISTORICAL_TASK_DESCRIPTION_ONLY',
        'field': 'mentions',
        'target_task_id': context_trace['FINAL_TARGET'],
        'removed_mention_ids': removed,
    }]


def repair_collection_handle_mentions(draft, *, parse, handles, candidates):
    """Retag an already selected identity only with unique exact current evidence.

    Catalog handles encode candidate, role and mention. A model can select the
    correct candidate but copy its handle offered for another current mention.
    Only metrics/dimensions with one exact governed display-name or synonym
    mention and one matching offered identity are repaired. No fuzzy or
    normalized-name inference, new identity, role, evidence, operation or scope
    is introduced. All later guards still run. Ambiguity and repeated mentions
    stay rejected.
    """
    repaired = draft.model_copy(deep=True)
    offered = {c['binding_handle']: c for c in candidates}
    trace = []
    def supports_surface(candidate, surface):
        aliases = candidate.get('aliases', [])
        if isinstance(aliases, str):
            aliases = [aliases]
        return surface == candidate.get('name') or surface in aliases

    for index, edit in enumerate(repaired.edits):
        role = {'metrics': 'MEASURE', 'dimensions': 'GROUP_BY'}.get(edit.slot_path)
        if role is None or edit.operation == 'CLEAR':
            continue
        values = edit.value if isinstance(edit.value, list) else [edit.value]
        for position, value in enumerate(values):
            if not isinstance(value, dict) or set(value) != {'binding_handle'}:
                continue
            old_handle = value['binding_handle']
            chosen = offered.get(old_handle)
            if chosen is None or old_handle not in handles or chosen['role'] != role:
                continue
            if chosen['mention_id'] in edit.evidence_mention_ids:
                continue
            mentions = [m for m in parse.mentions
                if supports_surface(chosen, m.surface) and role in m.candidate_roles]
            if len(mentions) != 1:
                continue
            mention = mentions[0]
            if mention.mention_id not in edit.evidence_mention_ids or mention.mention_id in draft.unresolved_mention_ids:
                continue
            matching = [c for c in candidates if c['mention_id'] == mention.mention_id
                and c['role'] == role and supports_surface(c, mention.surface)
                and c['binding_handle'] in handles]
            identities = {handles[c['binding_handle']][0] for c in matching}
            old_identity = handles[old_handle][0]
            if identities != {old_identity} or len(matching) != 1:
                continue
            new_handle = matching[0]['binding_handle']
            if handles[new_handle] != (old_identity, handles[old_handle][1], mention.mention_id):
                continue
            value['binding_handle'] = new_handle
            trace.append({'reason_code': 'EXACT_UNIQUE_COLLECTION_MENTION_HANDLE',
                'field': f'edits[{index}].value[{position}]',
                'from_mention_id': chosen['mention_id'], 'to_mention_id': mention.mention_id})
    return repaired, trace
