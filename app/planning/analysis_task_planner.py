"""分析意图的确定性任务扩展。

规划模型对趋势/对比/占比/异常/归因/预测类问题只产出单任务取数参数；
这里按登记模板把单任务展开成多任务 DAG：每个取数操作、分析操作都是
独立任务节点，随任务表下发，调度执行期按依赖顺序调度并单独追踪。
任务文本素材只取自原问题与原提取参数，不引入新业务名词；维度通道
之外的派生改写只落在提取参数副本上。
"""
from __future__ import annotations

import copy
import calendar
import re
from datetime import date, timedelta
from typing import Any

from app.analysis.chain_tools.temporal_window import (
    disjunctive_periods_to_range,
    parse_absolute_range,
)
from app.domain.models import (
    AtomicTask,
    PlannerExtraction,
    PrimaryIntent,
    TaskPlan,
)
from app.planning.task_dag import extraction_to_parameters
from app.analysis.requirements import analysis_intent, build_requirements, trend_comparison_modes

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


# 时间类维度不是业务分组：拼进任务文本会被下游误绑成业务值
# （如"查询2025年10月各业务日期的X"会被绑成商品名称过滤值）
_TIME_DIMENSION_WORDS = {
    "业务日期", "日期", "时间", "月份", "月", "年份", "年", "季度", "周",
    "星期", "日", "半年", "period", "date", "time", "month", "year", "quarter", "week",
}


def _is_time_dimension(name: str) -> bool:
    lowered = name.strip().casefold()
    return lowered in _TIME_DIMENSION_WORDS or any(
        lowered.endswith(word) for word in ("日期", "时间", "月份")
    )


def _dimension_names(structured: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in structured.get("维度") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _grouping_dimensions(structured: dict[str, Any]) -> list[str]:
    return [name for name in _dimension_names(structured) if not _is_time_dimension(name)]


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
    cloned.pop("计算绑定", None)
    cloned.pop("计算绑定来源", None)
    cloned.pop("结果操作", None)
    cloned["意图"] = "指标查询"
    cloned["是否查询数据库"] = "是"
    for key, value in overrides.items():
        cloned[key] = value
    return cloned


def _trend_retrieval_window(question: str, structured: dict[str, Any]) -> dict[str, Any] | None:
    """兼容入口：主查询窗口可含相邻基期，同比基期不混入该窗口。"""
    granularity = structured.get("时间粒度")
    if not isinstance(granularity, dict):
        return None
    if not str(granularity.get("time_range") or "").strip():
        return None
    requirements = build_requirements(PrimaryIntent.TREND_ANALYSIS, question, structured, today=date.today())
    windows = requirements.get("windows") or []
    if not windows:
        return None
    return {**granularity, "time_range": f"{windows[0]['start']}至{windows[0]['end']}"}


def _absolute_window_bounds(time_range: str) -> tuple[str, str] | None:
    """把绝对/列举式时间范围文本解析成 (起, 止) ISO 日期；失败返回 None。"""
    absolute = parse_absolute_range(time_range)
    if absolute is None:
        covering = disjunctive_periods_to_range(time_range)
        if covering is not None:
            absolute = parse_absolute_range(covering)
    if absolute is None:
        return None
    start, end = absolute
    return start.isoformat(), end.isoformat()


def _display_window_start_label(time_range: str) -> str | None:
    """用户展示窗口起点对应的月桶标签（YYYY-MM），供引擎口径对齐。"""
    bounds = _absolute_window_bounds(time_range)
    if bounds is None:
        return None
    return bounds[0][:7]


def _plain_period_text(time_text: str) -> str:
    """ISO区间粘连"各维度"时会被下游解析误绑成业务过滤值，
    改写为自然中文日期写法，与问句原生的期间表述一致。"""
    text = str(time_text or "").strip()
    single_year = re.fullmatch(
        r"(\d{4})\s*-\s*01\s*-\s*01\s*[至~]\s*\1\s*-\s*12\s*-\s*31", text)
    if single_year is not None:
        return single_year.group(1) + "年"
    whole_month = re.fullmatch(
        r"(\d{4})\s*-\s*(\d{2})\s*-\s*01\s*[至~]\s*\1\s*-\s*\2\s*-\s*(\d{1,2})", text)
    if whole_month is not None:
        year, month = int(whole_month.group(1)), int(whole_month.group(2))
        if int(whole_month.group(3)) == calendar.monthrange(year, month)[1]:
            return f"{year}年{month}月"
    iso_range = re.fullmatch(
        r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*[至~]\s*"
        r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})", text)
    if iso_range is not None:
        y1, m1, d1, y2, m2, d2 = (int(item) for item in iso_range.groups())
        return f"{y1}年{m1}月{d1}日至{y2}年{m2}月{d2}日"
    return text


