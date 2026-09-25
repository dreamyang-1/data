"""Adapt an authorized V2 logical plan into the internal semantic contract."""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.domain.models import ChatRequest, PrimaryIntent
from app.domain.semantic_decision import (
    SemanticDecision,
    SemanticDecisionField,
    SemanticDecisionFilter,
    SemanticDecisionSource,
    SemanticDecisionTask,
    SemanticDecisionTimeRange,
    SemanticScopeProof,
)

from .authorized_contract import ScopedArtifact
from .models import BoundSemanticRef
from .pipeline import AuthorizedLogicalPlan


_PAYLOAD_INTENTS: dict[str, PrimaryIntent] = {
    "CHAT": PrimaryIntent.CHAT,
    "SCALAR_AGGREGATE": PrimaryIntent.METRIC_QUERY,
    "GROUPED_AGGREGATE": PrimaryIntent.METRIC_QUERY,
    "TIME_SERIES": PrimaryIntent.TREND_ANALYSIS,
    "RANKING": PrimaryIntent.METRIC_QUERY,
    "COMPARISON": PrimaryIntent.COMPARISON_ANALYSIS,
    "RELATION_LIST": PrimaryIntent.DETAIL_QUERY,
    "DETAIL_ROWS": PrimaryIntent.DETAIL_QUERY,
    "METRIC_DEFINITION": PrimaryIntent.METRIC_DEFINITION,
    "LINEAGE": PrimaryIntent.DATA_LINEAGE,
    "DATA_QUALITY": PrimaryIntent.DATA_QUALITY,
    "ANOMALY": PrimaryIntent.ANOMALY_ANALYSIS,
    "ROOT_CAUSE": PrimaryIntent.ROOT_CAUSE_ANALYSIS,
    "FORECAST": PrimaryIntent.FORECAST_ANALYSIS,
    "REPORT_COMPOSITION": PrimaryIntent.REPORT_GENERATION,
    "CAPABILITY_HELP": PrimaryIntent.CAPABILITY_HELP,
    "OUT_OF_SCOPE": PrimaryIntent.OUT_OF_SCOPE,
}


def _field(ref: BoundSemanticRef) -> SemanticDecisionField:
    return SemanticDecisionField(
        catalog_type=str(getattr(ref.catalog_type, "value", ref.catalog_type)),
        canonical_id=ref.canonical_id,
        canonical_code=ref.canonical_code,
        display_name=ref.display_name,
        semantic_model_id=ref.semantic_model_id,
        catalog_version=ref.catalog_version,
        business_domain_ids=ref.business_domain_ids,
        resolution_source=ref.resolution_source,
    )


