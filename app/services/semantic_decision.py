"""Safety validation and V1 request materialization for SemanticDecision."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.domain.models import (
    AtomicTask,
    CanonicalAnalysisRequest,
    ChatRequest,
    ConversationControl,
    IntentCandidate,
    MetricRef,
    SemanticFilterBinding,
    TimeRange,
    TaskPlan,
    TrustedIdentity,
)
from app.domain.semantic_decision import (
    SemanticDecision,
    SemanticDecisionField,
    SemanticDecisionFilter,
    SemanticDecisionSource,
    SemanticDecisionTask,
    SemanticDecisionTimeRange,
    SemanticScopeProof,
)
from app.intent.classifier import RuleBasedIntentClassifier
from app.services.authorized_scope import bind_authorized_scope


_GRAIN_ASSUMPTIONS = {
    "DAY": "DEFAULT_TIME_GRANULARITY=day",
    "WEEK": "DEFAULT_TIME_GRANULARITY=week",
    "MONTH": "DEFAULT_TIME_GRANULARITY=month",
    "QUARTER": "DEFAULT_TIME_GRANULARITY=quarter",
    "YEAR": "DEFAULT_TIME_GRANULARITY=year",
}


def _business_object(task: SemanticDecisionTask) -> str | None:
    if task.payload_type == "RELATION_LIST" and len(task.business_objects) >= 2:
        return task.business_objects[-1].display_name
    if task.payload_type == "DETAIL_ROWS" and task.business_objects:
        return task.business_objects[0].display_name
    if task.business_objects:
        return task.business_objects[-1].display_name
    return None


def canonical_request_from_semantic_decision(
    decision: SemanticDecision,
    *,
    chat: ChatRequest,
    identity: TrustedIdentity,
    rules: RuleBasedIntentClassifier,
) -> tuple[CanonicalAnalysisRequest | None, str | None]:
    """Return one executable V1 request or an explicit safe fallback reason."""

    mismatch = decision.request_mismatch_reason(
        message_id=chat.message_id,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        completed_question=chat.question,
        authorized_scope=chat.authorized_semantic_scope,
    )
    if mismatch:
        return None, mismatch

    task = decision.tasks[0]
    if task.intent is None:
        return None, "SEMANTIC_DECISION_INTENT_MISSING"

    request = rules.classify(chat.question, identity, chat.conversation_id)
    request.primary_intent = task.intent
    request.secondary_intents = list(task.secondary_intents)
    request.operators = rules._operators(task.intent, chat.question)
    request.metrics = [
        MetricRef(
            input=item.display_name,
            metric_id=(
                f"{chat.authorized_semantic_scope.semantic_model_id}:"
                f"{item.canonical_code}"
            ),
            canonical_name=item.display_name,
        )
        for item in task.metrics
    ]
    request.dimensions = [item.display_name for item in task.dimensions]
    request.fields = [item.display_name for item in task.fields]
    request.entity = _business_object(task)
    request.filters = [
        {
            "field": item.field.display_name,
            "operator": item.operator,
            "value": item.value,
        }
        for item in task.filters
    ]
    semantic_bindings: list[SemanticFilterBinding] = []
    for index, item in enumerate(task.filters):
        if len(item.value_refs) != 1:
            continue
        value_ref = item.value_refs[0]
        domains = [int(value) for value in item.field.business_domain_ids]
        semantic_bindings.append(SemanticFilterBinding(
            filter_index=index,
            input_value=value_ref.display_name,
            canonical_value=value_ref.display_name,
            canonical_name=item.field.display_name,
            attribute_code=str(item.field.canonical_code),
            record_id=value_ref.canonical_id,
            score=1.0,
            business_domain_id=domains[0] if len(domains) == 1 else None,
            semantic_model_version=(
                decision.scope_proof.semantic_model_version
            ),
        ))
    request.semantic_filter_bindings = semantic_bindings
    if task.time_range is not None:
        if task.time_range.start is not None:
            request.time_range = TimeRange(
                start=task.time_range.start.date(),
                end_exclusive=task.time_range.end_exclusive.date(),
                timezone=task.time_range.timezone,
            )
        else:
            request.assumptions.append("TIME_SCOPE=ALL_AVAILABLE_HISTORY")
        grain = _GRAIN_ASSUMPTIONS.get(task.time_range.grain.upper())
        if grain:
            request.assumptions.append(grain)
    request.ranking_limit = task.ranking_limit
    request.comparison_type = task.comparison_type
    request.forecast_horizon_periods = task.forecast_horizon_periods
    request.forecast_granularity = (
        task.forecast_granularity.lower()
        if task.forecast_granularity
        else None
    )
    request.forecast_history_provided = bool(
        task.payload_type == "FORECAST"
        and task.time_range is not None
        and task.time_range.start is not None
    )
    request.conversation_control = ConversationControl.NEW_REQUEST
    request.intent_source = "V2_SEMANTIC_DECISION"
    request.intent_confidence = task.intent_confidence
    request.intent_candidates = [
        IntentCandidate(
            intent=task.intent,
            confidence=task.intent_confidence,
            evidence=[],
        )
    ]
    request.original_question = decision.original_question
    request.rewritten_question = decision.completed_question
    request.assumptions = list(dict.fromkeys([
        *request.assumptions,
        "SEMANTIC_DECISION_CONSUMED",
        f"V2_PAYLOAD_TYPE={task.payload_type}",
    ]))
    # The authorized V2 task already owns the query shape. Re-applying V1
    # business-shape heuristics here could overwrite bound metrics or turn a
    # relation list into a different request, which would defeat the contract.
    rules.sanitize_semantic_entity_mentions(request)
    request.missing_slots = rules.required_missing_slots(request)
    if request.missing_slots:
        return None, "SEMANTIC_DECISION_V1_REQUIRED_SLOT_MISMATCH"

    bind_authorized_scope(
        request,
        chat.authorized_semantic_scope,
        execution_resolved_business_domain_ids=(
            chat._demo_execution_resolved_business_domain_ids
        ),
    )
    request.application_id = chat.application_id
    return request, None


def _unbound_field(name: str, catalog_type: str) -> SemanticDecisionField:
    return SemanticDecisionField(
        catalog_type=catalog_type,
        display_name=name,
        binding_status="UNBOUND",
    )


def _fallback_task(
    task: AtomicTask,
    request: CanonicalAnalysisRequest,
) -> SemanticDecisionTask:
    time_range = None
    if request.time_range is not None:
        zone = ZoneInfo(request.time_range.timezone)
        time_range = SemanticDecisionTimeRange(
            start=datetime.combine(request.time_range.start, time.min, tzinfo=zone),
            end_exclusive=datetime.combine(
                request.time_range.end_exclusive, time.min, tzinfo=zone
            ),
            source="V1_PRELIMINARY_RULE",
        )
    filters = tuple(
        SemanticDecisionFilter(
            field=_unbound_field(str(item.get("field") or "unknown"), "ATTRIBUTE"),
            operator=str(item.get("operator") or "EQ"),
            value=item.get("value"),
            value_type="UNBOUND",
            source="V1_PRELIMINARY_RULE",
        )
        for item in request.filters
        if item.get("field")
    )
    return SemanticDecisionTask(
        task_id=task.task_id,
        question=task.question,
        intent=request.primary_intent,
        secondary_intents=tuple(request.secondary_intents),
        intent_confidence=request.intent_confidence,
        metrics=tuple(_unbound_field(item.input, "METRIC") for item in request.metrics),
        dimensions=tuple(
            _unbound_field(item, "DIMENSION") for item in request.dimensions
        ),
        fields=tuple(_unbound_field(item, "ATTRIBUTE") for item in request.fields),
        filters=filters,
        time_range=time_range,
        business_objects=(
            (_unbound_field(request.entity, "ENTITY"),)
            if request.entity
            else ()
        ),
        depends_on=tuple(task.depends_on),
        semantic_complete=False,
        adaptation_reason="MULTI_TASK_REQUIRES_V1_SEMANTIC_CLASSIFICATION",
    )


def semantic_decision_for_task_plan(
    *,
    chat: ChatRequest,
    plan: TaskPlan,
    preliminary_requests: list[CanonicalAnalysisRequest],
) -> SemanticDecision:
    """Record a V1 decomposition without promoting unbound rules to authority."""

    if len(plan.tasks) != len(preliminary_requests):
        raise ValueError("task plan and preliminary semantic requests are not aligned")
    prior = chat._semantic_decision
    proof = (
        prior.scope_proof
        if isinstance(prior, SemanticDecision)
        and prior.scope_proof.authorized_scope == chat.authorized_semantic_scope
        else SemanticScopeProof(
            authorized_scope=chat.authorized_semantic_scope,
            authorized_scope_fingerprint=(
                chat.authorized_semantic_scope.fingerprint()
            ),
        )
    )
    return SemanticDecision(
        source=SemanticDecisionSource.V1_SEMANTIC_FALLBACK,
        status="REQUIRES_V1_FALLBACK",
        message_id=chat.message_id,
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        conversation_state=(
            prior.conversation_state
            if isinstance(prior, SemanticDecision)
            else "UNKNOWN"
        ),
        original_question=(
            prior.original_question
            if isinstance(prior, SemanticDecision)
            else chat.question
        ),
        completed_question=chat.question,
        tasks=tuple(
            _fallback_task(task, request)
            for task, request in zip(plan.tasks, preliminary_requests, strict=True)
        ),
        scope_proof=proof,
        fallback_reason="MULTI_TASK_REQUIRES_V1_SEMANTIC_CLASSIFICATION",
    )


def semantic_decision_with_v1_fallback(
    decision: SemanticDecision,
    reason: str,
) -> SemanticDecision:
    """Demote an unadaptable contract while preserving its audit fields."""

    tasks = tuple(
        task.model_copy(update={
            "semantic_complete": False,
            "adaptation_reason": reason,
        })
        for task in decision.tasks
    )
    return SemanticDecision(
        source=SemanticDecisionSource.V1_SEMANTIC_FALLBACK,
        status="REQUIRES_V1_FALLBACK",
        message_id=decision.message_id,
        conversation_id=decision.conversation_id,
        application_id=decision.application_id,
        conversation_state=decision.conversation_state,
        original_question=decision.original_question,
        completed_question=decision.completed_question,
        tasks=tasks,
        scope_proof=decision.scope_proof,
        clarification_needed=decision.clarification_needed,
        clarification_reason=decision.clarification_reason,
        fallback_reason=reason,
    )
