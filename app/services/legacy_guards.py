"""Small deterministic guards for the existing Legacy execution path."""
from __future__ import annotations

from typing import Mapping

from app.domain.models import CanonicalAnalysisRequest, ConversationControl, PrimaryIntent


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
    if 'region' in request.cleared_filter_families:
        from app.services.turn_admission import TurnAdmissionGate
        request.filters = [f for f in request.filters if TurnAdmissionGate._semantic_field_family(str(f.get('field') or '')) != 'region']
