"""Internal planner API compatibility; extraction and public I/O stay intact."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from app.domain.models import PlannerExtraction, TaskPlan


@dataclass(frozen=True)
class PlanningResult:
    plan: TaskPlan | None
    single_extraction: PlannerExtraction | None
    judgment: Any = None
    # 拆解模型生成的单任务陈述句名称，规划节点展示用
    single_task_name: str | None = None
    # 拆解模型输出的单任务问题文本，追问复用轮据此识别参数提取型复用
    single_task_question: str | None = None


def render_prior_task_table_context(prior_task_table: Any, budget: int = 3500) -> str:
    """提供历史任务及已有完整性证据，不把预览或文本截断后的名单当成完整结果。"""
    if not isinstance(prior_task_table, dict):
        return ""
    tasks = prior_task_table.get("tasks") or []
    if not tasks:
        return ""
    rules = (
        "追问轮上下文使用规则（不覆盖条件、粒度及完整性规则）：\n"
        "1. 已完成且足以满足本轮需要的结果不重复查询，不为重述上轮结论单独生成任务。\n"
        "2. 仅当本块明确标记该任务的全部结果已完整提供、所需字段逐项可见时，才可复用其中的具体参数。"
        "追问只有一个新增查询目标时可只规划该查询，把完整名单中的具体名称或编码写入question，"
        "不要仅写这几家、上述对象；保留本轮时间和筛选条件，新查询照常提取参数。\n"
        "3. 以下仅为采样，不能作为完整对象名单：未标记完整、行数不符或已省略的历史结果。"
        "所需名单应由完整数据集及依赖绑定取得对象范围；当前上下文不能提供完整范围时，按原口径重新查询，"
        "不得仅使用预览行，不得编造未展示值或沿用其他模型、业务域的结果。"
    )
    omission = "[部分历史上下文因长度限制已省略；省略内容不能作为完整名单。]"
    if budget <= 0:
        return ""
    if budget < len(rules) + len(omission) + len("[上一轮任务表]\n\n"):
        warning = "[上一轮任务表]\n上下文预算不足，未提供历史结果；不得据此复用具体名单。"
        return warning if len(warning) <= budget else ""
    table_budget = budget - len(rules) - len(omission) - 2
    lines = ["[上一轮任务表]"]
    omitted = False

    def append_block(block: str) -> None:
        nonlocal omitted
        # 不从完整名单中间截断，否则完整标记与实际送达的内容不符。
        if len("\n".join([*lines, block])) <= table_budget:
            lines.append(block)
        else:
            omitted = True

    question = str(prior_task_table.get("question") or "").strip()
    if question:
        append_block(f"上一轮问题：{question}")
    for row in tasks:
        if not isinstance(row, dict):
            continue
        name = str(row.get("名称") or "").strip()
        flag = str(row.get("是否查询数据库") or "").strip()
        line = f"- {name}（{flag}）" if flag else f"- {name}"
        summary = row.get("结果概要")
        if isinstance(summary, dict):
            detail = []
            columns = summary.get("columns")
            if isinstance(columns, list) and columns:
                detail.append("结果列：" + "、".join(str(c) for c in columns[:6]))
            if summary.get("row_count") is not None:
                detail.append(f"摘要记录{summary['row_count']}行")
            sample = summary.get("sample") or []
            valid_sample = isinstance(sample, list) and all(isinstance(item, dict) for item in sample)
            complete = (
                summary.get("sample_is_complete") is True
                and type(summary.get("row_count")) is int
                and valid_sample
                and summary["row_count"] == len(sample) <= 10
            )
            detail.append("全部结果已完整提供" if complete else "以下仅为采样，不能作为完整对象名单")
            if valid_sample:
                for sample_row in sample[:10]:
                    # 已证明完整的小结果保留所有字段，避免关联键在第五列以后被丢掉。
                    items = list(sample_row.items())
                    if not complete:
                        items = items[:4]
                    pairs = "；".join(
                        f"{k}={v}" for k, v in items
                    )
                    detail.append(pairs)
            if detail:
                line += "（" + "，".join(detail) + "）"
        append_block(line)
    if omitted:
        lines.append(omission)
    return "\n".join([*lines, rules])


def recognized_planning_intent(judgment, fallback=None):
    """Read an existing recognition verdict, without interpreting the question."""
    from app.planning.task_dag import SingleQuestionPlanner
    return SingleQuestionPlanner._sanitize_intent(getattr(judgment, 'intent', None)) or fallback


def inherit_recognized_intent(intent, *, plan=None, extraction=None, judgment=None):
    """Copy the recognition verdict; task execution bindings stay untouched."""
    intent = recognized_planning_intent(judgment, intent)
    from app.planning.task_dag import _PRIMARY_INTENT_LABELS
    if extraction is not None and intent is not None:
        extraction.intent = intent
        if extraction.structured is not None:
            extraction.structured = dict(extraction.structured,
                意图=_PRIMARY_INTENT_LABELS.get(intent, intent.value))
    if plan is not None:
        intents = [intent] * len(plan.tasks)
        if judgment is not None and getattr(judgment, 'is_compound', False):
            for group, entry in zip(plan.question_groups, judgment.entries):
                entry_intent = recognized_planning_intent(entry, intent)
                for index in range(group.task_offset, min(len(intents), group.task_offset + group.task_count)):
                    intents[index] = entry_intent
        for task, task_intent in zip(plan.tasks, intents):
            if task_intent is not None:
                task.primary_intent = task_intent
                if task.extraction is not None:
                    task.extraction = dict(task.extraction,
                        意图=_PRIMARY_INTENT_LABELS.get(task_intent, task_intent.value))


async def plan_question(planner: Any, question: str, *, semantic_context: str,
                        semantic_model_id: int, prior_judgment: Any = None,
                        root_intent: Any = None) -> PlanningResult:
    """Support legacy plan() and the deployed router without judging twice.

    root_intent 是意图识别节点的根意图：规划器支持该参数时整轮规划
    直接复用它，不再对问题或任务做意图判断。
    """
    root_intent = recognized_planning_intent(prior_judgment, root_intent)
    route_method = getattr(planner, 'route_and_plan', None)
    reuse = getattr(planner, 'plan_with_judgment', None)
    routed = callable(route_method)
    reuse_judgment = routed and prior_judgment is not None and callable(reuse)
    method = reuse if reuse_judgment else (route_method if routed else planner.plan)
    parameters = inspect.signature(method).parameters
    values = {'semantic_context': semantic_context, 'semantic_model_id': semantic_model_id}
    if root_intent is not None:
        values['root_intent'] = root_intent
    kwargs = {name: value for name, value in values.items() if name in parameters}
    args = (prior_judgment, question) if reuse_judgment else (question,)
    outcome = await method(*args, **kwargs)
    single = outcome.single if routed else outcome
    result = PlanningResult(plan=outcome.plan,
                          single_extraction=single.single_extraction if single is not None else None,
                          judgment=outcome.judgment if routed else None,
                          single_task_name=(
                              single.single_task_name if single is not None else None
                          ),
                          single_task_question=(
                              single.single_task_question if single is not None else None
                          ))
    inherit_recognized_intent(root_intent, plan=result.plan, extraction=result.single_extraction,
        judgment=prior_judgment or result.judgment)
    return result


def prepare_execution(planner: Any, plan: TaskPlan):
    """DAG helpers moved from instance methods to module functions on 49."""
    deduplicate = getattr(planner, 'deduplicate', None)
    layers = getattr(planner, 'execution_layers', None)
    if not callable(deduplicate) or not callable(layers):
        from app.planning.compound_question import deduplicate_plan, execution_layers
        deduplicate, layers = deduplicate_plan, execution_layers
    execution_plan, aliases = deduplicate(plan)
    return execution_plan, aliases, layers(execution_plan)
