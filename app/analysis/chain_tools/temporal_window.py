"""趋势/环比/同比链路共用的取数窗口对齐与边桶完整性检测。

查询日期与分组粒度独立。滚动日期保留日界，只有明确要求完整自然周期
才对齐周期边界；比较基期由分析规划器单独登记，不改变目标窗口。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

# Time parsing and calendar validation remain deterministic. Analysis capability
# selection belongs to the model plan, never to these date utilities.


def _model_plan_required(*args, **kwargs):
    from app.analysis.tools.plans import AnalysisToolPlanRequired
    raise AnalysisToolPlanRequired('ANALYSIS_TOOL_PLAN_REQUIRED')


# Compatibility names fail explicitly; none inspects question text.
period_change_series_question = _model_plan_required
two_period_change_question = _model_plan_required

_ROLLING_TOKEN = re.compile(
    r"(?:最近|近|过去)\s*([\d一二两三四五六七八九十]+)\s*个?\s*(半月|年|季度|季|月|周|星期|天|日)"
)
_RANGE_SPLIT = re.compile(r"\s*(?:至|到|[~～])\s*")
_ABSOLUTE_PERIOD = re.compile(
    r"(20\d{2})\s*[-/年]\s*(\d{1,2})(?:\s*[-/月]\s*(\d{1,2})\s*日?)?\s*月?"
)
_QUARTER_LABEL = re.compile(r"(20\d{2})\s*[-/年]?\s*(?:[Qq]|第)\s*([1-4一二三四])(?:季度|季)?")
_WEEK_LABEL = re.compile(r"(20\d{2})-?[Ww](\d{1,2})")
_MONTH_LABEL = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*月?")
_YEAR_LABEL = re.compile(r"(20\d{2})\s*年?")

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 分组粒度到桶跨月数的映射；周粒度单独处理。
_BUCKET_MONTHS = {"month": 1, "quarter": 3, "half_year": 6, "year": 12}
_GRAIN_ALIASES = {
    "日": "day", "天": "day", "day": "day",
    "月": "month", "月份": "month", "month": "month", "months": "month",
    "季度": "quarter", "季": "quarter", "quarter": "quarter",
    "半年": "half_year", "half_year": "half_year",
    "年": "year", "年度": "year", "自然年": "year", "year": "year",
    "周": "week", "星期": "week", "week": "week",
}
_RANGE_UNIT_MONTHS = {"半月": 6, "年": 12, "月": 1, "季度": 3, "季": 3}


def normalize_grain(unit: Any) -> str | None:
    if unit is None:
        return None
    return _GRAIN_ALIASES.get(str(unit).strip().casefold())


has_pop_signal = _model_plan_required
has_yoy_signal = _model_plan_required


def _cn_number(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text.count("十") == 1:
        left, right = text.split("十")
        if (not left or left in _CN_DIGITS) and (not right or right in _CN_DIGITS):
            return (_CN_DIGITS.get(left, 1) if left else 1) * 10 + (_CN_DIGITS.get(right, 0) if right else 0)
    return None


def _month_index(value: date) -> int:
    return value.year * 12 + value.month - 1


def _first_day(index: int) -> date:
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _last_day(index: int) -> date:
    year, month = divmod(index, 12)
    return date(year, month + 1, calendar.monthrange(year, month + 1)[1])


def _shift_months(value: date, months: int) -> date:
    return _first_day(_month_index(value) + months)


def _rolling_window_spec(time_range: str) -> tuple[int, str] | None:
    """把“最近N个月/季/年/周/天”换算成 (数量, 单位)。

    月类单位统一折算成总月数；周单位返回周数，由调用方按周对齐；
    天单位原样返回天数。非滚动表述返回 None。
    """
    match = _ROLLING_TOKEN.fullmatch(str(time_range or "").strip())
    if match is None:
        return None
    amount = _cn_number(match.group(1))
    if amount is None or amount <= 0:
        return None
    unit_word = match.group(2)
    if unit_word in {"天", "日"}:
        return amount, "day"
    if unit_word in {"周", "星期"}:
        return amount, "week"
    return amount * _RANGE_UNIT_MONTHS[unit_word], "month"


def _align_full_buckets(total_months: int, grain: str, today: date) -> tuple[date, date] | None:
    """返回最近 N 个完整桶的闭区间；桶数向下取整，不足一个桶时不改写。"""
    bucket_months = _BUCKET_MONTHS.get(grain)
    if bucket_months is None:
        return None
    bucket_count = total_months // bucket_months
    if bucket_count < 1:
        return None
    if grain == "month":
        # 当月未完结，最近完整月是上个月。
        end_index = _month_index(today) - 1
        start = _first_day(end_index - bucket_count + 1)
        return start, _last_day(end_index)
    if grain in {"quarter", "half_year", "year"}:
        current_bucket = _month_index(today) // bucket_months
        end_index = current_bucket * bucket_months + bucket_months - 1
        # 当桶未完结时退到上一桶。
        if end_index >= _month_index(today):
            end_index -= bucket_months
        start = _first_day(end_index - bucket_months * bucket_count + 1)
        return start, _last_day(end_index)
    return None


def _align_full_weeks(weeks: int, today: date) -> tuple[date, date] | None:
    if weeks < 1:
        return None
    # 当周未完结，最近完整周是上周；ISO 周一为一周起点。
    last_sunday = today - timedelta(days=today.weekday() + 1)
    start = last_sunday - timedelta(days=7 * weeks - 1)
    return start, last_sunday


def _parse_absolute_period(text: str) -> tuple[date, date] | None:
    """解析单个绝对期间（年/半年/月/日），返回该期间的闭区间。"""
    compact = str(text or "").strip().replace(" ", "").replace("年份", "年").replace("月份", "月")
    year_only = _YEAR_LABEL.fullmatch(compact)
    if year_only:
        year = int(year_only.group(1))
        return date(year, 1, 1), date(year, 12, 31)
    quarter = _QUARTER_LABEL.fullmatch(compact)
    if quarter:
        number = quarter.group(2)
        number = int(number) if number.isdigit() else _CN_DIGITS[number]
        start = date(int(quarter.group(1)), (number - 1) * 3 + 1, 1)
        return start, _last_day(_month_index(start) + 2)
    half_year = re.fullmatch(r"(20\d{2})\s*年?\s*(上|下)半年(?:度)?", compact)
    if half_year is not None:
        year = int(half_year.group(1))
        first_month = 1 if half_year.group(2) == "上" else 7
        start = date(year, first_month, 1)
        return start, _last_day(_month_index(start) + 5)
    match = _ABSOLUTE_PERIOD.fullmatch(compact)
    if match is None:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), match.group(3)
    if not 1 <= month <= 12:
        return None
    if day is None:
        return _first_day(_month_index(date(year, month, 1))), _last_day(_month_index(date(year, month, 1)))
    day = int(day)
    try:
        point = date(year, month, day)
    except ValueError:
        return None
    return point, point


def parse_absolute_range(time_range: str) -> tuple[date, date] | None:
    """解析"X至Y"绝对区间或单个绝对期间；无法确定起止时返回 None。

    "2025年7月至12月"这类终止段缺年份的表述，终止段继承起点年份。
    """
    parts = _RANGE_SPLIT.split(str(time_range or "").strip())
    if len(parts) == 1:
        return _parse_absolute_period(parts[0])
    if len(parts) != 2:
        return None
    first = _parse_absolute_period(parts[0])
    last = _parse_absolute_period(parts[1])
    if last is None and first is not None:
        anchor_year = re.search(r"20\d{2}", parts[0])
        if anchor_year is not None:
            last = _parse_absolute_period(f"{anchor_year.group(0)}年{parts[1]}")
    if first is None or last is None or last[1] < first[0]:
        return None
    return first[0], last[1]


_DISJUNCTIVE_SPLIT = re.compile(r"\s*(?:和|及|、|，|,)\s*")


def disjunctive_periods_to_range(text: str) -> str | None:
    """“X和Y”“X、Y”式离散期间改写为覆盖区间。

    两期对比（“2025年11月和10月”）与月份列举（“2025年10月、11月、
    12月”）取数时都需要一段连续窗口，按月分组后各期间自然成分开
    的行。无年份的分段沿用首个带年份分段的年份；任何分段无法解析、
    或全程无年份时不改写，交给下游按缺口径追问，不猜测年份。
    返回“起始至结束”文本；非离散列举返回 None。
    """
    raw = str(text or "").strip()
    if not raw or not _DISJUNCTIVE_SPLIT.search(raw):
        return None
    parts = [part for part in _DISJUNCTIVE_SPLIT.split(raw) if part.strip()]
    if len(parts) < 2:
        return None
    anchor_year: str | None = None
    for part in parts:
        match = re.search(r"(20\d{2})", part)
        if match is not None:
            anchor_year = match.group(1)
            break
    if anchor_year is None:
        return None
    parsed: list[tuple[date, date]] = []
    for part in parts:
        segment = part.strip()
        if re.search(r"20\d{2}", segment) is None:
            segment = f"{anchor_year}年{segment}"
        period = _parse_absolute_period(segment)
        if period is None:
            return None
        parsed.append(period)
    start = min(period[0] for period in parsed)
    end = max(period[1] for period in parsed)
    return f"{start.isoformat()}至{end.isoformat()}"


def align_structured_time_window(
    question: str,
    time_grain: dict[str, Any] | None,
    *,
    intent: str,
    today: date,
) -> dict[str, Any] | None:
    """趋势链路的取数窗口改写入口。

    返回改写后的“时间粒度”dict（深拷贝），未命中改写条件时返回 None。
    改写仅作用于发给 ASL 的副本，请求侧的原始提取保持不变。
    """
    if not isinstance(time_grain, dict):
        return None
    if str(intent) != "TREND_ANALYSIS":
        return None
    time_range = str(time_grain.get("time_range") or "").strip()
    if not time_range:
        return None
    grain = normalize_grain(time_grain.get("unit"))
    if grain is None:
        return None
    # 此兼容入口只解析目标日期，绝不隐式加入同比或环比基期。
    window = resolve_target_window(time_range, grain, today=today)
    if window is None or parse_absolute_range(time_range) is not None:
        return None
    rewritten = dict(time_grain)
    rewritten["time_range"] = f"{window[0].isoformat()}至{window[1].isoformat()}"
    rewritten["window_alignment"] = {
        "derived_from": time_range,
        "reasons": ["按用户日期语义解析目标窗口；分组粒度不改变日期范围"],
    }
    return rewritten


def shift_calendar_months(value: date, months: int) -> date:
    """日历月回推，保留日号；闰日等超出目标月时截到合法月末。"""
    year, month = divmod(_month_index(value) + months, 12)
    return date(year, month + 1, min(value.day, calendar.monthrange(year, month + 1)[1]))


def resolve_target_window(text: str, grain: str | None, *, today: date) -> tuple[date, date] | None:
    absolute = parse_absolute_range(text)
    if absolute:
        return absolute
    covering = disjunctive_periods_to_range(text)
    if covering:
        return parse_absolute_range(covering)
    raw = str(text or "").strip()
    complete = "完整" in raw
    normalized = raw.replace("完整", "").replace("自然", "")
    rolling = _rolling_window_spec(normalized)
    if rolling is None:
        return None
    amount, unit = rolling
    if complete:
        if unit == "week":
            return _align_full_weeks(amount, today)
        if unit == "day":
            # 完整天数窗口没有可对齐的日历桶，保持原表述不改写
            return None
        return _align_full_buckets(amount, grain or "month", today)
    if unit == "day":
        return today - timedelta(days=amount), today
    if unit == "week":
        return today - timedelta(weeks=amount), today
    return shift_calendar_months(today, -amount), today


def bucket_bounds(label: str, grain: str | None = None) -> tuple[date, date] | None:
    """共享周期身份，供范围裁剪和连续性校验使用。"""
    for candidate in ([grain] if grain else ["day", "month", "quarter", "week", "year"]):
        if candidate:
            result = _bucket_bounds(label, candidate)
            if result:
                return result
    return None


def in_target_window(label: str, start: date, end_exclusive: date, grain: str | None = None) -> bool:
    bounds = bucket_bounds(label, grain)
    return bool(bounds and bounds[0] < end_exclusive and bounds[1] > start)


def extend_window_for_pop_baseline(time_grain: dict[str, Any]) -> dict[str, Any] | None:
    """环比基期扩展：把绝对取数区间的起点向前推一个粒度桶。

    趋势分析要算出展示窗口首期的环比，必须多读一期基期数据；扩展只
    作用于发给 ASL 的时间粒度副本，扩展原因记入 window_alignment 披露。
    输入无法解析为绝对区间（含"X至Y"或"X、Y"列举式）时不改写。
    """
    if not isinstance(time_grain, dict):
        return None
    time_range = str(time_grain.get("time_range") or "").strip()
    if not time_range:
        return None
    grain = normalize_grain(time_grain.get("unit")) or "month"
    absolute = parse_absolute_range(time_range)
    if absolute is None:
        covering = disjunctive_periods_to_range(time_range)
        if covering is not None:
            absolute = parse_absolute_range(covering)
    if absolute is None:
        return None
    bucket_months = _BUCKET_MONTHS.get(grain)
    if bucket_months is None:
        return None
    start, end = absolute
    start = _shift_months(start, -bucket_months)
    rewritten = dict(time_grain)
    rewritten["time_range"] = f"{start.isoformat()}至{end.isoformat()}"
    alignment = dict(rewritten.get("window_alignment") or {})
    alignment.setdefault("derived_from", time_range)
    alignment["reasons"] = [
        *alignment.get("reasons", []),
        "环比计算需要上一期基期，取数窗口起点已向前扩展1个粒度",
    ]
    rewritten["window_alignment"] = alignment
    return rewritten


def month_bucket_index(label: str) -> int | None:
    """把月桶标签解析成全局月序号（year*12+month-1）。

    无年份的标签（如“10月”）无法定位所属年份，返回 None，由调用方
    按不可对齐处理而不是猜测年份。
    """
    match = _MONTH_LABEL.fullmatch(str(label or "").strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year * 12 + month - 1


def _bucket_bounds(label: str, grain: str) -> tuple[date, date] | None:
    """把桶标签解析成 [start, end_exclusive)；无法解析时返回 None。"""
    text = str(label or "").strip()
    if grain == "week":
        match = _WEEK_LABEL.fullmatch(text)
        if match is None:
            return None
        try:
            start = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            return None
        return start, start + timedelta(days=7)
    if grain == "quarter":
        match = _QUARTER_LABEL.fullmatch(text)
        if match is None:
            return None
        quarter = match.group(2)
        quarter = int(quarter) if quarter.isdigit() else _CN_DIGITS[quarter]
        if not 1 <= quarter <= 4:
            return None
        year = int(match.group(1))
        start = date(year, (quarter - 1) * 3 + 1, 1)
        return start, _shift_months(start, 3)
    if grain == "year":
        match = _YEAR_LABEL.fullmatch(text)
        if match is None:
            return None
        year = int(match.group(1))
        return date(year, 1, 1), date(year + 1, 1, 1)
    if grain == "month":
        match = _MONTH_LABEL.fullmatch(text)
        if match is None:
            return None
        month = int(match.group(2))
        if not 1 <= month <= 12:
            return None
        start = date(int(match.group(1)), month, 1)
        return start, _shift_months(start, 1)
    if grain == "day":
        try:
            point = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return point, point + timedelta(days=1)
    return None


def detect_edge_bucket_alignment(
    labels: list[str],
    window_start: date | None,
    window_end_exclusive: date | None,
    grain: str,
) -> dict[str, Any]:
    """检测被查询窗口边界截断的首/尾桶。

    首桶起点早于窗口起点，或尾桶终点晚于窗口终点时，该桶只包含部分
    期间数据，直接参与逐期比较会得到半月对全月的失真结果。
    """
    result: dict[str, Any] = {
        "first_bucket_partial": False,
        "first_partial_label": None,
        "last_bucket_partial": False,
        "last_partial_label": None,
    }
    if window_start is None or window_end_exclusive is None or not labels:
        return result
    first = _bucket_bounds(labels[0], grain)
    if first is not None and first[0] < window_start:
        result["first_bucket_partial"] = True
        result["first_partial_label"] = labels[0]
    last = _bucket_bounds(labels[-1], grain)
    if last is not None and last[1] > window_end_exclusive:
        result["last_bucket_partial"] = True
        result["last_partial_label"] = labels[-1]
    return result
