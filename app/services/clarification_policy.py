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


def decide_clarification(request: CanonicalAnalysisRequest, slot: str, *, source_stage: str, asked_keys: set[str]):
    ambiguities = [a for a in request.semantic_ambiguities if a.blocking]
    options = sorted({c for a in ambiguities for c in a.candidates}) if slot in {'semantic_ambiguity','turn_relation'} else []
    candidates = ['option-' + hashlib.sha256(c.encode()).hexdigest()[:20] for c in options]
    key = clarification_key(slot, candidates)
    already = key in asked_keys
    semantic = slot in {'semantic_ambiguity', 'turn_relation'}
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
