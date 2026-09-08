"""Small deterministic guards for the existing Legacy execution path."""
from __future__ import annotations

from typing import Mapping

from app.domain.models import CanonicalAnalysisRequest, ConversationControl, PrimaryIntent


PRODUCT_CLEAR_BARRIER = "FILTER_INHERITANCE_BARRIER=product"


def product_filter_clear_requested(question: str) -> bool:
    """Recognize a complete condition-clear command, never a noun substring."""
    compact = "".join(question.split()).rstrip("。？！?!")
    return compact in {
        prefix + role
        for prefix in ("不限", "不限制") for role in ("产品", "商品")
    } | {
        prefix + role + suffix
        for prefix in ("去掉", "移除", "取消", "删除")
        for role in ("产品", "商品")
        for suffix in ("条件", "筛选", "过滤", "筛选条件", "过滤条件")
    }


def update_product_clear_barrier(
    request: CanonicalAnalysisRequest,
    current: CanonicalAnalysisRequest,
    question: str,
    *,
    explicit_filters: list[dict] | None = None,
) -> None:
    """Only the current explicit product edit can change the barrier."""
    from app.services.turn_admission import TurnAdmissionGate

    if product_filter_clear_requested(question):
        if PRODUCT_CLEAR_BARRIER not in request.assumptions:
            request.assumptions.append(PRODUCT_CLEAR_BARRIER)
        request.asl_template = None
        request.source_dataset_id = None
    elif any(
        TurnAdmissionGate._semantic_field_family(str(item.get("field") or "")) == "product"
        for item in (explicit_filters if explicit_filters is not None else current.filters)
    ):
        request.assumptions = [a for a in request.assumptions if a != PRODUCT_CLEAR_BARRIER]


def apply_product_clear_barrier(request: CanonicalAnalysisRequest) -> None:
    """Remove a cleared product predicate and its evidence after state restores.

    Brand/category/manufacturer conditions and grouping/projection are separate
    roles. Existing semantic bindings can identify a product-name attribute by
    its governed canonical label; no physical table or column is guessed here.
    """
    if PRODUCT_CLEAR_BARRIER not in request.assumptions:
        return
    from app.services.turn_admission import TurnAdmissionGate

    def product_field(field):
        return TurnAdmissionGate._semantic_field_family(str(field or "")) == "product"

    product_codes = {binding.attribute_code for binding in request.semantic_filter_bindings
                     if product_field(binding.canonical_name)}
    def is_product(item):
        return product_field(item.get("field")) or item.get("field") in product_codes

    def values(items):
        return {str(value) for item in items
                for value in (item.get("value") if isinstance(item.get("value"), list) else [item.get("value")])
                if value is not None}

    removed = [item for item in request.filters if is_product(item)]
    old_values = values(removed)
    if request.turn_admission is not None:
        old_values |= values([item for item in request.turn_admission.context_before.get("filters", []) if is_product(item)])
    index_map = {index: new_index for new_index, index in enumerate(
        index for index, item in enumerate(request.filters) if not is_product(item))}
    bindings = []
    for binding in request.semantic_filter_bindings:
        if product_field(binding.canonical_name) or binding.filter_index not in index_map:
            old_values.update((binding.input_value, binding.canonical_value))
        else:
            bindings.append(binding.model_copy(update={"filter_index": index_map[binding.filter_index]}))
    request.filters = [item for item in request.filters if not is_product(item)]
    request.semantic_filter_bindings = bindings
    retained_values = values(request.filters)
    request.semantic_entity_mentions = [value for value in request.semantic_entity_mentions
                                        if value not in old_values or value in retained_values]
    if removed:
        request.asl_template = None
        request.source_dataset_id = None


def lineage_target_present(request: CanonicalAnalysisRequest) -> bool:
    return bool(request.metrics or request.fields or request.entity or request.source_dataset_id or request.lineage_target)


