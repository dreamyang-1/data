"""Deterministic readiness of required task outputs, before child parsing."""
from collections.abc import Mapping, Sequence

from app.domain.models import AgentResponse


class TaskDependencySkipped(RuntimeError):
    """A downstream task did not run; this is not a missing user parameter."""


def confirmed_empty_result(response: AgentResponse) -> bool:
    """Use result evidence, never answer prose, preview length or a zero metric.

    A successful aggregate with a single value of zero still has one row.
    Missing/truncated previews do not prove the complete result is empty.
    """
    proofs = [item.payload for item in response.evidence if item.kind == 'QUERY_RESULT']
    if not proofs:
        proofs = [item.payload for item in response.evidence
                  if item.kind == 'ANALYSIS_RESULT' and 'row_count' in item.payload]
    if not proofs:
        return False
    for proof in proofs:
        if proof.get('truncated') or proof.get('total_row_count_confirmed') is False:
            return False
        count = proof.get('total_row_count', proof.get('row_count'))
        if type(count) is not int or count != 0:
            return False
        if any(type(proof.get(key)) is int and proof[key] > 0
               for key in ('row_count', 'returned_row_count')):
            return False
    return True


def dependency_skip(
    dependency_ids: Sequence[str], responses: Mapping[str, AgentResponse | Exception],
    labels: Mapping[str, str],
) -> TaskDependencySkipped | None:
    reasons = []
    for dependency_id in dependency_ids:
        value = responses.get(dependency_id)
        label = labels.get(dependency_id, dependency_id)
        if isinstance(value, TaskDependencySkipped):
            reasons.append(f'{label}因前置结果不可用已跳过')
        elif not isinstance(value, AgentResponse):
            reasons.append(f'{label}尚未完成' if value is None else f'{label}执行未成功')
        elif value.status == 'NEEDS_CLARIFICATION':
            reasons.append(f'{label}尚待补充信息，前置结果还未产生')
        elif value.status not in {'COMPLETED', 'PARTIAL_SUCCESS'}:
            reasons.append(f'{label}未成功完成')
        elif confirmed_empty_result(value):
            reasons.append(f'{label}已完成查询或处理，但在本次条件下返回0条结果')
    if not reasons:
        return None
    return TaskDependencySkipped(
        '；'.join(reasons) + '，无法提供本任务所需的前置结果，因此本任务未执行。'
        '无需为本任务单独补充条件；其他独立任务不受影响。'
    )
