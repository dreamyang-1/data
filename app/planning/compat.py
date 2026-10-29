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
    """把上一轮任务表渲染成拆分判断/任务规划的上下文块。

    上轮没有任务表时返回空串，调用方把原 semantic_context 原样透传。
    规划模型必须先看到上一轮有什么，否则会把上轮已查过的口径再拆一个任务。
    """
    if not isinstance(prior_task_table, dict):
        return ""
    tasks = prior_task_table.get("tasks") or []
    if not tasks:
        return ""
    lines = ["[上一轮任务表]"]
    question = str(prior_task_table.get("question") or "").strip()
    if question:
        lines.append(f"上一轮问题：{question}")
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
            if columns:
                detail.append("结果列：" + "、".join(str(c) for c in columns[:6]))
            if summary.get("row_count") is not None:
                detail.append(f"共{summary['row_count']}行")
            # 采样整段给出：追问要引用的参数值（实体名单）分布在各行，
            # 只给首行会让模型拿不全参数，进而退回"先查名单再查明细"的拆分
            sample = summary.get("sample")
            if sample and isinstance(sample[0], dict):
                for sample_row in sample[:10]:
                    pairs = "；".join(
                        f"{k}={v}" for k, v in list(sample_row.items())[:4]
                    )
                    detail.append(pairs)
            if detail:
                line += "（" + "，".join(detail) + "）"
        lines.append(line)
    rules = (
        "追问轮拆分规则（优先于通用拆分规则）：\n"
        "1. 上一轮已经查到的结果（如\"排名前三的是哪几家\"）不再生成任何任务，"
        "尤其不要生成以\"复用上一轮查询结果\"开头、用来重述上轮结论的任务。\n"
        "2. 追问只有1个新增查询目标、且所需参数（实体名单等）已出现在上一轮任务表"
        "的采样里时，整轮判定为单一问题，只输出1个任务：把采样里的参数值逐个写进"
        "任务问题文本（如\"复用上一轮查询结果：查询A公司、B公司、C公司2025年10月"
        "主要销售的产品明细\"），不得用\"这三家\"\"上述对象\"之类的指代替换参数值——"
        "后续环节只认问题文本里的具体值；该任务照常发起新查询并完整提取参数。\n"
        "3. 只有追问包含多个相互独立的新增查询目标、或所需参数无法从上一轮任务表"
        "取得时，才按通用规则拆分。"
    )
    # 规则必须完整送达拆分模型，长度超限时压缩采样区而不是截掉规则
    table_budget = budget - len(rules)
    table_text = "\n".join(lines)
    if len(table_text) > table_budget:
        table_text = table_text[:table_budget]
    text = table_text + "\n" + rules
    return text if len(text) <= budget else text[:budget]


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
                        root_intent: Any = None, intents_locked: bool = False) -> PlanningResult:
    """Support legacy plan() and the deployed router without judging twice.

    root_intent 是意图识别节点的根意图：规划器支持该参数时整轮规划
    直接复用它，不再对问题或任务做意图判断。
    """
    root_intent = recognized_planning_intent(prior_judgment, root_intent)
    route_method = getattr(planner, 'route_and_plan', None)
    reuse = getattr(planner, 'plan_with_judgment', None)
    routed = callable(route_method)
    reuse_judgment = routed and prior_judgment is not None and callable(reuse)
    if intents_locked and routed and not reuse_judgment:
        from app.planning.task_dag import TaskPlanningError
        raise TaskPlanningError("V2任务规划缺少问题补全阶段的拆分与意图合同")
    method = reuse if reuse_judgment else (route_method if routed else planner.plan)
    parameters = inspect.signature(method).parameters
    values = {'semantic_context': semantic_context, 'semantic_model_id': semantic_model_id}
    if root_intent is not None:
        values['root_intent'] = root_intent
    if intents_locked:
        values['intents_locked'] = True
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
