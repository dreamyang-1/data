"""Auditable last gate for Legacy clarification; no raw business text in traces."""
import hashlib
import json

from app.domain.models import CanonicalAnalysisRequest, ClarificationDecisionTrace


USER_SLOTS = {
    'metric': 'METRIC_NAME', 'dimension': 'GROUPING_CHOICE', 'entity': 'ENTITY_NAME',
    'time_range': 'TIME_RANGE', 'comparison_type': 'COMPARISON_TYPE',
    'comparison_objects': 'ENTITY_NAMES', 'forecast_horizon': 'HORIZON',
    'forecast_history_range': 'TIME_RANGE', 'lineage_target': 'LINEAGE_TARGET',
    'product': 'ENTITY_NAME', 'task_answer_mapping': 'TASK_ANSWER_MAPPING',
}


def clarification_key(slot, candidates):
    return hashlib.sha256(json.dumps([slot, sorted(candidates)], ensure_ascii=False).encode()).hexdigest()


def semantic_question_identity(request: CanonicalAnalysisRequest, slot: str):
    """Hash only the displayed choice, its target and stable catalog identity.

    Scores, generated ambiguity IDs and question wording do not change the
    business decision. Other, not-yet-displayed choices must not affect its key.
    """
    ambiguity = next((a for a in request.semantic_ambiguities if a.blocking), None)
    if ambiguity is None:
        return clarification_key(slot, []), []
    identities = []
    for index, label in enumerate(ambiguity.candidates):
        detail = ambiguity.candidate_details[index] if index < len(ambiguity.candidate_details) else {}
        catalog = {key: detail[key] for key in (
            'metric_id', 'canonical_code', 'attribute_code', 'semantic_id',
            'canonical_name', 'attribute_name', 'entity_name', 'value',
            'business_domain_id', 'version', 'semantic_model_version',
        ) if detail.get(key) is not None}
        if not any(catalog.get(key) for key in ('metric_id', 'canonical_code', 'attribute_code', 'semantic_id')):
            catalog.update({key: detail[key] for key in ('id', 'candidate_id', 'record_id') if detail.get(key) is not None})
        encoded = json.dumps([label, catalog], ensure_ascii=False, sort_keys=True, default=str)
        identities.append('option-' + hashlib.sha256(encoded.encode()).hexdigest()[:20])
    context = [slot, ambiguity.type, ambiguity.phrase,
               sorted(ambiguity.affected_slots), ambiguity.semantic_model_id,
               ambiguity.semantic_model_version, sorted(identities)]
    key = hashlib.sha256(json.dumps(context, ensure_ascii=False).encode()).hexdigest()
    return key, identities


def restore_clarification_keys(previous: CanonicalAnalysisRequest, asked_keys: set[str]) -> set[str]:
    """Map an old union-of-labels key to the question actually shown in Pending.

    Use the prior snapshot, not the newly answered request: two distinct targets
    can have identical labels. No persisted schema or raw-text trace is added.
    """
    restored = set(asked_keys)
    options = sorted({c for a in previous.semantic_ambiguities if a.blocking for c in a.candidates})
    candidates = ['option-' + hashlib.sha256(c.encode()).hexdigest()[:20] for c in options]
    for slot in ('semantic_ambiguity', 'turn_relation'):
        if slot in previous.missing_slots and clarification_key(slot, candidates) in asked_keys:
            restored.add(semantic_question_identity(previous, slot)[0])
    return restored


def decide_clarification(request: CanonicalAnalysisRequest, slot: str, *, source_stage: str, asked_keys: set[str]):
    ambiguities = [a for a in request.semantic_ambiguities if a.blocking]
    semantic = slot in {'semantic_ambiguity', 'turn_relation'}
    options = ambiguities[0].candidates if semantic and ambiguities else []
    key, candidates = semantic_question_identity(request, slot) if semantic else (clarification_key(slot, []), [])
    already = key in asked_keys
    is_user = len(options) >= 2 if semantic else slot in USER_SLOTS
    reason = ('USER_REFERENCE_AMBIGUITY' if slot=='turn_relation' else 'USER_SEMANTIC_AMBIGUITY') if semantic else 'MISSING_USER_SLOT'
    if slot == 'task_answer_mapping':
        reason = 'USER_REFERENCE_AMBIGUITY'
    system_repair = False
    if slot == 'fields':
        reason, is_user = 'CATALOG_GOVERNANCE_GAP', False
    elif not is_user:
        reason = 'SYSTEM_FAILURE'
    # A supplied metric/field must never be asked for again as if absent.
    if slot == 'metric' and (request.metrics or request.lineage_target):
        reason, is_user, system_repair = 'SYSTEM_FAILURE', False, True
    if already:
        reason = 'REPEATED_QUESTION'
    safe_default = slot == 'time_range' and (request.time_range is not None or 'TIME_SCOPE=ALL_TIME' in request.assumptions)
    allowed = is_user and not system_repair and not already and not safe_default
    trace = ClarificationDecisionTrace(
        conversation_id=request.conversation_id, source_stage=source_stage, reason_type=reason,
        blocking_slot=slot, expected_answer_type='CANDIDATE_OPTION' if semantic else USER_SLOTS.get(slot,'NONE'),
        candidate_ids=candidates, already_asked=already, base_task_reference=request.analysis_thread_id or str(request.request_id),
        pending_reference=str(request.pending_state_version) if request.pending_state_version else None,
        evidence_codes=[reason, 'LEGACY_CLARIFICATION_GATE_V1'], is_user_ambiguity=is_user,
        system_repair_possible=system_repair, safe_default_available=safe_default, decision='ASK' if allowed else 'SUPPRESS',
    )
    return key, trace
