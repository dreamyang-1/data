"""Cross-object validation helpers for semantic V2 plans."""

from __future__ import annotations

from .models import PlanEnvelope


def validate_plan(plan: PlanEnvelope) -> list[str]:
    """Return deterministic semantic validation errors for shadow comparison."""

    errors: list[str] = []
    accepted_ids = {
        candidate.canonical_id
        for candidate_set in plan.semantic_candidates
        for candidate in candidate_set.candidates
        if candidate.status.value == "ACCEPTED"
    }
    for binding in plan.semantic_bindings:
        if plan.semantic_candidates and binding.canonical_id not in accepted_ids:
            errors.append(f"binding not accepted by candidate set: {binding.canonical_id}")
    if plan.adapter_report and not plan.adapter_report.can_execute_safely:
        errors.append("legacy adapter reports unsafe semantic loss")
    return errors