def _query_clause(time_text: str, grouping: str, metric_text: str) -> str:
    time_text = _plain_period_text(time_text)
    middle = f"{time_text}{grouping}的" if (time_text or grouping) else ""
    return f"查询{middle}{metric_text}"


# 分组 TopN：“各事业部中销售额最高的产品线前三名”。这是每组各取前N，
# 与全局排序LIMIT不同；全局LIMIT会只留下整体最高的N行。
_CN_NUMBER = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "两": 2,
}
_GROUPED_TOP_N = re.compile(
    r"各[^，。；]{1,12}?(?:中|里)[^，。；]{0,30}?前\s*([一二三四五六七八九十两\d]+)\s*(?:名|位)"
)


def _grouped_top_n(
    question: str, dimensions: list[str],
) -> tuple[int, str, str] | None:
    """识别“各分组中取前N”的问法，返回 (N, 分组维度, 排名对象维度)。"""
    if len(dimensions) < 2:
        return None
    match = _GROUPED_TOP_N.search(re.sub(r"\s+", "", str(question or "")))
    if match is None:
        return None
    text = match.group(1)
    limit = int(text) if text.isdigit() else _CN_NUMBER.get(text)
    if not limit or not 1 <= limit <= 10:
        return None
    return limit, dimensions[0], dimensions[1]


