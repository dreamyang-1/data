"""Presentation-only model for the public intent-recognition trace.

This module deliberately receives an already classified request and produces
display text only.  It must never mutate the request or feed values back into
intent recognition, ASL generation, SQL translation, or execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.domain.models import (
    CanonicalAnalysisRequest,
    ContextMode,
    PrimaryIntent,
    TaskPlan,
    TurnRelation,
)


_INTENT_LABELS = {
    PrimaryIntent.CHAT: "闲聊",
    PrimaryIntent.METRIC_QUERY: "指标查询",
    PrimaryIntent.DETAIL_QUERY: "明细查询",
    PrimaryIntent.TREND_ANALYSIS: "趋势分析",
    PrimaryIntent.COMPARISON_ANALYSIS: "对比分析",
    PrimaryIntent.COMPOSITION_ANALYSIS: "占比分析",
    PrimaryIntent.ANOMALY_ANALYSIS: "异常分析",
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: "归因分析",
    PrimaryIntent.FORECAST_ANALYSIS: "预测分析",
    PrimaryIntent.REPORT_GENERATION: "报表生成",
    PrimaryIntent.METRIC_DEFINITION: "解释口径",
    PrimaryIntent.DATA_LINEAGE: "数据血缘",
    PrimaryIntent.DATA_QUALITY: "数据质量",
    PrimaryIntent.CAPABILITY_HELP: "能力说明",
    PrimaryIntent.OUT_OF_SCOPE: "非数据任务",
}

_TURN_RELATION_LABELS = {
    TurnRelation.STANDALONE_NEW_TOPIC: "独立新问题",
    TurnRelation.CURRENT_TOPIC_FOLLOWUP: "当前主题追问",
    TurnRelation.CURRENT_TOPIC_MODIFICATION: "当前主题条件修改",
    TurnRelation.CURRENT_TOPIC_DRILLDOWN: "当前主题下钻",
    TurnRelation.HISTORICAL_TOPIC_RETURN: "返回历史主题",
    TurnRelation.CLARIFICATION_RESPONSE: "澄清回复",
    TurnRelation.CORRECTION: "纠正上一请求",
    TurnRelation.AMBIGUOUS_RELATION: "轮次关系待确认",
}

_INTENT_BASES = {
    PrimaryIntent.DETAIL_QUERY: (
        "用户要求返回符合条件的业务明细或对象列表，未要求执行统计聚合。"
    ),
    PrimaryIntent.METRIC_QUERY: "用户要求计算或汇总业务指标，需按指标口径执行查询。",
    PrimaryIntent.TREND_ANALYSIS: "用户要求观察指标随时间的变化，需按时间粒度组织结果。",
    PrimaryIntent.COMPARISON_ANALYSIS: "用户要求比较对象或时期差异，需保留比较维度。",
    PrimaryIntent.COMPOSITION_ANALYSIS: "用户要求分析组成或占比，需按业务维度分组。",
    PrimaryIntent.ANOMALY_ANALYSIS: "用户要求识别异常表现，需先取得可校验的业务事实。",
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: "用户要求分析原因，需基于可验证维度逐层分解。",
    PrimaryIntent.FORECAST_ANALYSIS: "用户要求预测未来结果，需校验历史窗口与预测周期。",
    PrimaryIntent.REPORT_GENERATION: "用户要求生成综合报表，需组织多项查询与分析结果。",
    PrimaryIntent.METRIC_DEFINITION: "用户询问指标定义或计算口径，无需执行明细查询。",
    PrimaryIntent.DATA_LINEAGE: "用户询问数据来源与加工关系，需查询语义血缘信息。",
    PrimaryIntent.DATA_QUALITY: "用户要求检查数据质量，需执行完整性与一致性校验。",
    PrimaryIntent.CHAT: "用户当前问题属于一般交流，不需要执行数据查询。",
    PrimaryIntent.CAPABILITY_HELP: "用户询问系统能力或使用方法，不需要执行业务查询。",
    PrimaryIntent.OUT_OF_SCOPE: "用户问题不属于当前数据智能体可执行的业务范围。",
}


def _single_line(value: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _unique_text(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(
        text for item in values if (text := _single_line(item, 120))
    ))


def _list_text(values: list[str]) -> str:
    return "[" + ", ".join(f"'{value}'" for value in values) + "]"


@dataclass(frozen=True)
class IntentRecognitionDisplayV2:
    """Immutable display projection; never part of the execution contract."""

    version: str = "V2"
    original_question: str = ""
    completed_question: str = ""
    file_judgement: str = ""
    task_intent: str = ""
    intent_basis: str = ""
    show_structure: bool = True
    metrics: list[str] = field(default_factory=list)
    show_empty_metrics: bool = False
    entity: str = ""
    entity_values: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    time_range: str = ""
    time_source: str = ""
    filters: list[str] = field(default_factory=list)
    ranking_count: int | None = None
    context_completion: str = "否"
    turn_relation: str = "未判定"
    needs_clarification: bool = False
    clarification_reason: str = ""
    normalization_status: str = "已完成"


@dataclass(frozen=True)
class CompositeIntentTaskDisplayV2:
    task_id: str
    question: str
    intent: str
    depends_on: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompositeIntentRecognitionDisplayV2:
    """Root-level display projection for a validated multi-task request."""

    version: str = "V2"
    original_question: str = ""
    completed_question: str = ""
    planner: str = ""
    shared_identifiers: list[str] = field(default_factory=list)
    tasks: list[CompositeIntentTaskDisplayV2] = field(default_factory=list)


def _file_judgement(file_status: str, file_based: bool) -> str:
    if file_status == "READ_SUCCESS":
        return (
            "检测到用户上传文件，按文件数据链路处理。"
            if file_based
            else "检测到用户上传文件，但当前问题不使用该文件，按业务数据链路处理。"
        )
    if file_status == "DATASET_BOUND":
        return (
            "检测到已绑定数据集，按数据集分析链路处理。"
            if file_based
            else "检测到已绑定数据集，但当前问题不使用该数据集，按业务数据链路处理。"
        )
    # Keep the established file behavior: show this row only when the request
    # actually carries a file or a bound dataset.
    return ""


def _time_source(request: CanonicalAnalysisRequest) -> str:
    assumptions = set(request.assumptions)
    if any(
        value.startswith(("DEFAULT_TIME_RANGE=", "ACTIVE_TIME_DEFAULT="))
        for value in assumptions
    ):
        return "系统推断"
    provenance = request.slot_provenance.get("time_range")
    if provenance is not None:
        source = str(provenance.source.value)
        if "CONTEXT" in source or "HISTORY" in source:
            return "上下文补全"
    return "用户原始输入"


def _clarification_reason(request: CanonicalAnalysisRequest) -> str:
    if request.missing_slots:
        return "仍缺少必填参数：" + "、".join(request.missing_slots) + "。"
    if request.primary_intent == PrimaryIntent.DETAIL_QUERY:
        return "查询对象、返回字段及筛选条件已满足明细查询要求，无缺失必填参数。"
    if request.primary_intent in {
        PrimaryIntent.METRIC_QUERY,
        PrimaryIntent.TREND_ANALYSIS,
        PrimaryIntent.COMPARISON_ANALYSIS,
        PrimaryIntent.COMPOSITION_ANALYSIS,
    }:
        return "指标、维度、时间及筛选条件已满足当前分析要求，无缺失必填参数。"
    return "当前任务所需参数已经完整，无缺失必填参数。"


def build_intent_recognition_display_v2(
    request: CanonicalAnalysisRequest,
    *,
    file_status: str = "NOT_PROVIDED",
    file_based: bool = False,
) -> IntentRecognitionDisplayV2:
    """Build a detached presentation snapshot from an executable request."""

    display = dict(request.semantic_display_slots or {})
    metrics = _unique_text(display.get("metrics"))
    entity_values = _unique_text(display.get("entity_values"))
    dimensions = _unique_text(display.get("dimensions"))
    fields = _unique_text(display.get("fields"))
    entity = _single_line(display.get("entity"), 120)
    filters: list[str] = []
    normalized_original = re.sub(r"\s+", "", request.original_question).casefold()
    for index, item in enumerate((display.get("filters") or [])[:10]):
        if not isinstance(item, dict):
            continue
        field_name = _single_line(item.get("field"), 120)
        operator = _single_line(item.get("operator") or "=", 20)
        source_index = item.get("_source_filter_index", index)
        try:
            source_index = int(source_index)
        except (TypeError, ValueError):
            source_index = index
        value = _single_line(item.get("value"), 120)
        if field_name and value:
            raw_item = (
                request.filters[source_index]
                if 0 <= source_index < len(request.filters)
                and isinstance(request.filters[source_index], dict)
                else {}
            )
            raw_value = raw_item.get("value")
            raw_field = _single_line(raw_item.get("field"), 120)
            if raw_field in {"适用类型", "适用科室类型", "关系类型"}:
                enum_labels = {"1": "主要适用", "2": "次要适用"}
                if isinstance(raw_value, list):
                    value = "[" + ", ".join(
                        enum_labels.get(str(item), str(item)) for item in raw_value
                    ) + "]"
                else:
                    value = enum_labels.get(str(raw_value), value)
            raw_values = raw_value if isinstance(raw_value, list) else [raw_value]
            explicitly_typed = any(
                (candidate_text := re.sub(
                    r"\s+", "", str(candidate or "")
                ).casefold())
                and candidate_text in normalized_original
                for candidate in raw_values
            )
            if explicitly_typed:
                source = "用户原始输入，经当前语义模型向量库规范化"
            elif (
                request.context_mode != ContextMode.NONE
                or request.rewrite_context_applied
            ):
                source = "上下文补全，经当前语义模型向量库规范化"
            else:
                source = "当前语义模型向量库规范化"
            filters.append(f"{field_name} {operator} {value}（来源：{source}）")

    relation = request.turn_relation
    inherited = (
        request.turn_admission.inherit_business_context
        if request.turn_admission is not None
        else request.context_mode != ContextMode.NONE
    )
    context_used = bool(inherited or request.rewrite_context_applied)
    if (
        request.time_range is not None
        and "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
        and request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    ):
        # The concrete interval is finalized only after the query service
        # returns its verified business-data watermark. Showing a wall-clock
        # date here would contradict the later source-aware execution range.
        time_range = "最近12个完整业务月份（执行时按数据水位确定）"
    elif request.time_range is not None:
        time_range = (
            f"{request.time_range.start.isoformat()} 至 "
            f"{request.time_range.end_exclusive.isoformat()}（右开区间）"
        )
    else:
        time_range = ""
    intent_label = _INTENT_LABELS.get(
        request.primary_intent, request.primary_intent.value
    )
    task_intent = (
        f"{'基于用户文件进行' if file_based else ''}{intent_label}"
        f"（{request.primary_intent.value}，置信度 {request.intent_confidence:.2f}）"
    )
    return IntentRecognitionDisplayV2(
        original_question=_single_line(request.original_question),
        completed_question=_single_line(
            request.rewritten_question or request.original_question
        ),
        file_judgement=_file_judgement(file_status, file_based),
        task_intent=task_intent,
        intent_basis=_INTENT_BASES.get(
            request.primary_intent, "根据当前问题中的任务目标和输出要求完成分类。"
        ),
        show_structure=request.primary_intent not in {
            PrimaryIntent.CHAT,
            PrimaryIntent.CAPABILITY_HELP,
            PrimaryIntent.OUT_OF_SCOPE,
        },
        metrics=metrics,
        show_empty_metrics=(
            request.primary_intent == PrimaryIntent.DETAIL_QUERY and not metrics
        ),
        entity=entity,
        entity_values=entity_values,
        dimensions=dimensions,
        fields=fields,
        time_range=time_range,
        time_source=_time_source(request) if time_range else "",
        filters=filters,
        ranking_count=request.ranking_limit,
        context_completion="是" if context_used else "否",
        turn_relation=(
            f"{_TURN_RELATION_LABELS.get(relation, relation.value)}（{relation.value}）"
            if relation is not None else "未判定"
        ),
        needs_clarification=bool(request.missing_slots),
        clarification_reason=_clarification_reason(request),
        normalization_status=(
            "待补充必填参数" if request.missing_slots else "已完成"
        ),
    )


def render_intent_recognition_display_v2(
    view: IntentRecognitionDisplayV2,
) -> str:
    """Render V2 as stable Markdown with one fact per line."""

    lines = [
        "### 1、意图识别",
        "",
        f"- 用户原始问题：{view.original_question}",
        f"- 补全后的问题：{view.completed_question}",
    ]
    if view.file_judgement:
        lines.append(f"- 文件判断：{view.file_judgement}")
    lines.extend([
        f"- 任务意图：{view.task_intent}",
        f"- 意图判定依据：{view.intent_basis}",
    ])
    structured: list[str] = []
    if view.metrics:
        structured.append(
            f"指标：{_list_text(view.metrics)}（来源：当前语义模型向量库）"
        )
    elif view.show_empty_metrics:
        structured.append("指标：[]（用户未要求统计指标）")
    if view.entity:
        structured.append(f"查询对象：{view.entity}（来源：当前语义模型向量库）")
    if view.entity_values:
        structured.append(
            f"业务实体值：{_list_text(view.entity_values)}"
            "（来源：当前语义模型向量库）"
        )
    if view.dimensions:
        structured.append(
            f"维度：{_list_text(view.dimensions)}（来源：当前语义模型向量库）"
        )
    if view.fields:
        structured.append(
            f"查询字段：{_list_text(view.fields)}（来源：当前语义模型向量库）"
        )
    if view.time_range:
        structured.append(
            f"时间区间：{view.time_range}（来源：{view.time_source}）"
        )
    if view.filters:
        structured.append(f"筛选条件：{_list_text(view.filters)}")
    if view.show_structure:
        structured.append(
            f"排序数量：{view.ranking_count if view.ranking_count is not None else '无'}"
        )
    if structured and view.show_structure:
        lines.append("- 结构化提取：")
        lines.extend(f"  - {item}" for item in structured)
    lines.extend([
        f"- 上下文补全：{view.context_completion}",
        f"- 轮次关系：{view.turn_relation}",
        f"- 是否需要追问：{'是' if view.needs_clarification else '否'}",
        (
            f"- {'追问' if view.needs_clarification else '不追问'}理由："
            f"{view.clarification_reason}"
        ),
        f"- 参数规范化：{view.normalization_status}",
    ])
    return "\n".join(lines)


def build_composite_intent_recognition_display_v2(
    original_question: str,
    plan: TaskPlan,
    *,
    task_intents: list[PrimaryIntent] | None = None,
) -> CompositeIntentRecognitionDisplayV2:
    """Build one immutable root display instead of exposing a DAG child.

    Task questions come from the already validated plan.  They are displayed
    as planning facts only and are never fed back into ASL or SQL execution.
    """

    normalized_source = str(original_question).translate(str.maketrans({
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
        "\uff0d": "-",
    }))
    identifiers = list(dict.fromkeys(re.findall(
        r"(?<![0-9A-Za-z])(?=[0-9A-Za-z-]{3,64}(?![0-9A-Za-z-]))"
        r"(?=[0-9A-Za-z-]*[A-Za-z])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+",
        normalized_source,
    )))
    intents = task_intents or []
    tasks = [
        CompositeIntentTaskDisplayV2(
            task_id=task.task_id,
            question=_single_line(task.question, 1000),
            intent=(
                _INTENT_LABELS.get(intents[index], intents[index].value)
                if index < len(intents)
                else "待子任务语义校验"
            ),
            depends_on=list(task.depends_on),
        )
        for index, task in enumerate(plan.tasks)
    ]
    completed = "；".join(
        f"{index}. {task.question}" for index, task in enumerate(tasks, 1)
    )
    return CompositeIntentRecognitionDisplayV2(
        original_question=_single_line(original_question, 1000),
        completed_question=completed,
        planner=plan.planner,
        shared_identifiers=identifiers,
        tasks=tasks,
    )


def render_composite_intent_recognition_display_v2(
    view: CompositeIntentRecognitionDisplayV2,
) -> str:
    """Render one deterministic parent trace for every DAG child."""

    lines = [
        "### 1、意图识别",
        "",
        f"- 用户原始问题：{view.original_question}",
        f"- 补全后的问题：{view.completed_question}",
        f"- 任务意图：复合查询（TASK_DAG，共 {len(view.tasks)} 个子任务）",
        "- 意图判定依据：用户要求分别返回多个可独立交付的业务结果。",
    ]
    if view.shared_identifiers:
        lines.append(
            f"- 共享业务标识：{_list_text(view.shared_identifiers)}"
            "（每个子任务继续按当前语义层独立规范化）"
        )
    lines.append("- 结构化拆分：")
    for index, task in enumerate(view.tasks, 1):
        dependency = (
            f"；依赖={_list_text(task.depends_on)}" if task.depends_on else ""
        )
        lines.append(
            f"  - 子任务 {index}（{task.task_id}）：{task.question}；"
            f"意图={task.intent}{dependency}"
        )
    lines.extend([
        "- 上下文补全：否（共享条件已写入每个完整子任务）",
        "- 轮次关系：独立复合问题（TASK_DAG）",
        "- 是否需要追问：由各子任务完成语义规范化后分别校验",
        f"- 参数规范化：任务拆分已完成（规划器={view.planner}）",
    ])
    return "\n".join(lines)
