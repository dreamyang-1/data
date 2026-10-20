"""分析意图的确定性任务扩展。

规划模型对趋势/对比/占比/异常/归因/预测类问题只产出单任务取数参数；
这里按登记模板把单任务展开成多任务 DAG：每个取数操作、分析操作都是
独立任务节点，随任务表下发，调度执行期按依赖顺序调度并单独追踪。
任务文本素材只取自原问题与原提取参数，不引入新业务名词；维度通道
之外的派生改写只落在提取参数副本上。
"""
from __future__ import annotations

import copy
import re
from typing import Any

from app.domain.models import (
    AtomicTask,
    PlannerExtraction,
    PrimaryIntent,
    TaskPlan,
)
from app.planning.task_dag import extraction_to_parameters

# 分析意图 -> 执行模板：分析类型键、对外工具名、引擎 facts 键对应的子工具名。
# 子工具行只在引擎 facts 实际产出对应键时发射，展示与真实计算一致。
ANALYSIS_PLAYBOOKS: dict[PrimaryIntent, dict[str, Any]] = {
    PrimaryIntent.TREND_ANALYSIS: {
        "analysis_type": "trend",
        "tool": "趋势分析工具",
        "sub_tools": {
            "period_over_period": "环比计算工具",
            "year_over_year": "同比计算工具",
        },
    },
    PrimaryIntent.COMPARISON_ANALYSIS: {
        "analysis_type": "comparison",
        "tool": "对比分析工具",
        "sub_tools": {},
    },
    PrimaryIntent.COMPOSITION_ANALYSIS: {
        "analysis_type": "composition",
        "tool": "占比分析工具",
        "sub_tools": {},
    },
    PrimaryIntent.ANOMALY_ANALYSIS: {
        "analysis_type": "anomaly",
        "tool": "异常检测工具",
        "sub_tools": {},
    },
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: {
        "analysis_type": "root_cause",
        "tool": "归因分析工具",
        "sub_tools": {},
    },
    PrimaryIntent.FORECAST_ANALYSIS: {
        "analysis_type": "forecast",
        "tool": "预测分析工具",
        "sub_tools": {},
    },
}

_ANALYSIS_BINDING_KEY = "分析绑定"
_NO_DATABASE_MARK = "不再查询数据库"
_REUSE_PREFIX = "复用上一轮查询结果"

# 对比分析的期间提及：2025年11月 / 2025年 / 10月 / 本月 / 上月 / 本季度 / 今年 等。
# 只识别可排序的完整期间词；"近3个月"这类含量词的窗口不匹配。
_ABSOLUTE_MONTH = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:份)?")
_ABSOLUTE_YEAR = re.compile(r"(\d{4})\s*年(?:度)?(?!\s*\d)(?!\s*个)")
_BARE_MONTH = re.compile(r"(?<![\d年月])\d{1,2}\s*月(?:份)?(?![\d号日])")
_RELATIVE_PERIOD = re.compile(
    r"(?:本|上|下)(?:个)?(?:月|季度)|(?:今|去|明|前)年|(?:上|下)半年(?:度)?"
)

_RELATIVE_RANK = {
    "前年": 0, "去年": 1, "今年": 2, "明年": 3,
    "上季度": 1, "本季度": 2, "下个季度": 3, "下季度": 3,
    "上月": 1, "本月": 2, "下个月": 3, "下月": 3,
    "上半年": 1, "上半年度": 1, "下半年": 2, "下半年度": 2,
}