def build_analysis_task_plan(
    question: str,
    extraction: PlannerExtraction,
    *,
    task_question: str | None = None,
    task_name: str | None = None,
    today: date | None = None,
) -> TaskPlan | None:
    """把分析类单任务展开成取数任务+分析任务的 DAG；不满足模板时返回 None。

    取数任务统一降为指标查询口径，分析计算只发生在分析任务节点，
    保证分析链路在任务表与调度执行中都是独立可见的节点。
    """
    structured = extraction.structured
    intent = extraction.intent
    playbook = ANALYSIS_PLAYBOOKS.get(intent)
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
    if today is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    requirements = build_requirements(intent, question, structured, today=today)
    if requirements.get('grain') and not (structured.get('时间粒度') or {}).get('unit'):
        structured = copy.deepcopy(structured)
        structured['时间粒度'] = {**(structured.get('时间粒度') or {}), 'unit': requirements['grain']}
    time_text = _time_text(structured)
    dimensions = _grouping_dimensions(structured)
    analysis_type = playbook["analysis_type"]

    grouped = _grouped_top_n(question, dimensions)
    if grouped is not None:
        limit, group_dimension, item_dimension = grouped
        retrieval_structured = _retrieval_extraction(structured)
        # 分组TopN需要每个分组的全部候选；保留全局LIMIT会截断其他分组。
        retrieval_structured["限制"] = None
        # 分析任务的输出要求（前三名、商品名称升序等）不是取数契约，
        # 带着它会让“产品线”维度绑定被误判为商品名称，这里清空。
        retrieval_structured["输出要求"] = None
        # “各A、B”会让绑定只认出 A；写成“各A中B”才能把第二个维度也绑上。
        grouping_text = "各" + group_dimension + "中" + "、".join(dimensions[1:])
        retrieval_question = _query_clause(time_text, grouping_text, metric_text)
        analysis_structured = copy.deepcopy(structured)
        analysis_structured["是否查询数据库"] = "否"
        analysis_structured[_ANALYSIS_BINDING_KEY] = {
            "analysis_type": "group_top_n",
            "intent": PrimaryIntent.COMPARISON_ANALYSIS.value,
            "tool": "分组TopN工具",
            "sub_tools": {},
            "merge": "concat",
            "periods": [],
            "source_question": str(question or "").strip(),
            "display_window_start": None,
            "group_top_n": {
                "limit": limit,
                "group_dimension": group_dimension,
                "item_dimension": item_dimension,
            },
        }
        analysis_question = (
            f"按{group_dimension}分组，取每组{metric_text}最高的前{limit}个{item_dimension}"
        )
        grouped_tasks = [
            AtomicTask(
                task_id="task-1",
                question=retrieval_question,
                name=(task_name or f"获取各{group_dimension}的{item_dimension}{metric_text}")[:60],
                extraction=retrieval_structured,
                parameters=tuple(extraction_to_parameters(retrieval_structured)),
                primary_intent=PrimaryIntent.METRIC_QUERY,
                expected_output=f"各{group_dimension}全部{item_dimension}的{metric_text}",
            ),
            AtomicTask(
                task_id="task-2",
                question=f"根据上述任务的查询结果，{analysis_question}，{_NO_DATABASE_MARK}",
                name=analysis_question[:60],
                depends_on=["task-1"],
                extraction=analysis_structured,
                parameters=tuple(extraction_to_parameters(analysis_structured)),
                primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
                expected_output="分组TopN工具输出的每组前N名结果",
            ),
        ]
        return TaskPlan(tasks=grouped_tasks, planner="DETERMINISTIC_RULE")

    retrieval_specs: list[dict[str, Any]] = []
    merge_mode = "concat"
    binding_periods: list[dict[str, str]] = []

    if intent is PrimaryIntent.COMPARISON_ANALYSIS or (
        intent is PrimaryIntent.COMPOSITION_ANALYSIS
        and (
            "structural_share_shift" in (requirements.get("methods") or [])
            or "share_shift" in (requirements.get("tool_selection", {}).get("capabilities") or [])
        )
    ):
        from app.analysis.tools.compiler import periods_from_proposal
        declared_periods = periods_from_proposal(structured)
        periods = declared_periods if declared_periods is not None else _comparison_periods(question)
        if periods is not None:
            from app.analysis.period_comparison import sort_field_key
            requirements['comparison_dimensions'] = [name for name in dimensions if not _is_time_dimension(name)]
            requirements['output_sort'] = [copy.deepcopy(item) for item in structured.get('排序') or []
                                           if sort_field_key(item.get('field'))]
            merge_mode = "period_roles"
            grouping = ("各" + "、".join(dimensions)) if dimensions else ""
            for index, (label, sort_key) in enumerate(periods):
                role = "基期" if index == 0 else "当前期"
                # 裸月份（"10月"）补全对齐后的年份，ASL 侧不需要再猜年份；
                # 年份来自原问题中的另一期间提及，不引入新时间条件
                if declared_periods is None and "年" not in label and sort_key[0] > 1900:
                    label = f"{sort_key[0]}年{label}"
                period_structured = _retrieval_extraction(structured)
                # Derived comparison columns do not exist in either SQL input.
                period_structured['排序'] = [item for item in period_structured.get('排序') or []
                                            if not sort_field_key(item.get('field'))]
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

    if (
        intent is PrimaryIntent.COMPARISON_ANALYSIS
        and not retrieval_specs
        and len(requirements.get("windows") or []) > 1
    ):
        # Relative comparison windows come from structured requirements, never
        # from prose in a discarded model calculation task.
        merge_mode = "period_roles"
        requirements["comparison_dimensions"] = [
            name for name in dimensions if not _is_time_dimension(name)
        ]
        for index, window in enumerate(requirements["windows"]):
            role = "当前期" if window.get("role") == "target" else "基期"
            period_structured = _retrieval_extraction(structured)
            granularity = copy.deepcopy(period_structured.get("时间粒度") or {})
            granularity["time_range"] = f"{window['start']}至{window['end']}"
            period_structured["时间粒度"] = granularity
            task_id = f"task-{index + 1}"
            grouping = [name for name in dimensions if not _is_time_dimension(name)]
            text = _window_query(
                granularity, requirements.get("grain"), metric_text, grouping, period_structured,
            )
            retrieval_specs.append({
                "task_id": task_id, "question": text,
                "name": ("同比基期：" if role == "基期" else "") + text[:54],
                "structured": period_structured,
            })
            binding_periods.append({
                "task": task_id, "label": granularity["time_range"], "role": role,
            })

    if intent is PrimaryIntent.ROOT_CAUSE_ANALYSIS and dimensions:
        # Keep both periods even if the extractor has retained only November.
        from app.analysis.tools.compiler import periods_from_proposal
        declared_periods = periods_from_proposal(structured)
        periods = declared_periods if declared_periods is not None else _comparison_periods(question)
        if periods:
            normalized = [f"{key[0]}年{key[1]}月" if declared_periods is None and key[0] > 1900 and key[1] else label
                          for label, key in periods]
            if declared_periods is not None:
                from app.analysis.tools.plans import plan_from_structured
                declared = plan_from_structured(structured).comparison_periods
                bounds = (min(p.start for p in declared).isoformat(),
                          (max(p.end_exclusive for p in declared)-timedelta(days=1)).isoformat())
            else:
                bounds = _absolute_window_bounds('和'.join(normalized))
            time_text = f"{bounds[0]}至{bounds[1]}" if bounds else '和'.join(normalized)
            structured = copy.deepcopy(structured)
            structured['时间粒度'] = {**(structured.get('时间粒度') or {}), 'time_range': time_text,
                                      'unit': (requirements.get('grain') if declared_periods is not None
                                               else '月' if any('月' in label for label in normalized) else '年')}
        # Independent marginal dimensions must never be added together.
        drill_dimensions = dimensions[:3]
        requirements = build_requirements(intent, question, structured, today=today)
        requirements['unexecuted_dimensions'] = dimensions[3:]
        granularity = structured.get('时间粒度') or {}
        time_dimension = next((name for name in _dimension_names(structured) if _is_time_dimension(name)),
                              granularity.get('anchor') or '业务日期')
        total_structured = _retrieval_extraction(structured, 维度=[time_dimension], 排序=[])
        retrieval_specs.append({
            "task_id": "task-1",
            "question": _window_query(granularity, requirements['grain'], metric_text, [time_dimension], structured),
            "name": _query_clause(time_text, "", metric_text)[:60],
            "structured": total_structured,
        })
        for offset, dimension in enumerate(drill_dimensions, start=2):
            retrieval_specs.append({
                "task_id": f"task-{offset}",
                "question": _window_query(granularity, requirements['grain'], metric_text, [time_dimension, dimension], structured),
                "name": _query_clause(time_text, f"各{dimension}", metric_text)[:60],
                "structured": _retrieval_extraction(structured, 维度=[time_dimension, dimension], 排序=[]),
            })

    if not retrieval_specs:
        # 趋势/占比/异常/预测及未命中两期拆分的对比：原取数任务保持不变，
        # 只把意图口径降为指标查询，分析计算交给独立分析任务；
        # 趋势取数窗口按环比基期方法需要前推一个粒度并披露扩展原因。
        # Oagnet 主路径按问题文本解析 time_context，扩展窗口必须写进
        # 执行问题文本才生效；任务表展示名保持用户声明的窗口。
        retrieval_structured = _retrieval_extraction(structured)
        retrieval_question = source_text or _query_clause(time_text, "", metric_text)
        display_start_label: str | None = None
        if intent is PrimaryIntent.TREND_ANALYSIS:
            windows = requirements.get("windows") or []
            if windows:
                window = windows[0]
                granularity = copy.deepcopy(structured.get("时间粒度") or {})
                granularity["time_range"] = f"{window['start']}至{window['end']}"
                granularity["window_alignment"] = {
                    "derived_from": time_text,
                    "reasons": ["环比基期单独参与比较，目标范围保持不变"]
                    if window["start"] != requirements["target_window"]["start"] else ["保留目标日期；同比基期单独查询"],
                }
                retrieval_structured["时间粒度"] = granularity
                retrieval_question = _window_query(granularity, requirements["grain"], metric_text, dimensions, structured)
                display_start_label = requirements["target_window"]["start"][:7]
            if not retrieval_structured.get("排序"):
                # 时序取数必须有序返回：引擎拒绝在未知顺序上计算趋势，
                # 折线图也会按返回顺序连线。整年窗口被改写成“2025年”后
                # 取数文本不再按时间分组，此时再带时间维度排序会变成
                # 未选中维度的排序，ASL 会判定无效（实测“2025年各业务员…”）。
                anchor = (retrieval_structured.get("时间粒度") or {}).get("anchor")
                declared_time = next(
                    (name for name in _dimension_names(structured)
                     if _is_time_dimension(name)),
                    None,
                )
                grouped_by_time = any(
                    marker in retrieval_question
                    for marker in ("分日", "分周", "分月", "分季度", "分半年", "分年")
                )
                if anchor or declared_time or grouped_by_time:
                    time_dimension = (
                        str(anchor).strip() if anchor else (declared_time or "业务日期")
                    )
                    if not declared_time:
                        retrieval_structured["维度"] = [
                            *(retrieval_structured.get("维度") or []), time_dimension,
                        ]
                    retrieval_structured["排序"] = [{"field": time_dimension, "order": "asc"}]
        retrieval_specs.append({
            "task_id": "task-1",
            "question": retrieval_question,
            "name": (task_name or source_text or _query_clause(time_text, "", metric_text))[:60],
            "structured": retrieval_structured,
        })
        if intent is PrimaryIntent.TREND_ANALYSIS:
            for window in (requirements.get("windows") or [])[1:]:
                baseline = _retrieval_extraction(structured)
                granularity = copy.deepcopy(baseline.get("时间粒度") or {})
                granularity["time_range"] = f"{window['start']}至{window['end']}"
                baseline["时间粒度"] = granularity
                baseline["排序"] = copy.deepcopy(retrieval_structured.get("排序") or [])
                text = _window_query(granularity, requirements["grain"], metric_text, dimensions, structured)
                retrieval_specs.append({"task_id": f"task-{len(retrieval_specs) + 1}",
                    "question": text, "name": "同比基期：" + text[:54], "structured": baseline})
    else:
        display_start_label = None

    analysis_structured = copy.deepcopy(structured)
    analysis_structured.pop("计算绑定", None)
    analysis_structured.pop("计算绑定来源", None)
    analysis_structured.pop("结果操作", None)
    if intent != extraction.intent:
        from app.analysis.intents import LABELS
        analysis_structured["意图"] = LABELS[intent]
    analysis_structured["是否查询数据库"] = "否"
    analysis_structured[_ANALYSIS_BINDING_KEY] = {
        "analysis_type": analysis_type,
        "intent": intent.value,
        "tool": playbook["tool"],
        "sub_tools": playbook["sub_tools"],
        "merge": merge_mode,
        "periods": binding_periods,
        "source_question": str(question or "").strip(),
        "display_window_start": display_start_label,
        "requirements": requirements,
        "optional_dependencies": (
            [spec["task_id"] for spec in retrieval_specs if spec["name"].startswith("同比基期：")]
            if intent is PrimaryIntent.TREND_ANALYSIS else []
        ),
    }
    selection = requirements.get('tool_selection') or {}
    if selection.get('mode') == 'SELECTED':
        from app.analysis.tool_selection import TOOL_NAMES
        names = [TOOL_NAMES.get(key, key) for key in selection['capabilities']]
        analysis_structured[_ANALYSIS_BINDING_KEY]['tool'] = '、'.join(names)
        analysis_structured[_ANALYSIS_BINDING_KEY]['sub_tools'] = {}
    retrieval_ids = [spec["task_id"] for spec in retrieval_specs]
    if selection.get('mode') == 'SELECTED':
        analysis_question = f"对{time_text}{metric_text}执行" + '、'.join(names)
    else:
        analysis_question = _analysis_question(
            intent, metric_text, time_text, dimensions, binding_periods,
            source_question=str(question or ""),
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
    *,
    source_question: str = "",
) -> str:
    if intent is PrimaryIntent.TREND_ANALYSIS:
        modes = trend_comparison_modes(source_question)
        return f"对{time_text}{metric_text}进行趋势分析（含{'、'.join(modes)}），识别拐点和连续涨跌区间"
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


