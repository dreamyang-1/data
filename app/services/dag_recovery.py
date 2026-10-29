"""Restore an idempotent request without replanning its completed tasks."""
import hashlib

from app.domain.models import TaskPlan


def recover_task_plan(checkpoint, question, scope):
    if not checkpoint or checkpoint.get('authorized_scope') != scope:
        return None
    if checkpoint.get('source_question') != question or not checkpoint.get('task_plan'):
        return None
    try:
        plan = TaskPlan.model_validate(checkpoint['task_plan'])
    except (TypeError, ValueError):
        return None
    fingerprint = hashlib.sha256(plan.model_dump_json().encode('utf8')).hexdigest()
    return plan if fingerprint == checkpoint.get('plan_fingerprint') else None