def _unique_fields(refs: Iterable[Any]) -> tuple[SemanticDecisionField, ...]:
    result: list[SemanticDecisionField] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if not isinstance(ref, BoundSemanticRef):
            continue
        key = (str(ref.catalog_type), ref.canonical_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(_field(ref))
    return tuple(result)


def _typed_filter_value(value: Any) -> Any:
    value_type = str(getattr(value, "value_type", "UNKNOWN"))
    if value_type == "ENTITY_REF":
        return value.ref.display_name
    if value_type == "LIST":
        return [_typed_filter_value(item) for item in value.values]
    if value_type == "RANGE":
        return {
            "start": _typed_filter_value(value.start),
            "end": _typed_filter_value(value.end),
        }
    if value_type == "NULL":
        return None
    scalar = getattr(value, "value", None)
    if hasattr(scalar, "isoformat"):
        return scalar.isoformat()
    if hasattr(scalar, "as_tuple"):
        return str(scalar)
    return scalar


def _filter_value_refs(value: Any) -> tuple[SemanticDecisionField, ...]:
    value_type = str(getattr(value, "value_type", "UNKNOWN"))
    if value_type == "ENTITY_REF":
        return (_field(value.ref),)
    if value_type == "LIST":
        return _unique_fields(
            ref
            for item in value.values
            for ref in (
                [item.ref]
                if str(getattr(item, "value_type", "")) == "ENTITY_REF"
                else []
            )
        )
    return ()


def _filters(expression: Any) -> tuple[tuple[SemanticDecisionFilter, ...], str | None]:
    if expression is None:
        return (), None
    node_type = str(getattr(expression, "node_type", ""))
    if node_type in {"PREDICATE", "ALIASED_PREDICATE"}:
        value_refs = _filter_value_refs(expression.value)
        if len(value_refs) > 1:
            return (), "V2_MULTI_ENTITY_FILTER_NOT_ADAPTED"
        return (
            SemanticDecisionFilter(
                field=_field(expression.field_ref),
                operator=str(getattr(expression.operator, "value", expression.operator)),
                value=_typed_filter_value(expression.value),
                value_type=str(expression.value.value_type),
                source=expression.source,
                value_refs=value_refs,
            ),
        ), None
    if node_type == "BOOLEAN_GROUP" and expression.operator == "AND":
        combined: list[SemanticDecisionFilter] = []
        for child in expression.children:
            values, reason = _filters(child)
            if reason:
                return (), reason
            combined.extend(values)
        return tuple(combined), None
    return (), "V2_FILTER_BOOLEAN_GROUP_NOT_ADAPTED"


def _comparison_type(payload: Any) -> str | None:
    comparison = getattr(payload, "comparison", None)
    if comparison is None:
        return None
    value = str(getattr(comparison.comparison_type, "value", comparison.comparison_type))
    if value == "YOY":
        return "同比"
    if value in {"MOM", "PERIOD_OVER_PERIOD"}:
        return "环比"
    baseline = getattr(comparison, "baseline", None)
    if str(getattr(baseline, "baseline_type", "")) in {
        "SEMANTIC", "TASK", "DATASET"
    }:
        return "对象间比较"
    return "指定时段对比"


def _safe_reason(reason: str | None, default: str) -> str:
    candidate = str(reason or "").strip().upper()
    match = re.search(r"[A-Z][A-Z0-9_:-]{2,199}", candidate)
    return match.group(0) if match else default


def _conversation_state(route: str, relation: str | None) -> str:
    if relation:
        return relation
    if route == "V1_EXECUTION_FALLBACK_NEW_TASK":
        return "NEW_TASK"
    if route.startswith("V2_CONTEXT_"):
        return "CONTEXT_RESOLVED"
    return "UNKNOWN"


def _task_from_plan(
    plan: AuthorizedLogicalPlan,
    *,
    completed_question: str,
) -> SemanticDecisionTask:
    payload = plan.payload
    payload_type = payload.payload_type
    intent = _PAYLOAD_INTENTS.get(payload_type)
    reasons: list[str] = []
    if intent is None:
        reasons.append(f"V2_PAYLOAD_{payload_type}_NOT_ADAPTED")

    filters, filter_reason = _filters(getattr(payload, "filters", None))
    if filter_reason:
        reasons.append(filter_reason)

    metrics = _unique_fields([
        *getattr(payload, "measures", ()),
        *getattr(payload, "metric_refs", ()),
    ])
    dimensions = _unique_fields([
        *getattr(payload, "group_by", ()),
        *getattr(payload, "decomposition_dimensions", ()),
        *(
            [payload.ranking_target]
            if isinstance(getattr(payload, "ranking_target", None), BoundSemanticRef)
            else []
        ),
    ])
    projection = getattr(payload, "projection_spec", None)
    fields = _unique_fields(
        item.ref for item in getattr(projection, "items", ())
    )
    business_objects = _unique_fields([
        *(
            getattr(payload, name)
            for name in (
                "subject", "source_entity", "target_entity", "relation_target",
                "quality_target", "delivery_target",
            )
            if isinstance(getattr(payload, name, None), BoundSemanticRef)
        ),
        *(
            [payload.lineage_target.ref]
            if isinstance(
                getattr(getattr(payload, "lineage_target", None), "ref", None),
                BoundSemanticRef,
            )
            else []
        ),
    ])
    if payload_type == "RELATION_LIST" and not fields:
        target = getattr(payload, "target_entity", None) or getattr(
            payload, "relation_target", None
        )
        fields = _unique_fields([target] if target is not None else [])

    time = getattr(payload, "time", None)
    time_range = None
    if time is not None:
        time_range = SemanticDecisionTimeRange(
            start=time.range.start if time.range is not None else None,
            end_exclusive=(
                time.range.end_exclusive if time.range is not None else None
            ),
            grain=str(getattr(time.grain, "value", time.grain)),
            timezone=time.timezone,
            source=time.source,
        )

    ranking = getattr(payload, "ranking", None)
    forecast_grain = getattr(payload, "horizon_grain", None)
    semantic_complete = not reasons and intent is not None
    return SemanticDecisionTask(
        task_id=plan.task_id,
        question=completed_question,
        intent=intent,
        intent_confidence=1.0 if semantic_complete else 0.0,
        payload_type=payload_type,
        metrics=metrics,
        dimensions=dimensions,
        fields=fields,
        filters=filters,
        time_range=time_range,
        business_objects=business_objects,
        ranking_limit=getattr(ranking, "limit", None),
        comparison_type=_comparison_type(payload),
        forecast_horizon_periods=getattr(payload, "horizon_periods", None),
        forecast_granularity=(
            str(getattr(forecast_grain, "value", forecast_grain))
            if forecast_grain is not None
            else None
        ),
        semantic_complete=semantic_complete,
        adaptation_reason=reasons[0] if reasons else None,
    )


def build_semantic_decision(
    *,
    chat: ChatRequest,
    completed_question: str,
    bridge_route: str,
    plan_state: ScopedArtifact | None,
    conversation_state: str | None,
    fallback_reason: str | None,
    catalog_version: str | None,
) -> SemanticDecision:
    """Build a scope-bound contract; unsupported shapes explicitly fall back."""

    scope = chat.authorized_semantic_scope
    plan: AuthorizedLogicalPlan | None = None
    if plan_state is not None:
        plan = AuthorizedLogicalPlan.model_validate(plan_state.payload)
        task = _task_from_plan(plan, completed_question=completed_question)
        plan_scope_matches = plan.permission_requirement.authorized_scope == scope
        artifact_context_matches = (
            plan_state.kind == "LAST_REQUEST"
            and plan_state.context == plan.permission_requirement
        )
        accepted = (
            task.semantic_complete
            and plan_scope_matches
            and artifact_context_matches
        )
        reason = (
            None
            if accepted
            else task.adaptation_reason
            or (
                "V2_PLAN_ARTIFACT_CONTEXT_MISMATCH"
                if not artifact_context_matches
                else "V2_PLAN_SCOPE_MISMATCH"
            )
        )
        source = (
            SemanticDecisionSource.V2_AUTHORIZED_PLAN
            if accepted
            else SemanticDecisionSource.V1_SEMANTIC_FALLBACK
        )
        status = "ACCEPTED" if accepted else "REQUIRES_V1_FALLBACK"
        proof = SemanticScopeProof(
            authorized_scope=scope,
            authorized_scope_fingerprint=scope.fingerprint(),
            plan_scope_fingerprint=plan.permission_requirement.fingerprint(),
            plan_semantic_fingerprint=plan.semantic_fingerprint,
            plan_id=plan.plan_id,
            catalog_version=plan.snapshot_requirement.catalog_version,
            semantic_model_version=(
                plan.snapshot_requirement.semantic_model_version
            ),
        )
    else:
        reason = _safe_reason(
            fallback_reason,
            "V2_CONTEXT_ONLY_REQUIRES_V1_SEMANTICS",
        )
        task = SemanticDecisionTask(
            task_id="task-1",
            question=completed_question,
            semantic_complete=False,
            adaptation_reason=reason,
        )
        source = SemanticDecisionSource.V1_SEMANTIC_FALLBACK
        status = "REQUIRES_V1_FALLBACK"
        proof = SemanticScopeProof(
            authorized_scope=scope,
            authorized_scope_fingerprint=scope.fingerprint(),
            catalog_version=catalog_version,
        )

    return SemanticDecision(
        source=source,
        status=status,
        message_id=chat.message_id,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        conversation_state=_conversation_state(bridge_route, conversation_state),
        original_question=chat.question,
        completed_question=completed_question,
        tasks=(task,),
        scope_proof=proof,
        fallback_reason=reason,
    )
