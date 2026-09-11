"""Lossless bridge from one finalized V2 scalar plan to the existing query API.

The V2 task state remains the only conversation authority.  This module does
not classify or merge a turn; it only serializes an already authorized plan
into the legacy ``CanonicalAnalysisRequest`` consumed by Oagnet and SQL
Translator.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ChatRequest,
    ContextMode,
    ConversationControl,
    DataQueryResult,
    MetricRef,
    PrimaryIntent,
    SemanticFilterBinding,
    TimeRange,
    TrustedIdentity,
    TurnRelation,
)
from app.services.intent_asl_contract import (
    build_intent_asl_contract,
    validate_intent_asl_contract_definition,
)

from . import models as m
from .authorized_contract import contract_digest
from .completed_question import CompletedQuestionDisplay
from .isolated_execution import PreparedExecution, proof, require
from .pipeline import AuthorizedLogicalPlan
from .result_contract import completed_allowed, prove_result_contract


_RELATION = {
    "NEW_TASK": (TurnRelation.STANDALONE_NEW_TOPIC, ContextMode.NONE,
                 ConversationControl.NEW_REQUEST),
    "MODIFY": (TurnRelation.CURRENT_TOPIC_MODIFICATION, ContextMode.CURRENT_THREAD,
               ConversationControl.CORRECTION),
    "FOLLOW_UP": (TurnRelation.CURRENT_TOPIC_FOLLOWUP, ContextMode.CURRENT_THREAD,
                  ConversationControl.FOLLOW_UP),
    "RETURN_TO_TOPIC": (TurnRelation.HISTORICAL_TOPIC_RETURN, ContextMode.HISTORICAL_THREAD,
                        ConversationControl.FOLLOW_UP),
    "ANSWER_CLARIFICATION": (TurnRelation.CLARIFICATION_RESPONSE,
                             ContextMode.CLARIFICATION_RESUME,
                             ConversationControl.CLARIFICATION_RESPONSE),
    # ContextProposal represents relationship and task operation together. The
    # legacy request exposes them separately. V2 has already applied the exact
    # operation before this current-task boundary is serialized.
    "ADD": (TurnRelation.CURRENT_TOPIC_MODIFICATION, ContextMode.CURRENT_THREAD,
            ConversationControl.CORRECTION),
    "REPLACE": (TurnRelation.CURRENT_TOPIC_MODIFICATION, ContextMode.CURRENT_THREAD,
                ConversationControl.CORRECTION),
    "REMOVE": (TurnRelation.CURRENT_TOPIC_MODIFICATION, ContextMode.CURRENT_THREAD,
               ConversationControl.CORRECTION),
    "CLEAR": (TurnRelation.CURRENT_TOPIC_MODIFICATION, ContextMode.CURRENT_THREAD,
              ConversationControl.CORRECTION),
    "CORRECT": (TurnRelation.CURRENT_TOPIC_MODIFICATION, ContextMode.CURRENT_THREAD,
                ConversationControl.CORRECTION),
    "CONTINUE": (TurnRelation.CURRENT_TOPIC_FOLLOWUP, ContextMode.CURRENT_THREAD,
                 ConversationControl.FOLLOW_UP),
    "DRILL_DOWN": (TurnRelation.CURRENT_TOPIC_FOLLOWUP, ContextMode.CURRENT_THREAD,
                   ConversationControl.FOLLOW_UP),
}


def _raw_value(value):
    if isinstance(value, m.EntityValueRef):
        return value.ref.display_name
    if isinstance(value, m.EnumValue):
        return value.value
    if isinstance(value, m.NullValue):
        return None
    if isinstance(value, m.ListValue):
        return [_raw_value(item) for item in value.values]
    if isinstance(value, m.RangeValue):
        return [_raw_value(value.start), _raw_value(value.end)]
    raw = value.value
    if isinstance(raw, (date, datetime, Decimal)):
        return raw.isoformat() if hasattr(raw, "isoformat") else str(raw)
    return raw


def _predicates(expression) -> list[m.Predicate]:
    if expression is None:
        return []
    if isinstance(expression, (m.Predicate, m.AliasedPredicate)):
        return [expression]
    require(
        isinstance(expression, m.BooleanFilterGroup)
        and expression.operator == "AND",
        "V2_CANONICAL_FILTER_EXPRESSION_UNSUPPORTED",
    )
    result: list[m.Predicate] = []
    for child in expression.children:
        require(
            isinstance(child, (m.Predicate, m.AliasedPredicate)),
            "V2_CANONICAL_FILTER_EXPRESSION_UNSUPPORTED",
        )
        result.append(child)
    return result


def build_canonical_analysis_request(
    *,
    chat: ChatRequest,
    identity: TrustedIdentity,
    plan: AuthorizedLogicalPlan,
    display: CompletedQuestionDisplay,
) -> CanonicalAnalysisRequest:
    """Serialize exactly the semantic state used by the displayed question."""

    require(plan.payload.payload_type == "SCALAR_AGGREGATE",
            "EXECUTION_PAYLOAD_UNSUPPORTED")
    require(display.plan_id == plan.plan_id
            and display.task_id == plan.task_id
            and display.task_version == plan.task_version
            and display.semantic_fingerprint == plan.semantic_fingerprint,
            "V2_CANONICAL_DISPLAY_PLAN_MISMATCH")
    require(plan.permission_requirement.authorized_scope == chat.authorized_semantic_scope,
            "EXECUTION_SCOPE_PIN_MISMATCH")
    relation, context_mode, control = _RELATION.get(
        display.relation,
        (None, None, None),
    )
    require(relation is not None, "V2_CANONICAL_TURN_RELATION_UNSUPPORTED")

    metrics = [
        MetricRef(
            input=item.display_name,
            metric_id=f"{chat.semantic_model_id}:{item.canonical_code}",
            canonical_name=item.display_name,
        )
        for item in plan.payload.measures
    ]
    require(bool(metrics), "V2_CANONICAL_METRIC_REQUIRED")

    filters: list[dict] = []
    bindings: list[SemanticFilterBinding] = []
    for index, predicate in enumerate(_predicates(plan.payload.filters)):
        value = _raw_value(predicate.value)
        filters.append({
            "field": predicate.field_ref.display_name,
            "operator": predicate.operator.value,
            "value": value,
        })
        if isinstance(predicate.value, m.EntityValueRef):
            value_ref = predicate.value.ref
            require(
                value_ref.resolution_source.startswith("VERIFIED_SOURCE_"),
                "V2_CANONICAL_SOURCE_VALUE_PROOF_REQUIRED",
            )
            domains = [
                int(item) for item in predicate.field_ref.business_domain_ids
                if str(item).isascii() and str(item).isdecimal()
            ]
            domain = domains[0] if len(domains) == 1 else None
            bindings.append(SemanticFilterBinding(
                filter_index=index,
                input_value=value_ref.display_name,
                canonical_value=value_ref.display_name,
                canonical_name=predicate.field_ref.display_name,
                attribute_code=predicate.field_ref.canonical_code,
                record_id=value_ref.canonical_id,
                score=1.0,
                business_domain_id=domain,
                semantic_model_version=plan.snapshot_requirement.semantic_model_version,
            ))

    time_range = None
    if plan.payload.time is not None and plan.payload.time.range is not None:
        zone_range = plan.payload.time.range
        zone = ZoneInfo(plan.payload.time.timezone)
        time_range = TimeRange(
            start=zone_range.start.astimezone(zone).date(),
            end_exclusive=zone_range.end_exclusive.astimezone(zone).date(),
            timezone=plan.payload.time.timezone,
        )

    operators = [AnalysisOperator.AGGREGATE]
    if filters:
        operators.append(AnalysisOperator.FILTER)
    request = CanonicalAnalysisRequest(
        authorized_semantic_scope=chat.authorized_semantic_scope,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        turn_relation=relation,
        model_turn_relation=relation,
        context_mode=context_mode,
        original_question=chat.question,
        rewritten_question=display.completed_question,
        rewrite_context_applied=(display.relation != "NEW_TASK"),
        primary_intent=PrimaryIntent.METRIC_QUERY,
        conversation_control=control,
        operators=operators,
        metrics=metrics,
        entity=(plan.payload.subject.display_name
                if plan.payload.subject is not None else None),
        filters=filters,
        semantic_filter_bindings=bindings,
        time_range=time_range,
        missing_slots=[],
        assumptions=[
            "V2_FINAL_TASK_STATE_AUTHORITY",
            "V2_QUERY_SHAPE=SCALAR_AGGREGATE",
            f"V2_PLAN_FINGERPRINT={plan.semantic_fingerprint}",
            f"COMPLETED_QUESTION_DIGEST={display.display_digest}",
        ],
        intent_source="V2_FINAL_TASK_STATE",
        semantic_model_id=chat.semantic_model_id,
        semantic_model_version=plan.snapshot_requirement.semantic_model_version,
        database_id=chat.database_id,
        business_domain_ids=list(chat.business_domain_ids),
        resolved_business_domain_ids=list(chat.business_domain_ids),
        business_domain_selection_mode=(
            "EXPLICIT_DOMAINS" if chat.business_domain_ids else "MODEL_WIDE"
        ),
        knowledge_base_names=list(chat.knowledge_base_names),
    )
    errors = validate_intent_asl_contract_definition(
        build_intent_asl_contract(request)
    )
    require(not errors, "V2_CANONICAL_INTENT_ASL_CONTRACT_INVALID")
    return request


def validate_canonical_analysis_request(
    *,
    actual: CanonicalAnalysisRequest,
    chat: ChatRequest,
    identity: TrustedIdentity,
    plan: AuthorizedLogicalPlan,
    display: CompletedQuestionDisplay,
) -> None:
    """Reject any drift between construction and execution of the bridge."""

    expected = build_canonical_analysis_request(
        chat=chat,
        identity=identity,
        plan=plan,
        display=display,
    )

    def material(value: CanonicalAnalysisRequest) -> dict:
        result = value.model_dump(mode="json")
        result.pop("request_id", None)
        return result

    require(
        material(actual) == material(expected),
        "V2_CANONICAL_REQUEST_PLAN_MISMATCH",
    )


def validate_canonical_query_result(
    *,
    prepared: PreparedExecution,
    request: CanonicalAnalysisRequest,
    outcome: DataQueryResult,
) -> tuple[dict, m.ContractProof, str, str]:
    """Validate the original query-chain receipt against the V2 scalar plan."""

    plan = prepared.plan
    require(
        request.authorized_semantic_scope
        == plan.permission_requirement.authorized_scope,
        "EXECUTION_SCOPE_PIN_MISMATCH",
    )
    require(
        [item.metric_id for item in request.metrics]
        == [
            f"{plan.permission_requirement.authorized_scope.semantic_model_id}:"
            f"{item.canonical_code}"
            for item in plan.payload.measures
        ],
        "V2_CANONICAL_REQUEST_PLAN_MISMATCH",
    )
    require(outcome.data_source_id is not None
            and str(outcome.data_source_id) == str(prepared.sql_receipt["data_source_id"]),
            "EXECUTION_RESULT_DATA_SOURCE_MISMATCH")
    expected_codes = [item.canonical_code for item in plan.payload.measures]
    asl_metrics = outcome.asl.get("metrics")
    require(isinstance(asl_metrics, list), "EXECUTION_RESULT_ASL_INVALID")
    expected_subject = (
        plan.payload.subject.canonical_code
        if plan.payload.subject is not None
        else str((prepared.lowering.asl or {}).get("subject", {}).get("entity") or "")
    )
    actual_subject = outcome.asl.get("subject")
    actual_subject = (
        str(actual_subject.get("entity") or "")
        if isinstance(actual_subject, dict) else str(actual_subject or "")
    )
    require(bool(expected_subject) and actual_subject == expected_subject,
            "EXECUTION_RESULT_ASL_SUBJECT_MISMATCH")
    by_code = {
        str(item.get("name")): item
        for item in asl_metrics
        if isinstance(item, dict) and item.get("name")
    }
    require(
        len(asl_metrics) == len(expected_codes)
        and set(by_code) == set(expected_codes),
        "EXECUTION_RESULT_ASL_METRIC_MISMATCH",
    )

    dataset = outcome.dataset
    require(
        dataset.quality_status == "PASS"
        and not dataset.truncated
        and dataset.row_count == 1
        and dataset.total_row_count == 1
        and len(dataset.rows) == 1,
        "EXECUTION_RESULT_COMPLETENESS_UNPROVEN",
    )
    columns = list(dataset.columns)
    row = dataset.rows[0]
    require(
        set(row) == set(columns) and len(columns) == len(expected_codes),
        "EXECUTION_RESULT_COMPLETENESS_UNPROVEN",
    )

    required_by_code = {
        item.semantic_ref.canonical_code: item
        for item in prepared.lowering.result_contract.required_outputs
        if item.semantic_ref.catalog_type.value == "METRIC"
    }
    bindings: list[m.OutputBindingProof] = []
    for code in expected_codes:
        metric = by_code[code]
        alias = str(metric.get("alias") or code)
        require(alias in columns and code in required_by_code,
                "EXECUTION_RESULT_OUTPUT_BINDING_MISMATCH")
        value = row[alias]
        require(
            value is None
            or type(value) in (int, float, Decimal)
            and (value.is_finite() if isinstance(value, Decimal) else value == value and abs(value) != float("inf")),
            "EXECUTION_RESULT_MEASURE_TYPE_MISMATCH",
        )
        output = required_by_code[code]
        bindings.append(m.OutputBindingProof(
            output_field_id=output.output_field_id,
            asl_projection_id=f"metrics:{code}",
            sql_alias=alias,
            result_column_index=columns.index(alias),
            result_column_name=alias,
            status="PASS",
            semantic_fingerprint=plan.semantic_fingerprint,
        ))

    result_proof = prove_result_contract(
        prepared.lowering.result_contract,
        columns=columns,
        rows=dataset.rows,
        truncated=dataset.truncated,
        output_bindings=bindings,
        snapshot_id=dataset.snapshot_id,
    )
    require(completed_allowed(result_proof), "EXECUTION_RESULT_CONTRACT_FAILURE")
    result = {
        "success": True,
        "columns": columns,
        "data": dataset.rows,
        "row_count": dataset.row_count,
        "snapshot_id": dataset.snapshot_id,
        "data_as_of": dataset.data_as_of.isoformat(),
        "quality_status": dataset.quality_status,
        "truncated": False,
    }
    return (
        result,
        result_proof,
        contract_digest(outcome.asl),
        contract_digest({"sql": outcome.sql}),
    )