def apply_catalog_display_default(request: CanonicalAnalysisRequest, policy: Mapping) -> bool:
    """Use only a versioned, scope-resolved catalog policy supplied internally.

    Neither the intent model nor user text can supply this policy. Production
    callers must obtain it from an already authorized semantic asset snapshot.
    """
    if request.primary_intent != PrimaryIntent.DETAIL_QUERY or request.fields:
        return False
    fields = policy.get('default_display_attributes')
    allowed = policy.get('allowed_attributes')
    if (policy.get('entity') != request.entity or not policy.get('version')
            or policy.get('version') == 'current' or not isinstance(fields, list)
            or not fields or not isinstance(allowed, list)
            or not all(isinstance(x, str) and x.strip() and x in allowed for x in fields)):
        return False
    request.fields = list(dict.fromkeys(fields))
    request.missing_slots = [s for s in request.missing_slots if s != 'fields']
    return True


def pending_answer_admissibility(current: CanonicalAnalysisRequest, pending: CanonicalAnalysisRequest, *, self_contained: bool) -> str:
    """Readiness alone never binds an utterance to an old pending request."""
    if self_contained:
        return 'NEW_TASK'
    expected = set(pending.missing_slots)
    if 'comparison_objects' in expected:
        from app.intent.classifier import RuleBasedIntentClassifier
        if RuleBasedIntentClassifier._comparison_object_candidates(current.original_question, None):
            return 'ANSWER_PENDING'
    if current.primary_intent in {PrimaryIntent.CHAT, PrimaryIntent.CAPABILITY_HELP, PrimaryIntent.OUT_OF_SCOPE}:
        return 'NEW_TASK'
    if (('time_range' in expected and current.time_range is not None
         and 'DEFAULT_TIME_RANGE=LATEST_ONE_YEAR' not in current.assumptions)
            or ('metric' in expected and current.metrics)
            or ('dimension' in expected and current.dimensions)
            or ('entity' in expected and current.entity)
            or ('fields' in expected and current.fields)
            or ('comparison_type' in expected and current.comparison_type)
            or ('forecast_horizon' in expected and current.forecast_horizon_periods)):
        return 'ANSWER_PENDING'
    text = current.original_question.strip().rstrip('。？！?!')
    for ambiguity in pending.semantic_ambiguities:
        number_labels = ['一','二','三','四','五','六','七','八','九','十']
        for index in range(len(ambiguity.candidates)):
            if text in {str(index + 1), number_labels[index], '第' + str(index + 1) + '个', '第' + number_labels[index] + '个'}:
                return 'ANSWER_PENDING'
        if text in ambiguity.candidates:
            return 'ANSWER_PENDING'
        if text and len([c for c in ambiguity.candidates if c.startswith(text)]) == 1:
            return 'ANSWER_PENDING'
        if any(text == str(d.get('id') or d.get('candidate_id') or '') for d in ambiguity.candidate_details):
            return 'ANSWER_PENDING'
    if current.conversation_control in {ConversationControl.CORRECTION, ConversationControl.CANCEL}:
        return 'ANSWER_PENDING'
    return 'UNBOUND'


def apply_snapshot_display_default(request: CanonicalAnalysisRequest) -> bool:
    snapshot = request.semantic_context_snapshot
    if snapshot is None or snapshot.semantic_model_id != request.semantic_model_id:
        return False
    for asset in snapshot.assets:
        if asset.asset_type == 'ENTITY' and asset.canonical_name == request.entity:
            return apply_catalog_display_default(request, {
                **asset.metadata, 'entity': asset.canonical_name, 'version': asset.version,
                'allowed_attributes': [a.canonical_name for a in snapshot.assets if a.asset_type == 'FIELD'],
            })
    return False


def apply_region_clear_barrier(request: CanonicalAnalysisRequest) -> None:
    # Existing restore boundaries also enforce the product clear marker. Keep
    # the region field/schema and legacy entry point compatible.
    apply_product_clear_barrier(request)
    if 'region' in request.cleared_filter_families:
        from app.services.turn_admission import TurnAdmissionGate
        request.filters = [f for f in request.filters if TurnAdmissionGate._semantic_field_family(str(f.get('field') or '')) != 'region']
