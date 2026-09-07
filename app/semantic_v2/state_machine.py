"""Versioned conversation/task state machine for shadow-only evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from .enums import TaskVersionStatus
from .models import (Identifier, StrictModel, ReadinessBlocker, TaskSemanticState,
                     ExecutionAttemptRecord, DatasetAncestry, ClarificationOption)
from .slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint


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
    topic_id: Identifier | None = None
    status: Literal['ACTIVE', 'SUSPENDED', 'RESOLVED', 'CANCELLED'] = 'ACTIVE'
    blockers: list['PendingBlocker'] = Field(default_factory=list)
    active_blocker_id: Identifier | None = None
    asked_slots: list[str] = Field(default_factory=list)
    clarification_rounds: int = Field(default=0, ge=0)
    last_question: str | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None

    @model_validator(mode='after')
    def pending_invariants(self):
        ids = [b.blocker_id for b in self.blockers]
        if len(set(ids)) != len(ids):
            raise ValueError('pending blocker IDs must be unique')
        if self.active_blocker_id is not None and self.active_blocker_id not in ids:
            raise ValueError('active pending blocker must exist')
        if self.blockers and not (self.topic_id and self.created_at and self.updated_at):
            raise ValueError('scoped pending requires topic and lifecycle timestamps')
        return self


class PendingBlocker(StrictModel):
    blocker_id: Identifier
    plan_path: Identifier
    reason_type: Literal['USER_AMBIGUITY'] = 'USER_AMBIGUITY'
    expected_answer_type: Literal['OPTION_ID', 'SEMANTIC_REF', 'TYPED_VALUE']
    candidate_ids: list[Identifier] = Field(default_factory=list)
    information_gain: float = Field(ge=0)
    already_asked: bool = False
    status: Literal['OPEN', 'RESOLVED'] = 'OPEN'
    options: list[ClarificationOption] = Field(default_factory=list)
    selected_option_id: Identifier | None = None

    @model_validator(mode='after')
    def selected_option(self):
        if self.expected_answer_type == 'OPTION_ID' and len(self.options) < 2:
            raise ValueError('semantic ambiguity requires at least two meaningful options')
        if len({o.option_id for o in self.options}) != len(self.options):
            raise ValueError('pending option IDs must be unique')
        if self.selected_option_id is not None and self.selected_option_id not in {o.option_id for o in self.options}:
            raise ValueError('pending selected option does not exist')
        return self


PendingRecord = PendingClarification


class DatasetState(StrictModel):
    """Immutable dataset reference associated with an executed task version."""

    dataset_id: Identifier
    task_id: Identifier
    task_version: int = Field(ge=1)
    status: Literal["VALID", "INVALIDATED"] = "VALID"
    invalidation_reason: str | None = Field(default=None, max_length=500)
    ancestry: DatasetAncestry | None = None


class TaskVersion(StrictModel):
    """One immutable semantic version of a task."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    version: int = Field(ge=1)
    status: TaskVersionStatus
    plan_id: Identifier | None = None
    semantics: TaskSemanticState = Field(default_factory=TaskSemanticState)
    current_turn_ref: Identifier | None = None
    current_turn_digest: Identifier | None = None
    created_at: datetime

    @model_validator(mode='after')
    def immutable_semantics(self):
        from .models import freeze_contract
        object.__setattr__(self, 'semantics', freeze_contract(self.semantics))
        return self

    @property
    def slot_snapshot(self):
        """Read-only 0.2 debug view. Core state is always TaskSemanticState."""
        return self.semantics.model_dump(exclude_defaults=True)


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
    clear_barriers: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def version_pointers(self):
        numbers = [v.version for v in self.versions]
        if len(numbers) != len(set(numbers)) or self.active_version not in numbers:
            raise ValueError('task version numbers must be unique and active version must exist')
        active = next(v for v in self.versions if v.version == self.active_version)
        if self.status != active.status:
            raise ValueError('task status must agree with active semantic version')
        if any(v is not None and v not in numbers for v in (self.last_executable_version, self.last_executed_version)):
            raise ValueError('task execution version pointer must exist')
        return self


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
    pending_records: dict[str, PendingClarification] = Field(default_factory=dict)
    datasets: dict[str, DatasetState] = Field(default_factory=dict)
    execution_attempts: dict[str, ExecutionAttemptRecord] = Field(default_factory=dict)
    applied_mutations: dict[str, str] = Field(default_factory=dict)
    applied_messages: dict[str, str] = Field(default_factory=dict)

    @property
    def pending(self):
        """Compatibility view scoped to the active topic."""
        return next((p for p in self.pending_records.values() if p.status == 'ACTIVE' and p.topic_id == self.active_topic_id), None)

    @model_validator(mode='after')
    def references_exist(self):
        if self.active_topic_id is not None and self.active_topic_id not in self.topics:
            raise ValueError('active topic does not exist')
        if len(set(self.topic_stack)) != len(self.topic_stack) or not set(self.topic_stack) <= set(self.topics):
            raise ValueError('topic stack contains duplicate or dangling reference')
        for key, topic in self.topics.items():
            if key != topic.topic_id or len(set(topic.task_ids)) != len(topic.task_ids) or not set(topic.task_ids) <= set(self.tasks):
                raise ValueError('topic/task identities are inconsistent')
            if topic.active_task_id is not None and topic.active_task_id not in topic.task_ids:
                raise ValueError('topic active task is not a member')
            if any(self.tasks[t].topic_id != key for t in topic.task_ids):
                raise ValueError('topic points to task belonging to another topic')
        for key, task in self.tasks.items():
            if key != task.task_id or task.topic_id not in self.topics or key not in self.topics[task.topic_id].task_ids:
                raise ValueError('task must belong to an existing topic')
            if task.last_dataset_id is not None and (task.last_dataset_id not in self.datasets or self.datasets[task.last_dataset_id].task_id != key):
                raise ValueError('task dataset pointer is dangling or belongs to another task')
            if task.parent_task_id is not None and task.parent_task_id not in self.tasks:
                raise ValueError('parent task does not exist')
            if not set(task.child_task_ids) <= set(self.tasks):
                raise ValueError('child task does not exist')
        for collection in (self.datasets, self.pending_records, self.execution_attempts):
            for key, item in collection.items():
                identity = getattr(item, 'dataset_id', None) if isinstance(item, DatasetState) else getattr(item, 'pending_id', None) if isinstance(item, PendingClarification) else item.execution_id
                if key != identity or item.task_id not in self.tasks or item.task_version not in {v.version for v in self.tasks[item.task_id].versions}:
                    raise ValueError('dataset/pending/execution reference is orphaned')
                if isinstance(item, PendingClarification):
                    if item.topic_id != self.tasks[item.task_id].topic_id:
                        raise ValueError('pending topic/task scope mismatch')
                    if item.status == 'ACTIVE' and (item.topic_id != self.active_topic_id or item.task_version != self.tasks[item.task_id].active_version):
                        raise ValueError('active pending must match current topic and task version')
                if isinstance(item, DatasetState) and item.ancestry:
                    a = item.ancestry
                    if a.dataset_id != key or a.source_task_id != item.task_id or a.source_task_version != item.task_version:
                        raise ValueError('dataset ancestry source mismatch')
                    if a.root_dataset_id not in self.datasets or not set(a.parent_dataset_ids) <= set(self.datasets):
                        raise ValueError('dataset ancestry has dangling references')
        attempts = [(e.task_id, e.task_version, e.attempt_number) for e in self.execution_attempts.values()]
        if len(set(attempts)) != len(attempts):
            raise ValueError('execution attempt numbers must be unique per semantic version')
        def walk_dataset(identifier, visiting):
            if identifier in visiting:
                raise ValueError('dataset ancestry cycle')
            item = self.datasets[identifier]
            if item.ancestry:
                for parent in item.ancestry.parent_dataset_ids:
                    parent_state = self.datasets[parent]
                    if parent_state.ancestry and parent_state.ancestry.root_dataset_id != item.ancestry.root_dataset_id:
                        raise ValueError('dataset ancestry roots disagree')
                    walk_dataset(parent, visiting | {identifier})
        for identifier in self.datasets:
            walk_dataset(identifier, set())
        return self


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
        _scope_pending(data, topic.topic_id)
    elif event in {StateEvent.SWITCH_TOPIC, StateEvent.RETURN_TOPIC}:
        topic_id = str(body.get("topic_id") or "")
        if topic_id not in state.topics:
            raise StateTransitionError("target topic does not exist")
        data["active_topic_id"] = topic_id
        data["topic_stack"] = [item for item in state.topic_stack if item != topic_id] + [topic_id]
        _scope_pending(data, topic_id)
    elif event == StateEvent.CANCEL_PENDING:
        if state.pending:
            data['pending_records'][state.pending.pending_id]['status'] = 'CANCELLED'
    elif event == StateEvent.ANSWER_PENDING:
        if state.pending is None:
            raise StateTransitionError("there is no pending clarification")
        if body.get("pending_id") != state.pending.pending_id:
            raise StateTransitionError("pending clarification identity mismatch")
        raise StateTransitionError('ANSWER_PENDING requires a typed StateMutation and readiness recheck')
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
        for pending in data['pending_records'].values():
            if pending['task_id'] == task_id and pending['status'] in {'ACTIVE', 'SUSPENDED'}:
                pending['status'] = 'SUSPENDED'
    else:
        raise StateTransitionError(f"event not implemented by minimal shadow skeleton: {event}")
    data["state_version"] = state.state_version + 1
    return ConversationState.model_validate(data)


