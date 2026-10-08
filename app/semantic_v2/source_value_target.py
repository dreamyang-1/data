"""Existing filter identity constrains source lookup before any retrieval.

No name/code equivalence is inferred. Generation can use the existing target
selector; consumption and final binding both enforce the same canonical field.
"""
from . import models as m
from .recognition_client import RecognitionFailure
from .structured_edits import filter_targets


def referenced_requests(value):
    if isinstance(value, dict):
        return ({value['value_request_id']} if 'value_request_id' in value else set()).union(
            *(referenced_requests(v) for v in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(referenced_requests(v) for v in value))
    return set()


def validate_requested_fields(draft, request, fields, target, session):
    """An existing predicate's value edit cannot select a different source field."""
    targets = filter_targets(target)
    required = set()
    for edit in draft.filter_edits:
        if edit.target_handle is None or request.request_id not in referenced_requests(edit.value):
            continue
        current = targets.get(edit.target_handle)
        if current is None or not isinstance(current[1], m.Predicate):
            raise RecognitionFailure('V2_SOURCE_VALUE_FILTER_TARGET_INVALID')
        required.add(current[1].field_ref.canonical_id)
    if required and (len(required) != 1 or any(
            session._rows[attribute].metadata['catalog_logical_id'] not in required for attribute, _ in fields)):
        raise RecognitionFailure('SOURCE_VALUE_FILTER_FIELD_MISMATCH')


def source_target_schema(schema, parse, tasks):
    """Export the target-selector branch for declared existing value mutations.

REPLACE/REMOVE of a current filter value preserve its field. ADD can introduce
a new predicate, so it retains both existing selector forms. The model chooses
among offered targets; this function never picks a target or changes a relation.
"""
    offered = sorted({handle for task in tasks.values() for handle, (_, node) in filter_targets(task).items()
        if isinstance(node, m.Predicate) and node.field_ref.catalog_type == 'ATTRIBUTE'})
    if not offered:
        return schema
    rules = []
    for mention in getattr(parse, 'mentions', ()):
        operations = {mark.operation_hint for mark in getattr(parse, 'operation_markers', ())
            if mark.slot_name == 'filter_expression' and mark.mention_id == mention.mention_id}
        if 'FILTER_VALUE' not in mention.candidate_roles or not operations or not operations <= {'REPLACE', 'REMOVE'}:
            continue
        constraint = {'required': ['target_filter_handle'], 'properties': {
            'field_binding_handles': {'maxItems': 0},
            'target_filter_handle': {'type': 'string', 'enum': offered}}}
        rules.append({'if': {'properties': {'mention_id': {'const': mention.mention_id}}}, 'then': constraint})
    if rules:
        schema['$defs']['SourceValueRequestDraft']['allOf'] = rules
    return schema
