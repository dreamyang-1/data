"""Schema, reducer, state, clarification, and adapter tests for Phase 2.5."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError

from app.semantic_v2.clarification import decide_clarification
from app.semantic_v2.enums import (
    AdapterStatus,
    AnalysisGoal,
    CatalogType,
    DialogueAct,
    ErrorType,
    Presence,
    ProofStatus,
    QueryShape,
    ResolutionStatus,
    SemanticRole,
    ServiceRoute,
    SlotOperationType,
    TaskVersionStatus,
    TimeGrain,
)
from app.semantic_v2.legacy_adapter import assess_legacy_adapter
from app.semantic_v2.models import (
    AdapterReport,
    ChatPayload,
    ClarificationDecision,
    ComparisonPayload,
    ComparisonSpec,
    ContractProof,
    ControlPayload,
    DatasetTransformPayload,
    DialogueSpec,
    GroupedAggregatePayload,
    LineagePayload,
    LimitSpec,
    Mention,
    PermissionContext,
    PlanEnvelope,
    Provenance,
    RankingPayload,
    RankingSpec,
    RelationListPayload,
    ResultContract,
    RowBounds,
    SemanticAxes,
    SemanticRef,
    SlotOperation,
    SnapshotContext,
    TimeRange,
    TimeSeriesPayload,
    TimeSpec,
    TopicTarget,
    VersionMetadata,
)
from app.semantic_v2.result_contract import completed_allowed, prove_result_contract
from app.semantic_v2.schema import draft_2020_12_schema
from app.semantic_v2.slot_reducer import SlotReducerError, TaskSlotState, apply_slot_operations
from app.semantic_v2.state_machine import (
    ConversationState,
    DatasetState,
    StateEvent,
    StateTransitionError,
    TaskState,
    TaskVersion,
    TopicState,
    apply_state_event,
)
from app.semantic_v2.trace_models import TraceStage, TraceV2


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def ref(
    code: str,
    role: SemanticRole,
    catalog_type: CatalogType = CatalogType.METRIC,
    *,
    domain: str = "205",
) -> SemanticRef:
    """Build a scope-valid canonical reference."""

    return SemanticRef(
        catalog_type=catalog_type,
        semantic_role=role,
        canonical_id=f"id:{code}",
        canonical_code=code,
        display_name=code,
        catalog_version="catalog:1",
        semantic_model_id="81",
        business_domain_ids=[domain],
        resolution_status=ResolutionStatus.ACCEPTED,
        resolution_source="FIXTURE",
    )


def time_spec(grain: TimeGrain = TimeGrain.MONTH) -> TimeSpec:
    """Build a deterministic time fixture."""

    return TimeSpec(
        anchor=ref("transaction_date", SemanticRole.TIME_FIELD, CatalogType.DIMENSION),
        range=TimeRange(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_exclusive=datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
        grain=grain,
        timezone="Asia/Shanghai",
        source="USER_EXPLICIT",
        as_of=NOW,
    )


def plan(payload: object | None = None, **updates: object) -> PlanEnvelope:
    """Build a minimal, valid envelope for cross-object tests."""

    values: dict[str, object] = {
        "plan_id": "plan:1",
        "conversation_id": "conversation:1",
        "message_id": "message:1",
        "topic_id": "topic:1",
        "task_id": "task:1",
        "task_version": 1,
        "conversation_state_version": 0,
        "dialogue": DialogueSpec(act=DialogueAct.NEW_TASK),
        "topic_target": TopicTarget(topic_id="topic:1", task_id="task:1"),
        "semantics": SemanticAxes(
            service_route=ServiceRoute.CHAT,
            analysis_goals=[],
            query_shape=QueryShape.CHAT_ONLY,
        ),
        "payload": payload or ChatPayload(),
        "permission_context": PermissionContext(
            tenant_id="tenant:1",
            user_id="user:1",
            application_id="app:1",
            allowed_business_domain_ids=["205"],
        ),
        "snapshot_context": SnapshotContext(
            catalog_version="catalog:1", semantic_model_version="semantic:1"
        ),
        "version_metadata": VersionMetadata(
            prompt_version="prompt:1", policy_version="policy:1", adapter_version="adapter:1"
        ),
        "provenance": Provenance(
            current_turn_text="你好", current_turn_id="turn:1", source="MOCK"
        ),
    }
    values.update(updates)
    return PlanEnvelope.model_validate(values)


def operation(
    kind: SlotOperationType,
    *,
    value: object = None,
    presence: Presence = Presence.PRESENT,
    version: int = 0,
) -> SlotOperation:
    """Build a reducer operation fixture."""

    return SlotOperation(
        operation_id=f"op:{kind.value}",
        slot_path="metrics",
        operation=kind,
        new_value=value,
        evidence_mention_ids=[],
        source="CURRENT_EXPLICIT",
        reason_code="TEST",
        base_task_version=version,
        presence=presence,
    )


def conversation() -> ConversationState:
    """Build a versioned conversation containing one task and dataset."""

    version = TaskVersion(
        version=1, status=TaskVersionStatus.EXECUTED, created_at=NOW
    )
    task = TaskState(
        task_id="task:1",
        topic_id="topic:1",
        active_version=1,
        status=TaskVersionStatus.EXECUTED,
        versions=[version],
        last_executable_version=1,
        last_executed_version=1,
        last_dataset_id="dataset:1",
    )
    topic = TopicState(
        topic_id="topic:1",
        title="销售",
        active_task_id="task:1",
        task_ids=["task:1"],
        last_accessed_at=NOW,
    )
    return ConversationState(
        conversation_id="conversation:1",
        tenant_id="tenant:1",
        user_id="user:1",
        application_id="app:1",
        state_version=1,
        active_topic_id="topic:1",
        topic_stack=["topic:1"],
        topics={"topic:1": topic},
        tasks={"task:1": task},
        datasets={
            "dataset:1": DatasetState(
                dataset_id="dataset:1", task_id="task:1", task_version=1
            )
        },
    )


def test_schema_passes_draft_2020_12_metaschema() -> None:
    schema = draft_2020_12_schema(PlanEnvelope)
    Draft202012Validator.check_schema(schema)


def test_schema_is_generated_from_pydantic() -> None:
    generated = draft_2020_12_schema(PlanEnvelope)
    assert generated["properties"] == PlanEnvelope.model_json_schema()["properties"]


def test_chat_payload_has_no_query_requirements() -> None:
    assert ChatPayload().payload_type == "CHAT"


def test_control_payload_has_no_query_requirements() -> None:
    assert ControlPayload(control_action=DialogueAct.CANCEL).payload_type == "CONTROL"


def test_time_series_without_measure_fails() -> None:
    with pytest.raises(ValidationError):
        TimeSeriesPayload(group_by=[ref("product", SemanticRole.GROUP_BY)], time=time_spec())


def test_time_series_without_grain_fails() -> None:
    with pytest.raises(ValidationError):
        TimeSeriesPayload(
            measures=[ref("sales", SemanticRole.MEASURE)],
            group_by=[ref("date", SemanticRole.GROUP_BY)],
            time=time_spec(TimeGrain.NONE),
        )


def test_ranking_without_rank_by_fails() -> None:
    with pytest.raises(ValidationError):
        RankingSpec(direction="DESC", limit=5)


def test_ranking_without_limit_fails() -> None:
    with pytest.raises(ValidationError):
        RankingSpec(rank_by=ref("sales", SemanticRole.ORDER_BY), direction="DESC")


def test_relation_list_without_target_fails() -> None:
    with pytest.raises(ValidationError):
        RelationListPayload(
            source_entity=ref("product", SemanticRole.SOURCE_ENTITY, CatalogType.ENTITY)
        )


def test_dataset_transform_without_source_fails() -> None:
    with pytest.raises(ValidationError):
        DatasetTransformPayload(operation="LIMIT", limit=LimitSpec(limit=5))


def test_lineage_does_not_require_metric_or_time() -> None:
    payload = LineagePayload(
        lineage_target=ref("sales", SemanticRole.SOURCE_ENTITY, CatalogType.ENTITY)
    )
    assert payload.direction == "BOTH"


def test_clear_requires_explicitly_cleared_presence() -> None:
    with pytest.raises(ValidationError):
        operation(SlotOperationType.CLEAR)


def test_unknown_plan_field_is_rejected() -> None:
    data = plan().model_dump(mode="python")
    data["unknown"] = True
    with pytest.raises(ValidationError):
        PlanEnvelope.model_validate(data)


def test_empty_canonical_id_is_rejected() -> None:
    data = ref("sales", SemanticRole.MEASURE).model_dump(mode="python")
    data["canonical_id"] = ""
    with pytest.raises(ValidationError):
        SemanticRef.model_validate(data)


def test_mention_span_mismatch_fails() -> None:
    mention = Mention(
        mention_id="mention:1",
        surface="销售",
        normalized_surface="销售",
        start_char=0,
        end_char=2,
        candidate_roles=[SemanticRole.MEASURE],
        source_turn_id="turn:1",
    )
    with pytest.raises(ValidationError):
        plan(mentions=[mention])


def test_lossy_unsafe_adapter_cannot_execute() -> None:
    with pytest.raises(ValidationError):
        AdapterReport(adapter_status=AdapterStatus.LOSSY_UNSAFE, can_execute_safely=True)


def test_unsupported_comparison_is_not_silently_dropped() -> None:
    payload = ComparisonPayload(
        measures=[ref("sales", SemanticRole.MEASURE)],
        group_by=[ref("product", SemanticRole.GROUP_BY, CatalogType.DIMENSION)],
        comparison=ComparisonSpec(
            comparison_type="YOY",
            baseline="previous_year",
            calculation="RATE",
            output_metrics=[ref("sales", SemanticRole.MEASURE)],
        ),
    )
    report = assess_legacy_adapter(plan(payload=payload))
    assert report.adapter_status == AdapterStatus.LOSSY_UNSAFE
    assert "comparison" in report.dropped_features


def test_result_contract_missing_column_fails() -> None:
    proof = prove_result_contract(
        ResultContract(required_columns=["销售额"]), columns=["商品"], rows=[], truncated=False
    )
    assert proof.status == ProofStatus.FAIL


def test_add_preserves_existing_metrics() -> None:
    state = TaskSlotState(task_version=0, slots={})
    first = apply_slot_operations(state, [operation(SlotOperationType.SET, value=["m1"])])
    second = apply_slot_operations(first, [operation(SlotOperationType.ADD, value="m2", version=1)])
    assert second.slots["metrics"].value == ["m1", "m2"]


def test_replace_removes_same_slot_metrics() -> None:
    state = TaskSlotState(task_version=0, slots={})
    first = apply_slot_operations(state, [operation(SlotOperationType.SET, value=["m1", "m2"])])
    second = apply_slot_operations(
        first, [operation(SlotOperationType.REPLACE, value=["m3"], version=1)]
    )
    assert second.slots["metrics"].value == ["m3"]


def test_clear_blocks_later_inheritance() -> None:
    state = TaskSlotState(task_version=0, slots={})
    cleared = apply_slot_operations(
        state,
        [operation(SlotOperationType.CLEAR, presence=Presence.EXPLICITLY_CLEARED)],
    )
    inherited = apply_slot_operations(
        cleared, [operation(SlotOperationType.INHERIT, value=["m1"], version=1)]
    )
    assert inherited.slots["metrics"].presence == Presence.EXPLICITLY_CLEARED


def test_new_topic_does_not_inherit_old_filter() -> None:
    old = conversation()
    next_state = apply_state_event(
        old,
        event=StateEvent.NEW_TOPIC,
        expected_state_version=1,
        payload={
            "topic": TopicState(
                topic_id="topic:2", title="库存", last_accessed_at=NOW
            ).model_dump(mode="python"),
            "task": TaskState(
                task_id="task:2",
                topic_id="topic:2",
                active_version=1,
                status=TaskVersionStatus.PROVISIONAL,
                versions=[TaskVersion(version=1, status=TaskVersionStatus.PROVISIONAL, created_at=NOW)],
            ).model_dump(mode="python"),
        },
    )
    assert next_state.tasks["task:2"].versions[0].slot_snapshot == {}


def test_revise_invalidates_semantic_dataset() -> None:
    next_state = apply_state_event(
        conversation(),
        event=StateEvent.REVISE_TASK,
        expected_state_version=1,
        payload={
            "task_id": "task:1",
            "task_version": TaskVersion(
                version=2, status=TaskVersionStatus.PROVISIONAL, created_at=NOW
            ).model_dump(mode="python"),
        },
    )
    assert next_state.datasets["dataset:1"].status == "INVALIDATED"


def test_user_ambiguity_and_system_failure_are_distinct() -> None:
    user = decide_clarification(
        reason_type=ErrorType.USER_AMBIGUITY,
        affected_plan_paths=["payload.measures"],
        candidate_answers=["销售额", "销售量"],
        asked_slots=[],
        base_task_version=1,
        state_version=1,
    )
    system = decide_clarification(
        reason_type=ErrorType.SURFACE_PARSE_FAILURE,
        affected_plan_paths=["payload.measures"],
        candidate_answers=[],
        asked_slots=[],
        base_task_version=1,
        state_version=1,
    )
    assert user.create_pending is True
    assert system.create_pending is False


@pytest.mark.parametrize(
    "reason",
    [ErrorType.SURFACE_PARSE_FAILURE, ErrorType.RESULT_CONTRACT_FAILURE],
)
def test_system_failures_do_not_create_pending(reason: ErrorType) -> None:
    decision = decide_clarification(
        reason_type=reason,
        affected_plan_paths=["payload"],
        candidate_answers=[],
        asked_slots=[],
        base_task_version=1,
        state_version=1,
    )
    assert decision.create_pending is False


def test_cross_tenant_semantic_scope_is_rejected() -> None:
    binding = ref("sales", SemanticRole.MEASURE, domain="999")
    with pytest.raises(ValidationError):
        plan(semantic_bindings=[binding])


def test_state_version_conflict_is_rejected() -> None:
    with pytest.raises(StateTransitionError):
        apply_state_event(
            conversation(), event=StateEvent.REFRESH_TASK,
            expected_state_version=0, payload={"task_id": "task:1"}
        )


def test_applicable_and_administrative_department_are_distinct() -> None:
    applicable = ref(
        "applicable_department", SemanticRole.RELATION_TARGET, CatalogType.DIMENSION
    )
    administrative = ref(
        "department", SemanticRole.GROUP_BY, CatalogType.DIMENSION
    )
    assert applicable.canonical_id != administrative.canonical_id
    assert applicable.semantic_role != administrative.semantic_role


def test_first_five_rows_and_top_five_are_different_plans() -> None:
    rows = DatasetTransformPayload(
        source_dataset_id="dataset:1", operation="LIMIT", limit=LimitSpec(limit=5)
    )
    ranked = RankingPayload(
        measures=[ref("sales", SemanticRole.MEASURE)],
        group_by=[ref("product", SemanticRole.GROUP_BY, CatalogType.DIMENSION)],
        ranking_target=ref("product", SemanticRole.GROUP_BY, CatalogType.DIMENSION),
        ranking=RankingSpec(
            rank_by=ref("sales", SemanticRole.ORDER_BY), direction="DESC", limit=5
        ),
    )
    assert rows.payload_type == "DATASET_TRANSFORM"
    assert ranked.payload_type == "RANKING"


def test_slot_reducer_enforces_cas_version() -> None:
    with pytest.raises(SlotReducerError):
        apply_slot_operations(
            TaskSlotState(task_version=2),
            [operation(SlotOperationType.SET, value=["m1"], version=1)],
        )


def test_refresh_preserves_task_version() -> None:
    refreshed = apply_state_event(
        conversation(), event=StateEvent.REFRESH_TASK,
        expected_state_version=1, payload={"task_id": "task:1"}
    )
    assert refreshed.tasks["task:1"].active_version == 1


def test_completed_requires_all_proofs_to_pass() -> None:
    passed = ContractProof(status=ProofStatus.PASS)
    unknown = ContractProof(status=ProofStatus.UNKNOWN)
    assert completed_allowed(passed, passed, passed, passed)
    assert not completed_allowed(passed, unknown, passed, passed)


def test_trace_rejects_sensitive_payload() -> None:
    with pytest.raises(ValidationError):
        TraceV2(
            trace_id="trace:1",
            span_id="span:1",
            conversation_id="conversation:1",
            message_id="message:1",
            stage=TraceStage.REQUEST_RECEIVED,
            timestamp=NOW,
            duration_ms=1,
            input_digest="a" * 64,
            output_digest="b" * 64,
            sanitized_payload={"password": "secret"},
        )