def _window_query(granularity, grain, metric_text, dimensions, structured):
    grouping = {"day": "分日", "week": "分周", "month": "分月", "quarter": "分季度", "half_year": "分半年", "year": "分年"}.get(grain, "")
    time_text = str(granularity.get("time_range") or "")
    # 整年区间写成“2025年”，不叠加“分年”和日期区间：粘连长句会被规则解析
    # 误当成商品筛选值（如“2025-01-01至2025-12-31分年各业务员”）。
    single_year = re.fullmatch(r"(\d{4})\s*-\s*01\s*-\s*01\s*[至~]\s*\1\s*-\s*12\s*-\s*31", time_text)
    if single_year is not None:
        time_text = single_year.group(1) + "年"
        if grain == "year":
            grouping = ""
    if dimensions:
        grouping += "各" + "、".join(dimensions)
    text = _query_clause(time_text, grouping, metric_text)
    if granularity.get("anchor"):
        text += "；统计日期口径：" + str(granularity["anchor"])
    conditions = []
    for item in structured.get("过滤条件") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        if item.get("entity"):
            field = str(item["entity"]) + "的" + field
        conditions.append(f"{field}{item.get('op') or '='}{item.get('value')}")
    if conditions:
        text += "；筛选条件：" + "、".join(conditions)
    return text


