"""Deterministic public wording for one finalized V2 semantic task.

The renderer consumes only the accepted task state and its authorized logical
plan.  Its output is presentation evidence: it is never parsed back into a
task, ASL, SQL, or an authorization decision.
"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from . import models as m
from .authorized_contract import contract_digest
from .pipeline import AuthorizedLogicalPlan
from .state_machine import ConversationState


class CompletedQuestionDisplay(m.StrictModel):
    source: str = "FINAL_TASK_STATE_AND_AUTHORIZED_LOGICAL_PLAN"
    message_id: m.Identifier
    task_id: m.Identifier
    task_version: int
    plan_id: m.Identifier
    semantic_fingerprint: m.Identifier
    relation: str
    understanding: str
    completed_question: str
    display_digest: m.Identifier

    @property
    def public_message(self) -> str:
        return (
            f"本轮理解：{self.understanding}\n"
            f"补全后的完整问题：{self.completed_question}"
        )


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


def _ref_names(refs) -> list[str]:
    return list(dict.fromkeys(ref.display_name for ref in refs))


def _join_names(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return "、".join(values[:-1]) + "和" + values[-1]


def _scalar_value(value) -> str:
    if isinstance(value, m.EntityValueRef):
        return value.ref.display_name
    if isinstance(value, m.ListValue):
        return "、".join(_scalar_value(item) for item in value.values)
    if isinstance(value, m.RangeValue):
        return f"{_scalar_value(value.start)}至{_scalar_value(value.end)}"
    if isinstance(value, m.NullValue):
        return "空值"
    raw = value.value
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    if isinstance(raw, bool):
        return "是" if raw else "否"
    return str(raw)


_FILTER_OPERATORS = {
    "EQ": "等于",
    "NE": "不等于",
    "IN": "属于",
    "NOT_IN": "不属于",
    "GT": "大于",
    "GTE": "大于等于",
    "LT": "小于",
    "LTE": "小于等于",
    "BETWEEN": "介于",
    "LIKE": "匹配",
    "NOT_LIKE": "不匹配",
    "IS_NULL": "为空",
    "IS_NOT_NULL": "不为空",
}


def _filter_text(expression) -> str:
    if expression is None:
        return ""
    if isinstance(expression, m.BooleanFilterGroup):
        children = [_filter_text(child) for child in expression.children]
        children = [child for child in children if child]
        if _enum_value(expression.operator) == "NOT":
            return f"非（{children[0]}）"
        conjunction = " 且 " if _enum_value(expression.operator) == "AND" else " 或 "
        return "（" + conjunction.join(children) + "）"
    operator = _enum_value(expression.operator)
    label = expression.field_ref.display_name
    if operator in {"IS_NULL", "IS_NOT_NULL"}:
        return f"{label}{_FILTER_OPERATORS[operator]}"
    return f"{label}{_FILTER_OPERATORS.get(operator, operator)}{_scalar_value(expression.value)}"


def _time_text(spec: m.TimeSpec | None) -> str:
    if spec is None:
        return ""
    if spec.range is None:
        return "不限时间"
    zone = ZoneInfo(spec.timezone)
    start = spec.range.start.astimezone(zone)
    end = spec.range.end_exclusive.astimezone(zone)
    if (
        start.month == start.day == 1
        and start.hour == start.minute == start.second == start.microsecond == 0
        and end.month == end.day == 1
        and end.hour == end.minute == end.second == end.microsecond == 0
        and end.year == start.year + 1
    ):
        return f"{start.year}年"
    inclusive = end - timedelta(microseconds=1)
    if start.time().isoformat() == "00:00:00" and inclusive.date() >= start.date():
        return f"{start.date().isoformat()}至{inclusive.date().isoformat()}"
    return f"{start.isoformat()}至{end.isoformat()}（左闭右开）"


def _active_semantics(state: ConversationState, task_id: str, version: int) -> m.TaskSemanticState:
    task = state.tasks.get(task_id)
    if task is None or task.active_version != version:
        raise ValueError("V2_COMPLETED_QUESTION_TASK_VERSION_MISMATCH")
    matches = [item for item in task.versions if item.version == version]
    if len(matches) != 1:
        raise ValueError("V2_COMPLETED_QUESTION_TASK_VERSION_MISMATCH")
    return matches[0].semantics


def _validate_plan_state(plan: AuthorizedLogicalPlan, semantics: m.TaskSemanticState) -> None:
    payload = plan.payload
    for state_name, payload_name in (
        ("subject", "subject"),
        ("metrics", "measures"),
        ("dimensions", "group_by"),
        ("filter_expression", "filters"),
        ("time_spec", "time"),
        ("projection_spec", "projection_spec"),
    ):
        if hasattr(payload, payload_name) and getattr(payload, payload_name) != getattr(semantics, state_name):
            raise ValueError("V2_COMPLETED_QUESTION_PLAN_STATE_MISMATCH")


def _relation(context_trace) -> str:
    if isinstance(context_trace, dict):
        value = context_trace.get("FINAL_RELATION")
        if isinstance(value, str) and value:
            return value
    return "UNKNOWN"


def _prior_semantics(
    previous: ConversationState | None,
    task_id: str,
) -> m.TaskSemanticState | None:
    if previous is None or task_id not in previous.tasks:
        return None
    task = previous.tasks[task_id]
    return next(item.semantics for item in task.versions if item.version == task.active_version)


def _understanding(
    relation: str,
    previous: m.TaskSemanticState | None,
    current: m.TaskSemanticState,
) -> str:
    lead = {
        "NEW_TASK": "识别为独立新任务，未继承其他任务条件",
        "MODIFY": "沿用当前任务并应用本轮修改",
        "FOLLOW_UP": "沿用当前任务并应用本轮补充",
        "RETURN_TO_TOPIC": "返回已存在且范围兼容的历史任务",
        "ANSWER_CLARIFICATION": "使用本轮回答继续原任务",
    }.get(relation, "使用当前已确认任务")
    changes: list[str] = []
    if previous is not None:
        old_metrics = {item.canonical_id: item.display_name for item in previous.metrics}
        new_metrics = {item.canonical_id: item.display_name for item in current.metrics}
        added = [name for key, name in new_metrics.items() if key not in old_metrics]
        removed = [name for key, name in old_metrics.items() if key not in new_metrics]
        if added:
            changes.append("新增指标" + _join_names(added))
        if removed:
            changes.append("移除指标" + _join_names(removed))
        if previous.time_spec != current.time_spec:
            changes.append("更新时间范围为" + (_time_text(current.time_spec) or "未指定"))
        if previous.filter_expression != current.filter_expression:
            changes.append(
                "清除原筛选条件"
                if current.filter_expression is None
                else "更新筛选条件为" + _filter_text(current.filter_expression)
            )
    metrics = _join_names(_ref_names(current.metrics)) or "未指定指标"
    final = [f"最终指标为{metrics}"]
    if current.time_spec is not None:
        final.append(f"最终时间为{_time_text(current.time_spec)}")
    if current.filter_expression is not None:
        final.append(f"最终筛选为{_filter_text(current.filter_expression)}")
    return "；".join([lead, *changes, *final]) + "。"


def _completed_question(semantics: m.TaskSemanticState, *, cleared_filter: bool) -> str:
    metrics = _join_names(_ref_names(semantics.metrics))
    subject = semantics.subject.display_name if semantics.subject is not None else ""
    target = metrics or subject or "当前对象"
    qualifiers: list[str] = []
    time_text = _time_text(semantics.time_spec)
    if time_text:
        qualifiers.append(time_text)
    filter_text = _filter_text(semantics.filter_expression)
    if filter_text:
        qualifiers.append("筛选条件为" + filter_text)
    if semantics.dimensions:
        qualifiers.append("按" + _join_names(_ref_names(semantics.dimensions)) + "分组")
    prefix = "、".join(qualifiers)
    text = f"查询{prefix + '的' if prefix else ''}{target}"
    if cleared_filter and semantics.filter_expression is None:
        text += "，不再应用已清除的筛选条件"
    return text + "。"


def build_completed_question_display(
    *,
    message_id: str,
    plan: AuthorizedLogicalPlan,
    previous_state: ConversationState | None,
    next_state: ConversationState,
    context_trace,
) -> CompletedQuestionDisplay:
    semantics = _active_semantics(next_state, plan.task_id, plan.task_version)
    _validate_plan_state(plan, semantics)
    relation = _relation(context_trace)
    previous = _prior_semantics(previous_state, plan.task_id)
    task = next_state.tasks[plan.task_id]
    completed = _completed_question(
        semantics,
        cleared_filter=(
            "filter_expression" in task.clear_barriers
            and (previous is None or previous.filter_expression is not None)
        ),
    )
    understanding = _understanding(relation, previous, semantics)
    material = {
        "source": "FINAL_TASK_STATE_AND_AUTHORIZED_LOGICAL_PLAN",
        "message_id": message_id,
        "task_id": plan.task_id,
        "task_version": plan.task_version,
        "plan_id": plan.plan_id,
        "semantic_fingerprint": plan.semantic_fingerprint,
        "relation": relation,
        "understanding": understanding,
        "completed_question": completed,
    }
    display = CompletedQuestionDisplay(**material, display_digest=contract_digest(material))
    if len(display.public_message) > 500:
        raise ValueError("V2_COMPLETED_QUESTION_DISPLAY_TOO_LONG")
    return display
