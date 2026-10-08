"""Current-evidence edits of scoped task subtrees, lowered to the existing reducer.

Handles identify an exact task version and tree position. They are references, not
authority; only the already restored current-scope task supplies their values.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter

from . import models as m
from .authorized_contract import contract_digest
from .recognition_client import RecognitionFailure
from .slot_reducer import semantic_fingerprint


class FilterEditDraft(m.StrictModel):
    operation: Literal['ADD', 'REPLACE', 'REMOVE', 'CLEAR']
    target_handle: m.Identifier | None = None
    evidence_mention_ids: list[m.Identifier] = Field(min_length=1, max_length=50)
    # ADD without a target: a new Predicate ANDed with the whole tree.
    # ADD/REMOVE with a value: positive EQ/IN membership editing.
    # REPLACE: a new scalar/list for the selected predicate, preserving its field.
    value: JsonValue = None


class TemporalEditDraft(m.StrictModel):
    component: Literal['RANGE', 'GRAIN', 'ANCHOR']
    operation: Literal['SET', 'REPLACE', 'CLEAR']
    evidence_mention_ids: list[m.Identifier] = Field(min_length=1, max_length=50)
    value: JsonValue = None


class StructuredEditTrace(m.StrictModel):
    slot_path: Literal['filter_expression', 'time_spec']
    operation: Literal['ADD', 'REPLACE', 'REMOVE', 'CLEAR', 'SET']
    target: m.Identifier
    evidence_mention_ids: list[m.Identifier]
    before_digest: m.Identifier
    after_digest: m.Identifier
    lowered_operation: Literal['REPLACE', 'CLEAR']


def _nodes(node, path=()):
    if node is None:
        return
    yield path, node
    if isinstance(node, m.BooleanFilterGroup):
        for index, child in enumerate(node.children):
            yield from _nodes(child, (*path, index))


def filter_targets(task):
    if task is None:
        return {}
    state = next(v.semantics for v in task.versions if v.version == task.active_version)
    return {'filter:' + contract_digest([task.task_id, task.active_version, path,
        node.model_dump(mode='json')])[:40]: (path, node)
        for path, node in _nodes(state.filter_expression)}


def validate_current_filter(expression):
    """Both edit channels accept only current user predicates, recursively."""
    for _, node in _nodes(expression):
        if isinstance(node, (m.Predicate, m.AliasedPredicate)) and (
                node.source != 'USER_EXPLICIT' or node.scope != 'CURRENT_TASK'):
            raise RecognitionFailure('V2_FILTER_CURRENT_EVIDENCE_REQUIRED')


def _labels(value):
    """History is transient model context, not a source of new binding authority."""
    if isinstance(value, m.BoundSemanticRef):
        return dict(name=value.display_name, role=value.semantic_role, catalog_type=value.catalog_type)
    if isinstance(value, m.StrictModel):
        return {k: _labels(getattr(value, k)) for k in type(value).model_fields
            if k not in {'source_mention_ids', 'mention_ids', 'source', 'validation_status'}}
    if isinstance(value, (list, tuple)):
        return [_labels(v) for v in value]
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    # Decimal values are serialized by the existing model's JSON encoder below.
    from decimal import Decimal
    return str(value) if isinstance(value, Decimal) else value


def structured_labels(task):
    state = next(v.semantics for v in task.versions if v.version == task.active_version)
    return dict(filter_targets=[dict(target_handle=h, path=list(path), expression=_labels(node))
        for h, (path, node) in filter_targets(task).items()], time=_labels(state.time_spec),
        relationship_path=_labels(state.relationship_spec), comparison=_labels(state.comparison_spec))


def _replace_nodes(node, changes, path=()):
    if path in changes:
        return changes[path]
    if not isinstance(node, m.BooleanFilterGroup):
        return node
    children = [_replace_nodes(v, changes, (*path, i)) for i, v in enumerate(node.children)]
    children = [v for v in children if v is not None]
    if not children:
        return None
    # Never flatten AND/OR across NOT, or move an edited predicate to another branch.
    return m.BooleanFilterGroup(operator=node.operator, children=children)


def _items(value):
    return value.values if isinstance(value, m.ListValue) else [value]


def _membership(node, edit, value):
    if node.operator not in {'EQ', 'IN'}:
        raise RecognitionFailure('V2_FILTER_MEMBERSHIP_OPERATOR_UNSUPPORTED')
    values = {semantic_fingerprint(v): v for v in _items(node.value)}
    incoming = {semantic_fingerprint(v): v for v in _items(value)}
    if edit.operation == 'REMOVE':
        if not incoming.keys() <= values.keys():
            raise RecognitionFailure('V2_FILTER_REMOVE_VALUE_NOT_PRESENT')
        values = {k: v for k, v in values.items() if k not in incoming}
    else:
        values.update(incoming)
    if not values:
        return None
    ordered = list(values.values())
    return dict(operator='EQ' if len(ordered) == 1 else 'IN',
        value=ordered[0] if len(ordered) == 1 else m.ListValue(values=ordered))


def filter_edits(prior, task, edits, hydrate):
    targets = filter_targets(task)
    changes, additions = {}, []
    for edit in edits:
        if edit.target_handle is None:
            if edit.operation != 'ADD' or edit.value is None:
                raise RecognitionFailure('V2_FILTER_TARGET_REQUIRED')
            predicate = TypeAdapter(m.AliasedPredicate | m.Predicate).validate_python(hydrate(edit.value, edit.evidence_mention_ids))
            validate_current_filter(predicate)
            additions.append(predicate)
            continue
        if edit.target_handle not in targets:
            raise RecognitionFailure('V2_FILTER_TARGET_NOT_CURRENT_TASK')
        path, node = targets[edit.target_handle]
        if any(path[:len(p)] == p or p[:len(path)] == path for p in changes):
            raise RecognitionFailure('V2_FILTER_OVERLAPPING_EDITS')
        if edit.operation in {'CLEAR', 'REMOVE'} and edit.value is None:
            changes[path] = None
            continue
        if edit.operation == 'CLEAR' or not isinstance(node, m.Predicate) or edit.value is None:
            raise RecognitionFailure('V2_FILTER_VALUE_EDIT_INVALID')
        value = TypeAdapter(m.TypedFilterValue).validate_python(hydrate(edit.value, edit.evidence_mention_ids))
        if edit.operation in {'ADD', 'REMOVE'}:
            update = _membership(node, edit, value)
        else:
            operator = node.operator
            if operator in {'EQ', 'IN'}:
                operator = 'IN' if isinstance(value, m.ListValue) else 'EQ'
            update = dict(value=value, operator=operator)
        changes[path] = (type(node).model_validate(dict(node.model_dump(), **update,
            source='USER_EXPLICIT', mention_ids=edit.evidence_mention_ids, validation_status='UNKNOWN'))
            if update is not None else None)
    result = _replace_nodes(prior.filter_expression, changes)
    for addition in additions:
        result = m.BooleanFilterGroup(operator='AND', children=[result, addition]) if result else addition
    return result


def temporal_edits(prior, edits, hydrate, now, *, comparison_edit=False):
    if prior.time_spec is None:
        raise RecognitionFailure('V2_TEMPORAL_BASE_REQUIRED')
    data = prior.time_spec.model_dump()
    components = set()
    for edit in edits:
        if edit.component in components:
            raise RecognitionFailure('V2_TEMPORAL_OVERLAPPING_EDITS')
        components.add(edit.component)
        if edit.component in {'RANGE', 'ANCHOR'} and (data['comparison'] is not None or (
                prior.comparison_spec is not None and not isinstance(prior.comparison_spec, m.TemporalComparisonSpec) and not comparison_edit)):
            raise RecognitionFailure('V2_TEMPORAL_COMPARISON_DEPENDENCY')
        if edit.operation == 'CLEAR':
            if edit.value is not None or edit.component == 'ANCHOR':
                raise RecognitionFailure('V2_TEMPORAL_CLEAR_INVALID')
            value = None if edit.component == 'RANGE' else 'NONE'
        else:
            value = hydrate(edit.value, edit.evidence_mention_ids)
            expected = {'RANGE': m.TimeRange, 'GRAIN': m.TimeGrain, 'ANCHOR': m.BoundSemanticRef}[edit.component]
            value = TypeAdapter(expected).validate_python(value)
        data[edit.component.lower()] = value
    # The cleared range is represented explicitly; no guessed earliest date/default.
    data.update(source='USER_EXPLICIT_UNBOUNDED' if data['range'] is None else 'USER_EXPLICIT', as_of=now,
        default_policy_id=None, default_policy_version=None)
    return m.TimeSpec.model_validate(data)


def lower_edits(prior, task, filter_drafts, time_drafts, hydrate, base, now, *, comparison_edit=False):
    operations, traces = [], []
    for slot, edits, build in [('filter_expression', filter_drafts,
            lambda: filter_edits(prior, task, filter_drafts, hydrate)),
            ('time_spec', time_drafts, lambda: temporal_edits(prior, time_drafts, hydrate, now, comparison_edit=comparison_edit))]:
        if not edits:
            continue
        value = build()
        operation = 'CLEAR' if value is None else 'REPLACE'
        operations.append(m.SlotOperation(operation_id='structured:' + slot, slot_path=slot,
            operation=operation, new_value=value.model_dump(mode='json') if value else None,
            source='CURRENT_EXPLICIT', reason_code='CURRENT_STRUCTURED_EDIT', base_task_version=base,
            presence='EXPLICITLY_CLEARED' if value is None else 'PRESENT',
            evidence_mention_ids=sorted({i for edit in edits for i in edit.evidence_mention_ids})))
        for edit in edits:
            traces.append(StructuredEditTrace(slot_path=slot, operation=edit.operation,
                target=(edit.target_handle or 'NEW_CONDITION') if slot == 'filter_expression' else edit.component,
                evidence_mention_ids=edit.evidence_mention_ids,
                before_digest=semantic_fingerprint(getattr(prior, slot)), after_digest=semantic_fingerprint(value),
                lowered_operation=operation))
    return operations, traces