def _local_result_task(task: AtomicTask) -> bool:
    extraction = task.extraction or {}
    return bool(task.depends_on) and (
        _NO_DATABASE_MARK in task.question
        or str(extraction.get("是否查询数据库") or "").startswith("否")
    )


def _dependency_closure(plan: TaskPlan, terminal: AtomicTask) -> set[str]:
    by_id = {task.task_id: task for task in plan.tasks}
    remaining = list(terminal.depends_on)
    result = {terminal.task_id}
    while remaining:
        task_id = remaining.pop()
        if task_id in result:
            continue
        result.add(task_id)
        task = by_id.get(task_id)
        if task is not None:
            remaining.extend(task.depends_on)
    return result


def normalize_analysis_task_plan(
    question: str,
    plan: TaskPlan | None,
    intent: PrimaryIntent | None,
) -> TaskPlan | None:
    """Give model-preplanned DAGs the same analysis contract as single tasks.

    A prose task name or a sort operation is not an analysis deliverable.  A
    self-contained analysis subgraph can be replaced by its canonical playbook;
    mixed plans retain unrelated tasks and only replace their local analysis
    terminal when no additional retrieval is required.
    """
    if plan is None or intent not in ANALYSIS_PLAYBOOKS:
        return plan
    expected_type = ANALYSIS_PLAYBOOKS[intent]["analysis_type"]

    def has_real_calculation(task: AtomicTask) -> bool:
        extraction = task.extraction or {}
        calculation = extraction.get("计算绑定") or {}
        operation = (extraction.get("结果操作") or {}).get("operation") or {}
        steps = operation.get("operations") or operation.get("steps") or [operation]

        def explicit_result(field):
            return bool(field and str(field) in question)

        registered = bool(calculation) and (
            extraction.get("计算绑定来源") in {"REGISTERED_COMPOUND", "DECLARED_CALCULATION"}
        )
        return registered or any(
            isinstance(step, dict) and step.get("type") == "aggregate"
            and any(explicit_result(item.get("alias"))
                    for item in step.get("aggregations") or [] if isinstance(item, dict))
            for step in steps
        )

    matching_ids = {
        task.task_id for task in plan.tasks
        if isinstance((task.extraction or {}).get(_ANALYSIS_BINDING_KEY), dict)
        and (task.extraction or {})[_ANALYSIS_BINDING_KEY].get("analysis_type") == expected_type
    }
    from app.analysis.tools.plans import plan_from_structured
    catalog_proposal = next((plan_from_structured(task.extraction) for task in plan.tasks
                             if (task.extraction or {}).get('_analysis_tool_plan')), None)
    if (
        catalog_proposal is not None
        and len(matching_ids) == 1
        and all(
            (not task.depends_on and not _local_result_task(task))
            or task.task_id in matching_ids
            or (_local_result_task(task) and has_real_calculation(task))
            for task in plan.tasks
        )
    ):
        return plan
    candidates = [task for task in plan.tasks if _local_result_task(task)]
    terminal = candidates[-1] if candidates else None
    by_id = {task.task_id: task for task in plan.tasks}
    sources = (
        [by_id[task_id] for task_id in terminal.depends_on if task_id in by_id]
        if terminal is not None
        else [task for task in plan.tasks if not task.depends_on and not _local_result_task(task)]
    )
    if not sources:
        from app.planning.task_dag import TaskPlanningError
        raise TaskPlanningError("分析目标没有可用的前序取数依赖")
    seed_source = sources[0]
    if (
        _local_result_task(seed_source)
        or seed_source.depends_on
        or not _metric_text(seed_source.extraction or {})
    ):
        closure = _dependency_closure(plan, terminal) if terminal is not None else set()
        retrieval_sources = [
            task for task in plan.tasks
            if task.task_id in closure and not _local_result_task(task)
        ]
        if not retrieval_sources:
            from app.planning.task_dag import TaskPlanningError
            raise TaskPlanningError("分析目标不能仅依赖另一个分析任务的派生结果")
        seed_source = retrieval_sources[0]
    seed = copy.deepcopy(seed_source.extraction or {})
    seed["是否查询数据库"] = "是"
    canonical = build_analysis_task_plan(
        question,
        PlannerExtraction(intent=intent, parameters=[], structured=seed),
        task_question=seed_source.question,
        task_name=seed_source.name,
    )
    if canonical is None:
        from app.planning.task_dag import TaskPlanningError
        raise TaskPlanningError("分析目标无法展开为规范分析链路")
    if terminal is None:
        # A collapsed plan still carries the immutable root goal. Recompile the
        # canonical DAG instead of requiring the deleted local node to reappear.
        return canonical
    terminal_extraction = terminal.extraction or {}
    real_calculation = has_real_calculation(terminal)
    if not real_calculation and _dependency_closure(plan, terminal) == {
        task.task_id for task in plan.tasks
    }:
        return canonical

    # Mixed deliverables and real calculations must retain their existing
    # inputs.  Only a weak analysis terminal may be replaced together with its
    # canonical retrieval subgraph.
    if not real_calculation and len(canonical.tasks) != 2:
        from app.planning.task_dag import TaskPlanningError
        raise TaskPlanningError("混合任务中的分析目标需要额外取数窗口，未执行不完整计划")
    canonical_analysis = canonical.tasks[-1]
    tasks = list(plan.tasks)
    if real_calculation and len(tasks) >= 12:
        from app.planning.task_dag import TaskPlanningError
        raise TaskPlanningError("混合任务超过十二节点上限，无法追加分析交付节点")
    analysis_id = terminal.task_id
    if real_calculation:
        used_ids = {task.task_id for task in tasks}
        suffix = len(tasks) + 1
        while f"task-{suffix}" in used_ids:
            suffix += 1
        analysis_id = f"task-{suffix}"
    analysis = canonical_analysis.model_copy(deep=True)
    analysis.task_id = analysis_id
    analysis.name = terminal.name or analysis.name
    analysis.depends_on = [terminal.task_id] if real_calculation else list(terminal.depends_on)
    analysis_extraction = copy.deepcopy(terminal_extraction if real_calculation else sources[0].extraction or {})
    analysis_extraction.pop("计算绑定", None)
    analysis_extraction.pop("计算绑定来源", None)
    analysis_extraction.pop("结果操作", None)
    analysis_extraction["是否查询数据库"] = "否"
    analysis_extraction[_ANALYSIS_BINDING_KEY] = copy.deepcopy(
        canonical_analysis.extraction[_ANALYSIS_BINDING_KEY]
    )
    analysis_extraction[_ANALYSIS_BINDING_KEY]["merge"] = "concat"
    analysis_extraction[_ANALYSIS_BINDING_KEY]["periods"] = []
    analysis_extraction[_ANALYSIS_BINDING_KEY]["optional_dependencies"] = []
    analysis.extraction = analysis_extraction
    analysis.parameters = list(extraction_to_parameters(analysis_extraction))
    if real_calculation:
        tasks.append(analysis)
    else:
        tasks[tasks.index(terminal)] = analysis
    return plan.model_copy(update={"tasks": tasks})


