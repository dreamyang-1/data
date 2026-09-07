"""Deterministic slot reducer used by the offline V2 shadow path."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import Field

from .enums import Presence, SlotOperationType
from .models import SlotOperation, StrictModel


class SlotValue(StrictModel):
    """Materialized state for one canonical task slot."""

    value: Any = None
    presence: Presence = Presence.UNSPECIFIED
    last_operation_id: str | None = None
    source: str | None = None


class TaskSlotState(StrictModel):
    """Versioned slot map; values are never inferred implicitly."""

    task_version: int = Field(ge=0)
    slots: dict[str, SlotValue] = Field(default_factory=dict)


class SlotReducerError(ValueError):
    """Raised when a slot patch violates deterministic merge invariants."""


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
    for operation in operations:
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
    return TaskSlotState(task_version=state.task_version + 1, slots=slots)


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