def _scope_pending(data, target_topic_id):
    for pending in data['pending_records'].values():
        if pending['status'] in {'RESOLVED', 'CANCELLED'}:
            continue
        task = data['tasks'][pending['task_id']]
        pending['status'] = ('ACTIVE' if pending['topic_id'] == target_topic_id and
                             pending['task_version'] == task['active_version'] else 'SUSPENDED')


class PendingPatch(StrictModel):
    pending_id: Identifier
    action: Literal['CREATE', 'ANSWER', 'CANCEL']
    record: PendingRecord | None = None
    selected_option_id: Identifier | None = None
    remaining_blockers: list[ReadinessBlocker] = Field(default_factory=list)


class PointerUpdates(StrictModel):
    active_topic_id: Identifier | None = None
    active_task_id: Identifier | None = None


class StateMutation(StrictModel):
    mutation_id: Identifier
    message_id: Identifier
    turn_id: Identifier
    task_id: Identifier
    expected_state_version: int = Field(ge=0)
    base_task_version: int = Field(ge=1)
    task_patch: TaskPatch
    pending_patch: PendingPatch | None = None
    dataset_invalidations: list[Identifier] = Field(default_factory=list)
    pointer_updates: PointerUpdates = Field(default_factory=PointerUpdates)
    execution_attempt: ExecutionAttemptRecord | None = None
    created_at: AwareDatetime


