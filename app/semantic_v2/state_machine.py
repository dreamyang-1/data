"""Versioned conversation/task state machine for shadow-only evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from .enums import TaskVersionStatus
from .models import Identifier, StrictModel


class StateEvent(StrEnum):
    NEW_TOPIC = "NEW_TOPIC"
    SWITCH_TOPIC = "SWITCH_TOPIC"
    RETURN_TOPIC = "RETURN_TOPIC"
    MODIFY_TASK = "MODIFY_TASK"
    ADD_SLOT = "ADD_SLOT"
    REPLACE_SLOT = "REPLACE_SLOT"
    REMOVE_SLOT = "REMOVE_SLOT"
    CLEAR_SLOT = "CLEAR_SLOT"
    DRILL_DOWN = "DRILL_DOWN"
    ANSWER_PENDING = "ANSWER_PENDING"
    CANCEL_PENDING = "CANCEL_PENDING"
    REFRESH_TASK = "REFRESH_TASK"
    REVISE_TASK = "REVISE_TASK"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    DATASET_CREATED = "DATASET_CREATED"
    DATASET_INVALIDATED = "DATASET_INVALIDATED"


class PendingClarification(StrictModel):
    """Version-bound pending question that cannot hijack unrelated tasks."""

    pending_id: Identifier
    task_id: Identifier
    task_version: int = Field(ge=1)
    slot_path: str = Field(min_length=1, max_length=500)
    question: str = Field(min_length=1, max_length=1000)
    asked_at: datetime


class DatasetState(StrictModel):
    """Immutable dataset reference associated with an executed task version."""

    dataset_id: Identifier
    task_id: Identifier
    task_version: int = Field(ge=1)
    status: Literal["VALID", "INVALIDATED"] = "VALID"
    invalidation_reason: str | None = Field(default=None, max_length=500)


class TaskVersion(StrictModel):
    """One immutable semantic version of a task."""

    version: int = Field(ge=1)
    status: TaskVersionStatus
    plan_id: Identifier | None = None
    slot_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskState(StrictModel):
    """Task identity and distinct execution/version pointers."""

    task_id: Identifier
    topic_id: Identifier
    active_version: int = Field(ge=1)
    status: TaskVersionStatus
    parent_task_id: Identifier | None = None
    child_task_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    versions: list[TaskVersion] = Field(min_length=1, max_length=1000)
    last_executable_version: int | None = Field(default=None, ge=1)
    last_executed_version: int | None = Field(default=None, ge=1)
    last_dataset_id: Identifier | None = None


class TopicState(StrictModel):
    """Topic branch with its own anchors and active task."""

    topic_id: Identifier
    title: str = Field(min_length=1, max_length=500)
    status: Literal["ACTIVE", "INACTIVE", "CANCELLED"] = "ACTIVE"
    active_task_id: Identifier | None = None
    task_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    anchor_entities: list[Identifier] = Field(default_factory=list, max_length=100)
    anchor_metrics: list[Identifier] = Field(default_factory=list, max_length=100)
    last_accessed_at: datetime


class ConversationState(StrictModel):
    """CAS-versioned state tree; not wired to the current Redis schema."""

    conversation_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    application_id: Identifier
    state_version: int = Field(ge=0)
    active_topic_id: Identifier | None = None
    topic_stack: list[Identifier] = Field(default_factory=list, max_length=100)
    recent_turn_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    topics: dict[str, TopicState] = Field(default_factory=dict)
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    pending: PendingClarification | None = None
    datasets: dict[str, DatasetState] = Field(default_factory=dict)


class StateTransitionError(ValueError):
    """Raised for invalid state transitions or CAS conflicts."""


def apply_state_event(
    state: ConversationState,
    *,
    event: StateEvent,
    expected_state_version: int,
    payload: dict[str, Any] | None = None,
) -> ConversationState:
    """Apply a minimal deterministic state transition with CAS protection."""

    if expected_state_version != state.state_version:
        raise StateTransitionError("conversation state version conflict")
    data = state.model_dump(mode="python")
    body = payload or {}
    if event == StateEvent.NEW_TOPIC:
        topic = TopicState.model_validate(body["topic"])
        task = TaskState.model_validate(body["task"])
        if topic.topic_id in state.topics or task.task_id in state.tasks:
            raise StateTransitionError("topic or task already exists")
        topic.active_task_id = task.task_id
        topic.task_ids.append(task.task_id)
        data["topics"][topic.topic_id] = topic.model_dump(mode="python")
        data["tasks"][task.task_id] = task.model_dump(mode="python")
        data["active_topic_id"] = topic.topic_id
        data["topic_stack"] = [*state.topic_stack, topic.topic_id]
        data["pending"] = None
    elif event in {StateEvent.SWITCH_TOPIC, StateEvent.RETURN_TOPIC}:
        topic_id = str(body.get("topic_id") or "")
        if topic_id not in state.topics:
            raise StateTransitionError("target topic does not exist")
        data["active_topic_id"] = topic_id
        data["topic_stack"] = [item for item in state.topic_stack if item != topic_id] + [topic_id]
        data["pending"] = None
    elif event == StateEvent.CANCEL_PENDING:
        data["pending"] = None
    elif event == StateEvent.ANSWER_PENDING:
        if state.pending is None:
            raise StateTransitionError("there is no pending clarification")
        if body.get("pending_id") != state.pending.pending_id:
            raise StateTransitionError("pending clarification identity mismatch")
        data["pending"] = None
    elif event == StateEvent.DATASET_INVALIDATED:
        dataset_id = str(body.get("dataset_id") or "")
        if dataset_id not in state.datasets:
            raise StateTransitionError("dataset does not exist")
        data["datasets"][dataset_id]["status"] = "INVALIDATED"
        data["datasets"][dataset_id]["invalidation_reason"] = str(
            body.get("reason") or "SEMANTIC_CHANGE"
        )
    elif event == StateEvent.REFRESH_TASK:
        task_id = str(body.get("task_id") or "")
        if task_id not in state.tasks:
            raise StateTransitionError("task does not exist")
        # REFRESH deliberately leaves semantic version and dataset identity untouched.
    elif event == StateEvent.REVISE_TASK:
        task_id = str(body.get("task_id") or "")
        task = state.tasks.get(task_id)
        if task is None:
            raise StateTransitionError("task does not exist")
        next_version = TaskVersion.model_validate(body["task_version"])
        if next_version.version <= task.active_version:
            raise StateTransitionError("REVISE must create a newer task version")
        task_data = data["tasks"][task_id]
        task_data["versions"].append(next_version.model_dump(mode="python"))
        task_data["active_version"] = next_version.version
        task_data["status"] = next_version.status
        if task.last_dataset_id:
            dataset = data["datasets"].get(task.last_dataset_id)
            if dataset:
                dataset["status"] = "INVALIDATED"
                dataset["invalidation_reason"] = "TASK_REVISED"
        data["pending"] = None
    else:
        raise StateTransitionError(f"event not implemented by minimal shadow skeleton: {event}")
    data["state_version"] = state.state_version + 1
    return ConversationState.model_validate(data)