def validate_task_plan_execution_contract(
    plan: TaskPlan | None,
    intent: PrimaryIntent | None,
) -> None:
    """Reject incomplete core deliverables before any query is executed."""
    if plan is None:
        return
    from app.planning.task_dag import TaskPlanningError
    by_id = {task.task_id: task for task in plan.tasks}
    for task in plan.tasks:
        extraction = task.extraction or {}
        calculation = extraction.get("计算绑定")
        operation = extraction.get("结果操作")
        binding = extraction.get(_ANALYSIS_BINDING_KEY)
        if calculation is not None and operation is not None:
            raise TaskPlanningError(f"任务{task.task_id}同时声明计算公式与结果操作")
        if calculation is not None and (
            not isinstance(calculation, dict) or not str(calculation.get("result_field") or "").strip()
        ):
            raise TaskPlanningError(f"任务{task.task_id}缺少明确的计算结果字段")
        if binding is not None and (
            not isinstance(binding, dict) or not binding.get("analysis_type") or not task.depends_on
        ):
            raise TaskPlanningError(f"任务{task.task_id}的分析绑定或输入依赖不完整")
        if isinstance(binding, dict):
            if any(
                isinstance((by_id.get(task_id).extraction or {}).get(_ANALYSIS_BINDING_KEY), dict)
                for task_id in task.depends_on
                if by_id.get(task_id) is not None
            ):
                raise TaskPlanningError(f"任务{task.task_id}不得消费另一个分析任务的派生结果")
            requirements = binding.get("requirements") or {}
            selection = requirements.get("tool_selection") or {}
            capabilities = selection.get("capabilities") or []
            if selection.get("mode") == "SELECTED" and not capabilities:
                raise TaskPlanningError(f"任务{task.task_id}缺少明确的指定分析能力")
            if "share_shift" in capabilities and (
                binding.get("merge") != "period_roles"
                or {item.get("role") for item in binding.get("periods") or []} != {"基期", "当前期"}
            ):
                raise TaskPlanningError(f"任务{task.task_id}的结构变化分析缺少基期和本期输入")
        if _local_result_task(task) and calculation is None and operation is None and binding is None:
            raise TaskPlanningError(f"任务{task.task_id}缺少可执行的计算或分析合同")
    if intent in ANALYSIS_PLAYBOOKS:
        expected_type = ANALYSIS_PLAYBOOKS[intent]["analysis_type"]
        matching = [
            task for task in plan.tasks
            if isinstance((task.extraction or {}).get(_ANALYSIS_BINDING_KEY), dict)
            and (task.extraction or {})[_ANALYSIS_BINDING_KEY].get("analysis_type") == expected_type
        ]
        if not matching:
            raise TaskPlanningError("用户分析目标未绑定对应分析链路")
        if len(matching) != 1:
            raise TaskPlanningError("用户分析目标必须且只能绑定一个分析终端")