def _dimension_names(structured: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in structured.get("维度") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _metric_text(structured: dict[str, Any]) -> str:
    names: list[str] = []
    for item in structured.get("指标") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return "、".join(names)


def _time_text(structured: dict[str, Any]) -> str:
    granularity = structured.get("时间粒度")
    if isinstance(granularity, dict):
        value = granularity.get("time_range")
        if isinstance(value, str):
            return value.strip()
    return ""


def _comparison_periods(question: str) -> list[tuple[str, tuple[int, int]]] | None:
    """提取恰好两个可排序的期间提及；不满足对比两期条件时返回 None。"""
    found: list[tuple[str, tuple[int, int]]] = []
    covered: list[tuple[int, int]] = []
    for match in _ABSOLUTE_MONTH.finditer(question):
        found.append((match.group(0), (int(match.group(1)), int(match.group(2)))))
        covered.append(match.span())
    # 裸月份（如"2025年11月和10月"里的"10月"）与绝对月共存，按年份对齐
    for match in _BARE_MONTH.finditer(question):
        if any(start <= match.start() < end for start, end in covered):
            continue
        label = match.group(0)
        month = int(re.search(r"\d{1,2}", label).group(0))
        found.append((label, (0, month)))
    if not found:
        for match in _ABSOLUTE_YEAR.finditer(question):
            found.append((match.group(0), (int(match.group(1)), 0)))
    relative = [
        (match.group(0), (_RELATIVE_RANK.get(match.group(0), -1), 0))
        for match in _RELATIVE_PERIOD.finditer(question)
    ]
    relative = [item for item in relative if item[1][0] >= 0]
    # 绝对期间与相对期间混用时无法可靠排序，不拆
    if found and relative:
        return None
    candidates = found or relative
    unique: list[tuple[str, tuple[int, int]]] = []
    for item in candidates:
        if item[0] not in {label for label, _ in unique}:
            unique.append(item)
    if len(unique) != 2:
        return None
    if not found:
        families = {"月" if "月" in label else "季度" if "季度" in label else "年"
                    for label, _ in unique}
        if len(families) != 1:
            return None
    if found and all(key[0] == 0 for _, key in unique):
        # 两个裸月份（如"11月和10月"）视为同一年，按月份排序
        unique = [(label, (1, key[1])) for label, key in unique]
    elif found and any(key[0] == 0 for _, key in unique):
        years = [key[0] for _, key in unique if key[0] > 0]
        if len(years) != 1:
            return None
        unique = [(label, (years[0], key[1]) if key[0] == 0 else key)
                  for label, key in unique]
    unique.sort(key=lambda item: item[1])
    if unique[0][1] == unique[1][1]:
        return None
    return unique


def _retrieval_extraction(structured: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    cloned = copy.deepcopy(structured)
    cloned.pop(_ANALYSIS_BINDING_KEY, None)
    cloned["意图"] = "指标查询"
    cloned["是否查询数据库"] = "是"
    for key, value in overrides.items():
        cloned[key] = value
    return cloned


def _query_clause(time_text: str, grouping: str, metric_text: str) -> str:
    middle = f"{time_text}{grouping}的" if (time_text or grouping) else ""
    return f"查询{middle}{metric_text}"


def build_analysis_task_plan(
    question: str,
    extraction: PlannerExtraction,
    *,
    task_question: str | None = None,
    task_name: str | None = None,
) -> TaskPlan | None:
    """把分析类单任务展开成取数任务+分析任务的 DAG；不满足模板时返回 None。

    取数任务统一降为指标查询口径，分析计算只发生在分析任务节点，
    保证分析链路在任务表与调度执行中都是独立可见的节点。
    """
    structured = extraction.structured
    playbook = ANALYSIS_PLAYBOOKS.get(extraction.intent) if extraction.intent else None
    if playbook is None or not isinstance(structured, dict):
        return None
    # 快照复用、参数复用、纯计算的单任务维持原路由，不展开
    if str(structured.get("是否查询数据库") or "").startswith("否"):
        return None
    source_text = str(task_question or question or "").strip()
    if source_text.startswith(_REUSE_PREFIX) or _NO_DATABASE_MARK in source_text:
        return None
    metric_text = _metric_text(structured)
    if not metric_text:
        return None
    intent = extraction.intent
    time_text = _time_text(structured)
    dimensions = _dimension_names(structured)
    analysis_type = playbook["analysis_type"]

    retrieval_specs: list[dict[str, Any]] = []
    merge_mode = "concat"
    binding_periods: list[dict[str, str]] = []

    if intent is PrimaryIntent.COMPARISON_ANALYSIS:
        periods = _comparison_periods(question)
        if periods is not None:
            merge_mode = "period_roles"
            grouping = ("各" + "、".join(dimensions)) if dimensions else ""
            for index, (label, sort_key) in enumerate(periods):
                role = "基期" if index == 0 else "当前期"
                # 裸月份（"10月"）补全对齐后的年份，ASL 侧不需要再猜年份；
                # 年份来自原问题中的另一期间提及，不引入新时间条件
                if "年" not in label and sort_key[0] > 1900:
                    label = f"{sort_key[0]}年{label}"
                period_structured = _retrieval_extraction(structured)
                granularity = period_structured.get("时间粒度")
                if isinstance(granularity, dict):
                    granularity["time_range"] = label
                else:
                    period_structured["时间粒度"] = {
                        "unit": None, "time_range": label, "anchor": None,
                    }
                task_id = f"task-{index + 1}"
                retrieval_specs.append({
                    "task_id": task_id,
                    "question": _query_clause(label, grouping, metric_text),
                    "name": _query_clause(label, grouping, metric_text)[:60],
                    "structured": period_structured,
                })
                binding_periods.append({"task": task_id, "label": label, "role": role})

    if intent is PrimaryIntent.ROOT_CAUSE_ANALYSIS and dimensions:
        drill_dimensions = dimensions[:3]
        total_structured = _retrieval_extraction(structured, 维度=[])
        retrieval_specs.append({
            "task_id": "task-1",
            "question": _query_clause(time_text, "", metric_text),
            "name": _query_clause(time_text, "", metric_text)[:60],
            "structured": total_structured,
        })
        for offset, dimension in enumerate(drill_dimensions, start=2):
            retrieval_specs.append({
                "task_id": f"task-{offset}",
                "question": _query_clause(time_text, f"各{dimension}", metric_text),
                "name": _query_clause(time_text, f"各{dimension}", metric_text)[:60],
                "structured": _retrieval_extraction(structured, 维度=[dimension]),
            })

    if not retrieval_specs:
        # 趋势/占比/异常/预测及未命中两期拆分的对比：原取数任务保持不变，
        # 只把意图口径降为指标查询，分析计算交给独立分析任务
        retrieval_specs.append({
            "task_id": "task-1",
            "question": source_text or _query_clause(time_text, "", metric_text),
            "name": (task_name or source_text or _query_clause(time_text, "", metric_text))[:60],
            "structured": _retrieval_extraction(structured),
        })

    analysis_structured = copy.deepcopy(structured)
    analysis_structured["是否查询数据库"] = "否"
    analysis_structured[_ANALYSIS_BINDING_KEY] = {
        "analysis_type": analysis_type,
        "intent": intent.value,
        "tool": playbook["tool"],
        "sub_tools": playbook["sub_tools"],
        "merge": merge_mode,
        "periods": binding_periods,
    }
    retrieval_ids = [spec["task_id"] for spec in retrieval_specs]
    analysis_question = _analysis_question(
        intent, metric_text, time_text, dimensions, binding_periods,
    )
    tasks = [
        AtomicTask(
            task_id=spec["task_id"],
            question=spec["question"],
            name=spec["name"],
            extraction=spec["structured"],
            parameters=tuple(extraction_to_parameters(spec["structured"])),
            primary_intent=PrimaryIntent.METRIC_QUERY,
            expected_output=f"{spec['name']}的查询结果",
        )
        for spec in retrieval_specs
    ]
    tasks.append(AtomicTask(
        task_id=f"task-{len(tasks) + 1}",
        question=f"根据上述任务的查询结果，{analysis_question}，{_NO_DATABASE_MARK}",
        name=analysis_question[:60],
        depends_on=retrieval_ids,
        extraction=analysis_structured,
        parameters=tuple(extraction_to_parameters(analysis_structured)),
        primary_intent=intent,
        expected_output=f"{playbook['tool']}输出的分析结论与计算事实",
    ))
    return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")


def _analysis_question(
    intent: PrimaryIntent,
    metric_text: str,
    time_text: str,
    dimensions: list[str],
    periods: list[dict[str, str]],
) -> str:
    if intent is PrimaryIntent.TREND_ANALYSIS:
        return f"对{time_text}{metric_text}进行趋势分析（含环比、同比），识别涨跌与拐点"
    if intent is PrimaryIntent.COMPARISON_ANALYSIS:
        if periods:
            pair = "与".join(item["label"] for item in periods)
            return f"对比分析{pair}的{metric_text}，计算差值与差异率"
        return f"对比分析{time_text}{metric_text}，计算差值与差异率"
    if intent is PrimaryIntent.COMPOSITION_ANALYSIS:
        grouping = "、".join(dimensions)
        return f"计算各{grouping}{metric_text}占总体的比重并排序" if grouping else f"计算{metric_text}的占比结构"
    if intent is PrimaryIntent.ANOMALY_ANALYSIS:
        return f"检测{time_text}{metric_text}的异常波动，标记异常对象与异常时段"
    if intent is PrimaryIntent.ROOT_CAUSE_ANALYSIS:
        grouping = "、".join(dimensions[:3])
        return (
            f"按{grouping}拆解{time_text}{metric_text}的变化贡献，定位主要影响因素"
            if grouping else f"分析{time_text}{metric_text}的变化贡献"
        )
    if intent is PrimaryIntent.FORECAST_ANALYSIS:
        return f"基于{time_text}历史数据预测{metric_text}的未来走势"
    return f"分析{metric_text}"
