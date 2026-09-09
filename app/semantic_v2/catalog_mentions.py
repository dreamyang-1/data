"""Recover an exact catalog metric span before offering semantic candidates.

This is catalog-backed boundary resolution, separate from model representation
repair. It never binds an identity, guesses aliases or broadens the current pin.
Ambiguous full terms and independent/overlapping semantic evidence are retained.
"""
from .catalog_bridge import RECORD_TYPES
from .enums import CatalogType
from .pending_recognition import governed_aliases
from .pipeline import CurrentTurnParser


def recover_metric_spans(session, parsed, *, text):
    CurrentTurnParser.parse(text=text, turn_id=session._request.message_id,
        text_ref=session._request.message_id, parsed=parsed)
    metrics = session.candidates(CatalogType.METRIC)
    names = {}
    for row in metrics:
        name = row['display_name']
        if isinstance(name, str) and name:
            names.setdefault(name, []).append(row['candidate_id'])
    # Fetch other role families only when the current parse might split a term.
    proposals = []
    for name, records in names.items():
        start = text.find(name)
        if len(records) != 1 or start < 0 or text.find(name, start + 1) >= 0:
            continue
        end = start + len(name)
        parts = sorted((m for m in parsed.mentions if m.start_char < end and start < m.end_char),
            key=lambda m: m.start_char)
        if (len(parts) < 2 or parts[0].start_char != start or parts[-1].end_char != end
                or any(a.end_char != b.start_char for a, b in zip(parts, parts[1:]))):
            continue
        measures = [m for m in parts if set(m.candidate_roles) == {'MEASURE'}]
        if len(measures) != 1:
            continue
        measure = measures[0]
        absorbed = [m for m in parts if m is not measure]
        if any(not set(m.candidate_roles) <= {'SUBJECT_ENTITY', 'SOURCE_ENTITY'} for m in absorbed):
            continue
        if (any(not m.explicit or m.modifier_ids for m in parts)
                or len({(m.clause_id, m.coordination_group_id, m.negated) for m in parts}) != 1):
            continue
        ids = {m.mention_id for m in parts}
        if ids & set(parsed.negations) and not measure.negated:
            continue
        removed = ids - {measure.mention_id}
        if (any(ids & set(group) for group in parsed.coordination_groups)
                or ids & set(parsed.temporal_expressions)
                or any(ids & set(m.modifier_ids) for m in parsed.mentions)):
            continue
        if any(i in removed and slot != 'subject' or i == measure.mention_id and slot != 'metrics'
                for slot, mentions in parsed.explicit_slot_mentions.items() for i in mentions):
            continue
        relevant = [m for m in parsed.operation_markers if m.mention_id in ids]
        if any(m.mention_id in removed and (m.slot_name != 'subject' or m.operation_hint != 'SET')
                or m.mention_id == measure.mention_id and m.slot_name != 'metrics' for m in relevant):
            continue
        if len({m.operation_hint for m in relevant if m.mention_id == measure.mention_id}) > 1:
            continue
        if measure.mention_id not in parsed.explicit_slot_mentions.get('metrics', []):
            continue
        proposals.append((name, records[0], parts, measure, removed))
    if not proposals:
        return parsed, []
    other_terms = set()
    for kind in dict.fromkeys(definition[0] for definition in RECORD_TYPES.values()):
        for row in metrics if kind == CatalogType.METRIC else session.candidates(kind):
            meta = session._rows[row['candidate_id']].metadata
            terms = {row['display_name'], row['canonical_code'], *governed_aliases(meta)}
            for name, record, *_ in proposals:
                if row['candidate_id'] != record and name in terms:
                    other_terms.add(name)
    repaired = parsed.model_copy(deep=True)
    trace = []
    consumed = set()
    for name, record, parts, measure, removed in proposals:
        ids = {m.mention_id for m in parts}
        overlapping = any(record != other_record and ids & {m.mention_id for m in other_parts}
            for _, other_record, other_parts, *_ in proposals)
        if name in other_terms or ids & consumed or overlapping:
            continue
        target = next(m for m in repaired.mentions if m.mention_id == measure.mention_id)
        target.surface = target.normalized_surface = name
        target.start_char, target.end_char = parts[0].start_char, parts[-1].end_char
        repaired.mentions = [m for m in repaired.mentions if m.mention_id not in removed]
        repaired.operation_markers = [m for m in repaired.operation_markers if m.mention_id not in removed]
        repaired.explicit_slot_mentions = {slot: [i for i in mentions if i not in removed]
            for slot, mentions in repaired.explicit_slot_mentions.items()}
        repaired.negations = list(dict.fromkeys(measure.mention_id if i in removed else i for i in repaired.negations))
        consumed.update(ids)
        trace.append({'reason_code': 'EXACT_UNIQUE_PINNED_METRIC_SPAN', 'mention_id': measure.mention_id,
            'absorbed_mention_ids': sorted(removed), 'span': [target.start_char, target.end_char],
            'catalog_record_id': record, 'catalog_version': session.context.catalog_pin.catalog_version,
            'canonical_selection_performed': False})
    return repaired, trace
