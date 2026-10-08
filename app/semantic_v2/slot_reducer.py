"""Deterministic slot reducer used by the offline V2 shadow path."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, model_validator

from .enums import Presence, SlotOperationType
from .models import BoundSemanticRef, SlotOperation, StrictModel, TaskSemanticState
from .registries import SlotDefinitionRegistry


class SlotValue(StrictModel):
    """Materialized state for one canonical task slot."""

    value: JsonValue = None
    presence: Presence = Presence.UNSPECIFIED
    last_operation_id: str | None = None
    source: str | None = None


class TaskSlotState(StrictModel):
    """Versioned slot map; values are never inferred implicitly."""

    task_version: int = Field(ge=0)
    slots: dict[str, SlotValue] = Field(default_factory=dict)


class SlotReducerError(ValueError):
    """Raised when a slot patch violates deterministic merge invariants."""


_DISPLAY_FIELDS = frozenset({'display_name', 'display_label', 'source_mention_ids', 'mention_ids',
                            'resolution_source', 'provenance', 'source', 'current_turn_ref',
                            'current_turn_digest', 'sanitized_turn_text', 'last_operation_id'})
_SET_FIELDS = frozenset({'metrics', 'measures', 'dimensions', 'group_by', 'analysis_goals',
                        'policy_decisions', 'business_domain_ids', 'source_measure_refs'})


def _canonical(value, field_name=''):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='python')
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f')
    if isinstance(value, dict):
        result = {k: _canonical(v, k) for k, v in sorted(value.items()) if k not in _DISPLAY_FIELDS}
        if result.get('node_type') == 'BOOLEAN_GROUP' and result.get('operator') in {'AND', 'OR'}:
            result['children'] = sorted(result['children'], key=_json)
        return result
    if isinstance(value, (list, tuple)):
        items = [_canonical(v) for v in value]
        return sorted({_json(v): v for v in items}.values(), key=_json) if field_name in _SET_FIELDS else items
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def canonicalize_task_semantics(value) -> dict:
    if hasattr(value, 'payload') and hasattr(value, 'service_route'):
        value = dict(payload=value.payload, service_route=value.service_route,
                     analysis_goals=value.analysis_goals, delivery_spec=value.delivery_spec,
                     policy_decisions=value.policy_decisions)
    return _canonical(value)


def semantic_fingerprint(value) -> str:
    return hashlib.sha256(_json(canonicalize_task_semantics(value)).encode('utf-8')).hexdigest()


class TaskPatch(StrictModel):
    """Declarative patch; raw operation order is not an execution program."""
    reset: bool = False
    clears: list[SlotOperation] = Field(default_factory=list)
    removes: list[SlotOperation] = Field(default_factory=list)
    replacements: list[SlotOperation] = Field(default_factory=list)
    sets: list[SlotOperation] = Field(default_factory=list)
    adds: list[SlotOperation] = Field(default_factory=list)
    inherit_requests: list[SlotOperation] = Field(default_factory=list)
    base_task_version: int = Field(ge=0)

    @model_validator(mode='after')
    def validate_declaration(self):
        phases = (('clears', SlotOperationType.CLEAR), ('removes', SlotOperationType.REMOVE),
                  ('replacements', SlotOperationType.REPLACE), ('sets', SlotOperationType.SET),
                  ('adds', SlotOperationType.ADD), ('inherit_requests', SlotOperationType.INHERIT))
        assignments = {}
        identities = set()
        for name, kind in phases:
            for op in getattr(self, name):
                definition = SlotDefinitionRegistry.get(op.slot_path)
                if op.operation != kind or op.operation not in definition.allowed_operations:
                    raise SlotReducerError('operation is not allowed in this slot/patch phase')
                if op.base_task_version != self.base_task_version:
                    raise SlotReducerError('patch contains mixed base versions')
                if op.operation_id in identities:
                    raise SlotReducerError('duplicate operation identity')
                identities.add(op.operation_id)
                if kind in {SlotOperationType.REPLACE, SlotOperationType.SET, SlotOperationType.INHERIT}:
                    assignment_key = (op.slot_path, 'inherit' if kind == SlotOperationType.INHERIT else 'explicit')
                    prior = assignments.get(assignment_key)
                    canonical = _json(_canonical(op.new_value))
                    if prior is not None and prior != canonical:
                        raise SlotReducerError('conflicting explicit assignments')
                    assignments[assignment_key] = canonical
                if kind not in {SlotOperationType.CLEAR, SlotOperationType.REMOVE}:
                    value = op.new_value
                    if kind == SlotOperationType.ADD and not isinstance(value, list):
                        value = [value]
                    TypeAdapter(definition.value_type).validate_python(value)
        return self

    @classmethod
    def compile(cls, operations: list[SlotOperation], *, base_task_version: int):
        groups = {SlotOperationType.CLEAR: 'clears', SlotOperationType.REMOVE: 'removes',
                  SlotOperationType.REPLACE: 'replacements', SlotOperationType.SET: 'sets',
                  SlotOperationType.ADD: 'adds', SlotOperationType.INHERIT: 'inherit_requests'}
        data = {'base_task_version': base_task_version}
        priority = {'CURRENT_EXPLICIT': 3, 'USER_EXPLICIT': 3, 'CURRENT_REFERENCE_RESOLUTION': 2,
                    'HISTORY': 1, 'SYSTEM_DEFAULT': 0, 'FIXTURE': 3}
        highest = {}
        for op in operations:
            if op.operation not in {SlotOperationType.INHERIT, SlotOperationType.KEEP}:
                highest[op.slot_path] = max(highest.get(op.slot_path, -1), priority[op.source])
        for op in operations:
            if op.base_task_version != base_task_version:
                raise SlotReducerError('patch base version conflict')
            if op.operation not in {SlotOperationType.INHERIT, SlotOperationType.KEEP} and priority[op.source] < highest[op.slot_path]:
                continue
            if op.operation == SlotOperationType.RESET_TASK:
                data['reset'] = True
            elif op.operation == SlotOperationType.KEEP:
                SlotDefinitionRegistry.get(op.slot_path)
            else:
                data.setdefault(groups[op.operation], []).append(op)
        for name in groups.values():
            if name in data:
                data[name].sort(key=lambda op: (op.slot_path, _json(_canonical(op.new_value)), op.operation_id))
        return cls.model_validate(data)


class PatchResult(StrictModel):
    semantics: TaskSemanticState
    clear_barriers: list[str]
    changed: bool
    dataset_invalidated: bool
    semantic_fingerprint: str


def _identity(value):
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, dict) and value.get('canonical_id'):
        return (value.get('catalog_type'), value['canonical_id'], value.get('catalog_version'), value.get('semantic_model_id'))
    return _json(_canonical(value))


def apply_task_patch(state: TaskSemanticState, patch: TaskPatch, *, clear_barriers=()) -> PatchResult:
    data = (TaskSemanticState() if patch.reset else state).model_dump(mode='python')
    defaults = TaskSemanticState().model_dump(mode='python')
    barriers = set() if patch.reset else set(clear_barriers)
    explicit = {o.slot_path for phase in (patch.replacements, patch.sets, patch.adds) for o in phase}
    for op in patch.clears:
        data[op.slot_path] = deepcopy(defaults[op.slot_path])
        barriers.add(op.slot_path)
    for op in patch.removes:
        target = op.target_item_id
        wanted = op.new_value if isinstance(op.new_value, list) else [op.new_value]
        keys = {_identity(v) for v in wanted}
        data[op.slot_path] = [v for v in data[op.slot_path] if not
                              ((target is not None and isinstance(v, dict) and v.get('canonical_id') == target) or _identity(v) in keys)]
    for phase in (patch.replacements, patch.sets):
        for op in phase:
            data[op.slot_path] = deepcopy(op.new_value)
            barriers.discard(op.slot_path)
    for op in patch.adds:
        incoming = op.new_value if isinstance(op.new_value, list) else [op.new_value]
        existing = {_identity(v): v for v in data[op.slot_path]}
        for value in incoming:
            existing[_identity(value)] = deepcopy(value)
        data[op.slot_path] = list(existing.values())
        barriers.discard(op.slot_path)
    for op in patch.inherit_requests:
        if op.slot_path not in barriers and op.slot_path not in explicit and data[op.slot_path] == defaults[op.slot_path]:
            data[op.slot_path] = deepcopy(op.new_value)
    for name, definition in SlotDefinitionRegistry.definitions.items():
        if definition.cardinality == 'SET':
            data[name] = sorted({_identity(v): v for v in data[name]}.values(), key=lambda v: _json(_canonical(v)))
    next_state = TaskSemanticState.model_validate(data)
    fingerprint = semantic_fingerprint(next_state)
    changed = fingerprint != semantic_fingerprint(state)
    invalidated = any(semantic_fingerprint({'value': getattr(state, name)}) != semantic_fingerprint({'value': getattr(next_state, name)})
                      and definition.invalidation_policy == 'INVALIDATE_DATASET'
                      for name, definition in SlotDefinitionRegistry.definitions.items())
    return PatchResult(semantics=next_state, clear_barriers=sorted(barriers), changed=changed,
                       dataset_invalidated=invalidated, semantic_fingerprint=fingerprint)


def apply_slot_operations(
    state: TaskSlotState,
    operations: list[SlotOperation],
) -> TaskSlotState:
    """Apply ordered operations without mutating ``state``.

    Current explicit SET/ADD/REPLACE/CLEAR operations win because INHERIT is
    rejected after an explicit clear and cannot overwrite a present value.
    Every operation must target the same base task version, providing a small
    compare-and-swap boundary for the shadow state.
    """

    slots = deepcopy(state.slots)
    # Compatibility view for 0.2 callers; new execution state uses apply_task_patch.
    order = {SlotOperationType.RESET_TASK: 0, SlotOperationType.CLEAR: 1, SlotOperationType.REMOVE: 2,
             SlotOperationType.REPLACE: 3, SlotOperationType.SET: 4, SlotOperationType.ADD: 5,
             SlotOperationType.INHERIT: 6, SlotOperationType.KEEP: 7}
    for operation in sorted(operations, key=lambda op: (order[op.operation], op.slot_path, _json(op.new_value))):
        SlotDefinitionRegistry.get(operation.slot_path)
        if operation.base_task_version != state.task_version:
            raise SlotReducerError("slot operation base_task_version conflicts with state")
        current = slots.get(operation.slot_path, SlotValue())
        op = operation.operation
        if op == SlotOperationType.RESET_TASK:
            slots = {}
            continue
        if op == SlotOperationType.KEEP:
            continue
        if op == SlotOperationType.INHERIT:
            if current.presence == Presence.EXPLICITLY_CLEARED:
                continue
            if current.presence == Presence.PRESENT:
                continue
            slots[operation.slot_path] = SlotValue(
                value=deepcopy(operation.new_value),
                presence=Presence.PRESENT,
                last_operation_id=operation.operation_id,
                source=operation.source,
            )
            continue
        if op == SlotOperationType.CLEAR:
            slots[operation.slot_path] = SlotValue(
                value=None,
                presence=Presence.EXPLICITLY_CLEARED,
                last_operation_id=operation.operation_id,
                source=operation.source,
            )
            continue
        if op == SlotOperationType.REMOVE:
            value = _remove_value(current.value, operation.target_item_id, operation.new_value)
            slots[operation.slot_path] = SlotValue(
                value=value,
                presence=Presence.PRESENT if value not in (None, [], {}) else Presence.UNSPECIFIED,
                last_operation_id=operation.operation_id,
                source=operation.source,
            )
            continue
        if op == SlotOperationType.ADD:
            slots[operation.slot_path] = SlotValue(
                value=_add_value(current.value, operation.new_value),
                presence=Presence.PRESENT,
                last_operation_id=operation.operation_id,
                source=operation.source,
            )
            continue
        if op in {SlotOperationType.SET, SlotOperationType.REPLACE}:
            slots[operation.slot_path] = SlotValue(
                value=deepcopy(operation.new_value),
                presence=Presence.PRESENT,
                last_operation_id=operation.operation_id,
                source=operation.source,
            )
            continue
        raise SlotReducerError(f"unsupported slot operation: {op}")
    changed = semantic_fingerprint(slots) != semantic_fingerprint(state.slots)
    return TaskSlotState(task_version=state.task_version + int(changed), slots=slots)


def _add_value(old_value: Any, new_value: Any) -> Any:
    """Append new values while retaining order and existing values."""

    old_items = list(old_value) if isinstance(old_value, list) else ([] if old_value is None else [old_value])
    new_items = list(new_value) if isinstance(new_value, list) else [new_value]
    result = deepcopy(old_items)
    for item in new_items:
        if item not in result:
            result.append(deepcopy(item))
    return result


def _remove_value(old_value: Any, target_item_id: str | None, new_value: Any) -> Any:
    """Remove a selected item from scalar/list values without fuzzy matching."""

    target = target_item_id if target_item_id is not None else new_value
    if isinstance(old_value, list):
        return [item for item in old_value if _item_identity(item) != target and item != target]
    if _item_identity(old_value) == target or old_value == target:
        return None
    return deepcopy(old_value)


def _item_identity(value: Any) -> Any:
    """Return a stable canonical identity when one is present."""

    if isinstance(value, dict):
        return value.get("canonical_id") or value.get("id")
    return getattr(value, "canonical_id", None) or getattr(value, "id", None)
