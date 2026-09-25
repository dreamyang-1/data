"""Pure clarification policy for Phase 2.5 shadow evaluation."""

from __future__ import annotations

from .enums import ErrorType
from .models import ClarificationDecision, ReadinessBlocker, SystemRepairDecision, TerminalDecision


def authorized_clarification_options(options, *, snapshot, permission, authorizations):
    """Filter before rendering labels so denied candidates cannot leak through prompts."""
    from .pipeline import validate_bound_ref_scope_and_permission
    allowed = []
    for option in options:
        try:
            validate_bound_ref_scope_and_permission(option, snapshot, permission, authorizations)
        except ValueError:
            continue
        allowed.append(option)
    return allowed


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
) -> ClarificationDecision | SystemRepairDecision | TerminalDecision:
    """Create a decision while preventing system failures from becoming prompts."""

    affected = list(dict.fromkeys(affected_plan_paths))
    if reason_type != ErrorType.USER_AMBIGUITY:
        if reason_type in {ErrorType.NO_DATA, ErrorType.PERMISSION_DENIED, ErrorType.DATA_NOT_READY}:
            return TerminalDecision(reason_type=reason_type)
        return SystemRepairDecision(blockers=[ReadinessBlocker(
            blocker_id='legacy-decision', plan_path=affected[0] if affected else 'plan',
            blocker_type=reason_type, source_stage='PLAN', system_repairable=True,
            message_code=reason_type.value,
        )])
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