def standardize_period_analysis_plan(question, decomposition):
    """Canonicalize same-scope period queries without losing any input window.

    Models sometimes preplan target + last year + an unbound local formula.
    This is still one analysis goal. Only interchangeable retrieval shapes
    whose entire windows are covered by the full playbook may be replaced.
    Registered arithmetic and distinct business scopes retain their own DAG.
    """
    import json
    tasks = decomposition.tasks
    output_sort = []
    if re.search(r'平方|立方|开根|方差|标准差|中位数|分位数|协方差|相关系数|对数|取余|阶乘|绝对值', question):
        return None
    if any((task.structured or {}).get('分析绑定') or (task.structured or {}).get('计算绑定') for task in tasks):
        return None
    queries = [task for task in tasks if str((task.structured or {}).get('是否查询数据库') or '').startswith('是')
               and '不再查询数据库' not in task.question]
    if len(queries) < 2 or any(task.depends_on for task in queries):
        return None
    for task in tasks:
        if task in queries:
            continue
        operation = ((task.structured or {}).get('结果操作') or {}).get('operation') or {}
        steps = operation.get('operations') or operation.get('steps') or [operation]
        if any(not isinstance(step, dict) or step.get('type') not in {None, 'derive', 'aggregate', 'sort'}
               or step.get('type') == 'derive' and step.get('operator') not in {'add', 'subtract', 'multiply', 'divide'} for step in steps):
            return None
        derived = {step.get('result_field') for step in steps if step.get('type') == 'derive'}
        for step in steps:
            if step.get('type') == 'sort':
                if step.get('field') not in derived or step.get('direction', 'asc') not in {'asc', 'desc'}:
                    return None
                output_sort.append({'field': step['field'], 'order': step.get('direction', 'asc')})
    def shape(task):
        value = task.structured or {}
        return json.dumps({key: value.get(key) for key in ('业务域', '实体', '指标', '维度', '展示字段', '过滤条件', '限制')},
                          ensure_ascii=False, sort_keys=True) + json.dumps({key: (value.get('时间粒度') or {}).get(key)
                          for key in ('unit', 'anchor')}, ensure_ascii=False, sort_keys=True)
    if len({shape(task) for task in queries}) != 1:
        return None
    windows = [_absolute_window_bounds(_time_text(task.structured or {})) for task in queries]
    if any(window is None for window in windows):
        return None
    seed = queries[max(range(len(queries)), key=lambda index: windows[index][0])]
    intent = decomposition.intent or seed.intent
    if intent not in ANALYSIS_PLAYBOOKS:
        return None
    from app.analysis.period_comparison import sort_field_key
    if output_sort and (intent != PrimaryIntent.COMPARISON_ANALYSIS or any(
            not sort_field_key(item['field']) for item in output_sort)):
        return None
    structured = copy.deepcopy(seed.structured)
    if output_sort:
        structured['排序'] = output_sort
    source_bounds = _absolute_window_bounds(question) if intent == PrimaryIntent.TREND_ANALYSIS else None
    if source_bounds:
        structured['时间粒度']['time_range'] = f'{source_bounds[0]}至{source_bounds[1]}'
    plan = build_analysis_task_plan(question, PlannerExtraction(intent=intent,
        parameters=list(seed.parameters), structured=structured))
    if plan is None:
        return None
    planned_windows = [_absolute_window_bounds(_time_text(task.extraction or {})) for task in plan.tasks[:-1]]
    if any(not any(target and target[0] <= source[0] and target[1] >= source[1]
                   for target in planned_windows) for source in windows):
        return None
    return plan
