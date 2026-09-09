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
