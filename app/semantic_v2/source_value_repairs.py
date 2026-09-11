"""Lossless alignment of current source-value identities and filter edits.

The model may copy a request's mention pointer from a different offered
object, or omit the filter wrapper after selecting a current value request.
These repairs use only identities already present in the current parse,
offered handles and explicit operation evidence.  They never select a
catalog identity, source value, task or scope.
"""
from .structured_edits import FilterEditDraft


def _request_ids(value):
    if isinstance(value, dict):
        result = ({value['value_request_id']} if 'value_request_id' in value else set())
        for child in value.values():
            result.update(_request_ids(child))
        return result
    if isinstance(value, (list, tuple)):
        result = set()
        for child in value:
            result.update(_request_ids(child))
        return result
    return set()


def _request_edits(draft, request_id):
    return [edit for edit in (*draft.edits, *draft.filter_edits)
        if request_id in _request_ids(edit.value)]


def _current_filter_value(parse, mention_id, unresolved):
    matches = [mention for mention in parse.mentions if mention.mention_id == mention_id]
    return (matches[0] if len(matches) == 1 and matches[0].explicit
        and 'FILTER_VALUE' in matches[0].candidate_roles
        and mention_id not in unresolved else None)


def _field_handle_mention(request, handles):
    if request.target_filter_handle is not None or not request.field_binding_handles:
        return None
    selected = []
    for handle in request.field_binding_handles:
        value = handles.get(handle)
        if value is None or value[1] != 'FILTER_FIELD':
            return None
        selected.append(value[2])
    return selected[0] if len(set(selected)) == 1 else None


def _rebind_field_handles(request, handles, mention_id):
    """Keep each selected Catalog identity while moving it to exact evidence."""
    rebound = []
    for handle in request.field_binding_handles:
        value = handles.get(handle)
        if value is None or value[1] != 'FILTER_FIELD':
            return None
        if value[2] == mention_id:
            rebound.append(handle)
            continue
        matches = [candidate for candidate, offered in handles.items()
            if offered == (value[0], 'FILTER_FIELD', mention_id)]
        if len(matches) != 1:
            return None
        rebound.append(matches[0])
    return rebound


def align_source_value_requests(parse, draft, handles, *, base, target):
    """Align exact current request pointers and an omitted initial filter.

    A request pointer may be retargeted only when its selected field handles
    and every consuming edit independently identify the same current
    FILTER_VALUE mention.  A missing initial filter can be represented only
    when every otherwise-unused request has one current field handle and an
    explicit ADD declaration, with no competing filter edit.  SET still needs
    the model's complete Boolean operand and is never synthesized here.
    """
    repaired = draft.model_copy(deep=True)
    unresolved = set(repaired.unresolved_mention_ids)
    traces = []

    # A selected field handle is mention-scoped. Require it and all consuming
    # edits to agree before changing a copied pointer. For a wholly omitted
    # initial filter, the exact ADD marker supplies the missing edit evidence.
    for index, request in enumerate(repaired.source_value_requests):
        field_mention = _field_handle_mention(request, handles)
        if field_mention is None or field_mention == request.mention_id:
            continue
        edits = _request_edits(repaired, request.request_id)
        edit_mentions = {mention_id for edit in edits for mention_id in edit.evidence_mention_ids
            if _current_filter_value(parse, mention_id, unresolved) is not None}
        explicit = set(parse.explicit_slot_mentions.get('filter_expression', ()))
        initial = (not edits and target is None and base == 0
            and not repaired.filter_edits
            and not any(edit.slot_path == 'filter_expression' for edit in repaired.edits)
            and field_mention in explicit
            and {(marker.slot_name, marker.operation_hint, marker.mention_id)
                for marker in parse.operation_markers
                if marker.mention_id == field_mention} == {
                    ('filter_expression', 'ADD', field_mention)})
        if ((edits and edit_mentions != {field_mention}) or (not edits and not initial)
                or _current_filter_value(parse, field_mention, unresolved) is None):
            continue
        request.mention_id = field_mention
        traces.append({'reason_code': 'CURRENT_SOURCE_REQUEST_MENTION_IDENTITY',
            'field': f'source_value_requests[{index}].mention_id',
            'from_mention_id': draft.source_value_requests[index].mention_id,
            'to_mention_id': field_mention})

    # Preserve the chosen Catalog identity while correcting only the handle's
    # current mention component. An absent or non-unique equivalent stays bad.
    for index, request in enumerate(repaired.source_value_requests):
        if (_current_filter_value(parse, request.mention_id, unresolved) is None
                or request.target_filter_handle is not None):
            continue
        rebound = _rebind_field_handles(request, handles, request.mention_id)
        if rebound is None or rebound == request.field_binding_handles:
            continue
        old_mentions = [handles[handle][2] for handle in request.field_binding_handles]
        request.field_binding_handles = rebound
        traces.append({'reason_code': 'CURRENT_SOURCE_FIELD_HANDLE_IDENTITY',
            'field': f'source_value_requests[{index}].field_binding_handles',
            'from_mention_ids': old_mentions, 'to_mention_id': request.mention_id})

    used = set().union(*(_request_ids(edit.value)
        for edit in (*repaired.edits, *repaired.filter_edits))) if (
            repaired.edits or repaired.filter_edits) else set()
    request_ids = [request.request_id for request in repaired.source_value_requests]
    unused = [request for request in repaired.source_value_requests if request.request_id not in used]

    # Never reinterpret an extra request or compete with a filter structure.
    # This adapter is limited to an entirely empty new task and to the case
    # where every declared request is the omitted current filter assignment.
    if (not unused or len(set(request_ids)) != len(request_ids)
            or set(request_ids) != {request.request_id for request in unused}
            or target is not None or base != 0
            or repaired.filter_edits
            or any(edit.slot_path == 'filter_expression' for edit in repaired.edits)):
        return repaired, traces

    explicit = set(parse.explicit_slot_mentions.get('filter_expression', ()))
    marker_ops = {}
    predicates = []
    for request in unused:
        mention = _current_filter_value(parse, request.mention_id, unresolved)
        handle_mention = _field_handle_mention(request, handles)
        operations = {marker.operation_hint for marker in parse.operation_markers
            if marker.slot_name == 'filter_expression' and marker.mention_id == request.mention_id}
        if (mention is None or handle_mention != request.mention_id
                or request.mention_id not in explicit or len(operations) != 1
                or operations != {'ADD'}):
            return repaired, traces
        marker_ops[request.request_id] = next(iter(operations))
        predicates.append({
            'node_type': 'PREDICATE',
            'field_ref': {'value_field_request_id': request.request_id},
            'operator': 'EQ',
            'value': {'value_request_id': request.request_id},
            'source': 'USER_EXPLICIT',
            'mention_ids': [request.mention_id],
            'scope': 'CURRENT_TASK',
        })
    if len(set(request.mention_id for request in unused)) != len(unused):
        return repaired, traces
    operations = set(marker_ops.values())
    if operations != {'ADD'}:
        return repaired, traces

    operation = 'ADD'
    evidence = sorted(request.mention_id for request in unused)
    repaired.filter_edits.extend(FilterEditDraft(operation='ADD', target_handle=None,
        evidence_mention_ids=[request.mention_id], value=predicate)
        for request, predicate in zip(unused, predicates))
    traces.append({'reason_code': 'CURRENT_SOURCE_REQUEST_INITIAL_FILTER',
        'field': 'filter_expression', 'operation': operation,
        'request_count': len(unused), 'evidence_mention_ids': evidence})
    return repaired, traces
