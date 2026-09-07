"""Pure clarification policy for Phase 2.5 shadow evaluation."""

from __future__ import annotations

from .enums import ErrorType
from .models import ClarificationDecision


def decide_clarification(
    *,
    reason_type: ErrorType,
    affected_plan_paths: list[str],
    candidate_answers: list[str],
    asked_slots: list[str],
    base_task_version: int,
    state_version: int,
    information_gain: float = 0.0,
    fallback_available: bool = False,
    assumption_available: bool = False,
) -> ClarificationDecision:
    """Create a decision while preventing system failures from becoming prompts."""

    affected = list(dict.fromkeys(affected_plan_paths))
    already_asked = bool(set(affected) & set(asked_slots))
    create_pending = reason_type == ErrorType.USER_AMBIGUITY and bool(affected) and not already_asked
    return ClarificationDecision(
        reason_type=reason_type,
        blocking=reason_type not in {ErrorType.NO_DATA, ErrorType.DATA_NOT_READY},
        affected_plan_paths=affected,
        candidate_answers=list(dict.fromkeys(candidate_answers)),
        information_gain=information_gain,
        already_asked=already_asked,
        asked_slots=list(dict.fromkeys(asked_slots)),
        fallback_available=fallback_available,
        assumption_available=assumption_available,
        base_task_version=base_task_version,
        state_version=state_version,
        create_pending=create_pending,
    )