def apply_state_mutation(state: ConversationState, mutation: StateMutation) -> ConversationState:
    """One pure CAS transition, with idempotency checked before stale-version rejection."""
    import hashlib
    import json
    digest_data = mutation.model_dump(mode='json', exclude={'mutation_id', 'expected_state_version'})
    digest = hashlib.sha256(json.dumps(digest_data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for recorded in (state.applied_mutations.get(mutation.mutation_id), state.applied_messages.get(mutation.message_id)):
        if recorded is not None:
            if recorded != digest:
                raise StateTransitionError('mutation/message identity reused with different content')
            return state
    if mutation.expected_state_version != state.state_version:
        raise StateTransitionError('conversation state version conflict')
    task = state.tasks.get(mutation.task_id)
    if task is None or task.active_version != mutation.base_task_version or mutation.task_patch.base_task_version != mutation.base_task_version:
        raise StateTransitionError('task version conflict')
    active = next(v for v in task.versions if v.version == task.active_version)
    reduced = apply_task_patch(active.semantics, mutation.task_patch, clear_barriers=task.clear_barriers)
    data = state.model_dump(mode='python')
    changed_task = data['tasks'][task.task_id]
    changed_task['clear_barriers'] = reduced.clear_barriers
    if reduced.changed:
        for version in changed_task['versions']:
            if version['version'] == task.active_version:
                version['status'] = TaskVersionStatus.SUPERSEDED
        new_version = max(v.version for v in task.versions) + 1
        version = TaskVersion(version=new_version, status=TaskVersionStatus.RESOLVED,
                              semantics=reduced.semantics, created_at=mutation.created_at,
                              current_turn_ref=mutation.turn_id)
        changed_task['versions'].append(version.model_dump())
        changed_task['active_version'] = new_version
        changed_task['status'] = version.status
    invalidations = set(mutation.dataset_invalidations)
    if reduced.dataset_invalidated:
        invalidations |= {d.dataset_id for d in state.datasets.values() if d.task_id == task.task_id}
    for identifier in invalidations:
        if identifier not in data['datasets'] or data['datasets'][identifier]['task_id'] != task.task_id:
            raise StateTransitionError('dataset invalidation outside target task')
        data['datasets'][identifier].update(status='INVALIDATED', invalidation_reason='SEMANTIC_CHANGE')
    if mutation.pending_patch is not None:
        patch = mutation.pending_patch
        if patch.action == 'CREATE':
            if patch.record is None or patch.pending_id in data['pending_records'] or patch.record.pending_id != patch.pending_id or patch.record.task_id != task.task_id:
                raise StateTransitionError('invalid pending creation')
            if not patch.record.blockers:
                raise StateTransitionError('pending requires real ambiguity blockers')
            data['pending_records'][patch.pending_id] = patch.record.model_dump()
        else:
            pending = data['pending_records'].get(patch.pending_id)
            if pending is None or pending['task_id'] != task.task_id or pending['task_version'] != task.active_version or pending['status'] != 'ACTIVE':
                raise StateTransitionError('pending answer does not match active task/version')
            if patch.action == 'CANCEL':
                pending['status'] = 'CANCELLED'
            else:
                selected = next((b for b in pending['blockers'] if b['blocker_id'] == pending['active_blocker_id']), None)
                if selected is None or patch.selected_option_id not in {o['option_id'] for o in selected['options']}:
                    raise StateTransitionError('answer must select a known option ID')
                chosen = next(o for o in selected['options'] if o['option_id'] == patch.selected_option_id)
                slot = selected['plan_path'].removeprefix('semantics.')
                from .registries import SlotDefinitionRegistry
                SlotDefinitionRegistry.get(slot)
                actual = getattr(reduced.semantics, slot)
                expected = chosen['canonical_ref'] or chosen['typed_value']
                values = actual if isinstance(actual, (list, tuple)) else [actual]
                if not any(semantic_fingerprint({'value': v}) == semantic_fingerprint({'value': expected}) for v in values):
                    raise StateTransitionError('task patch does not apply the selected option value')
                selected['selected_option_id'] = patch.selected_option_id
                selected['status'] = 'RESOLVED'
                # Recheck every existing blocker against the materialized typed state.
                # A caller-provided empty blocker list cannot silently clear unanswered slots.
                remaining_ids = set()
                for blocker in pending['blockers']:
                    slot = blocker['plan_path'].removeprefix('semantics.')
                    SlotDefinitionRegistry.get(slot)
                    actual = getattr(reduced.semantics, slot)
                    values = actual if isinstance(actual, (list, tuple)) else [actual]
                    choices = [o['canonical_ref'] or o['typed_value'] for o in blocker['options']]
                    if not choices or not any(semantic_fingerprint({'value': v}) == semantic_fingerprint({'value': expected}) for v in values for expected in choices):
                        remaining_ids.add(blocker['blocker_id'])
                remaining_ids |= {b.blocker_id for b in patch.remaining_blockers if b.blocker_type.value == 'USER_AMBIGUITY' and b.user_action_required}
                for blocker in pending['blockers']:
                    blocker['status'] = 'OPEN' if blocker['blocker_id'] in remaining_ids else 'RESOLVED'
                if remaining_ids - {b['blocker_id'] for b in pending['blockers']}:
                    raise StateTransitionError('new ambiguities require explicit pending options')
                pending['status'] = 'ACTIVE' if remaining_ids else 'RESOLVED'
                pending['task_version'] = changed_task['active_version']
                available = [b for b in pending['blockers'] if b['status'] == 'OPEN' and not b['already_asked']]
                next_blocker = min(available, key=lambda b: (-b['information_gain'], b['blocker_id'])) if available else None
                pending['active_blocker_id'] = next_blocker['blocker_id'] if next_blocker else None
                if next_blocker:
                    next_blocker['already_asked'] = True
                    pending['asked_slots'].append(next_blocker['plan_path'])
                pending['clarification_rounds'] += 1
                pending['updated_at'] = mutation.created_at
    pointers = mutation.pointer_updates
    if pointers.active_topic_id is not None:
        if pointers.active_topic_id not in data['topics']:
            raise StateTransitionError('target topic does not exist')
        data['active_topic_id'] = pointers.active_topic_id
        data['topic_stack'] = [i for i in data['topic_stack'] if i != pointers.active_topic_id] + [pointers.active_topic_id]
    if pointers.active_task_id is not None:
        topic = data['topics'][data['active_topic_id']]
        if pointers.active_task_id not in topic['task_ids']:
            raise StateTransitionError('active task is outside target topic')
        topic['active_task_id'] = pointers.active_task_id
    _scope_pending(data, data['active_topic_id'])
    if mutation.execution_attempt:
        attempt = mutation.execution_attempt
        prior = [e for e in state.execution_attempts.values() if e.task_id == task.task_id and e.task_version == changed_task['active_version']]
        if (attempt.execution_id in data['execution_attempts'] or attempt.task_id != task.task_id or
                attempt.task_version != changed_task['active_version'] or attempt.attempt_number != len(prior) + 1):
            raise StateTransitionError('invalid execution attempt identity/version/sequence')
        data['execution_attempts'][attempt.execution_id] = attempt.model_dump()
    data['applied_mutations'][mutation.mutation_id] = digest
    data['applied_messages'][mutation.message_id] = digest
    data['recent_turn_ids'] = [*data['recent_turn_ids'], mutation.turn_id][-100:]
    data['state_version'] += 1
    return ConversationState.model_validate(data)


PendingClarification.model_rebuild()
