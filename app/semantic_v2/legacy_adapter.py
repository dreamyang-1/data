"""Loss-reporting adapter from V2 payloads to the legacy ASL 1.0 surface."""

from __future__ import annotations

from .enums import AdapterStatus
from .models import (
    AdapterReport,
    ComparisonPayload,
    DatasetTransformPayload,
    PlanEnvelope,
    RelationListPayload,
)


def assess_legacy_adapter(plan: PlanEnvelope) -> AdapterReport:
    """Classify semantic loss; this function never executes ASL or SQL."""

    dropped: list[str] = []
    approximated: list[str] = []
    post: list[str] = []
    errors: list[str] = []
    payload = plan.payload
    if isinstance(payload, ComparisonPayload):
        dropped.append("comparison")
    if isinstance(payload, RelationListPayload) and payload.relation_target is not None:
        approximated.append("relation_target")
    if isinstance(payload, DatasetTransformPayload):
        post.append("dataset_transform")
    if plan.result_contract is not None:
        approximated.append("result_contract")
    if plan.semantic_bindings:
        approximated.append("typed_semantic_roles")
    if dropped:
        status = AdapterStatus.LOSSY_UNSAFE
        errors.append("ASL 1.0 cannot represent all required semantics")
        safe = False
    elif post:
        status = AdapterStatus.LOSSY_COMPENSATED
        safe = True
    elif approximated:
        status = AdapterStatus.LOSSY_COMPENSATED
        safe = True
    else:
        status = AdapterStatus.LOSSLESS
        safe = True
    return AdapterReport(
        adapter_status=status,
        dropped_features=dropped,
        approximated_features=approximated,
        post_processing_required=post,
        validation_errors=errors,
        can_execute_safely=safe,
    )
