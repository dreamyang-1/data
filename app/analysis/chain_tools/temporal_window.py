"""趋势/环比/同比链路共用的取数窗口对齐与边桶完整性检测。

月粒度趋势要求每个数据点都是完整自然月：滑动起点（如“最近12个月”
从今天起算得到的月中日期）会把首个月桶截成半月，导致首尾比较和环比
全部失真。本模块在结构化提取发出 ASL 之前把相对范围改写成桶对齐的
绝对区间，并在引擎侧检测窗口边界截断的首/尾桶。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

# 触发环比/同比计算的提问词表；编排器、规划器与引擎共用同一份定义，
# 避免各处词表漂移后行为不一致。
POP_SIGNALS = ("环比", "比上月", "较上月", "上月相比", "逐期环比")
YOY_SIGNALS = ("同比", "比去年同期", "去年同期相比", "较上年同期", "比上年同期")

_ROLLING_TOKEN = re.compile(
    r"(?:最近|近|过去)\s*([\d一二两三四五六七八九十]+)\s*个?\s*(半月|年|月|季度|季|周|星期)"
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


def has_pop_signal(question: str) -> bool:
    compact = str(question or "").replace(" ", "")
    return any(token in compact for token in POP_SIGNALS)


def has_yoy_signal(question: str) -> bool:
    compact = str(question or "").replace(" ", "")
    return any(token in compact for token in YOY_SIGNALS)


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
    """把“最近N个月/季/年/周”换算成 (数量, 单位)。

    月类单位统一折算成总月数；周单位返回周数，由调用方按周对齐。
    非滚动表述返回 None。
    """
    match = _ROLLING_TOKEN.fullmatch(str(time_range or "").strip())
    if match is None:
        return None
    amount = _cn_number(match.group(1))
    if amount is None or amount <= 0:
        return None
    unit_word = match.group(2)
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
    """解析单个绝对期间（年/月/日），返回该期间的闭区间。"""
    compact = str(text or "").strip().replace(" ", "").replace("年份", "年").replace("月份", "月")
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
    """解析“X至Y”形式的绝对区间；无法确定起止时返回 None。"""
    parts = _RANGE_SPLIT.split(str(time_range or "").strip())
    if len(parts) != 2:
        return None
    first = _parse_absolute_period(parts[0])
    last = _parse_absolute_period(parts[1])
    if first is None or last is None or last[1] < first[0]:
        return None
    return first[0], last[1]


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
    compact_question = str(question or "").replace(" ", "")
    wants_yoy = any(token in compact_question for token in YOY_SIGNALS)

    aligned_start: date | None = None
    aligned_end: date | None = None
    notes: list[str] = []

    rolling = _rolling_window_spec(time_range)
    if rolling is not None:
        amount, unit = rolling
        if unit == "week":
            window = _align_full_weeks(amount, today) if grain == "week" else None
        else:
            window = _align_full_buckets(amount, grain, today)
        if window is not None:
            aligned_start, aligned_end = window
            notes.append(f"相对范围“{time_range}”已对齐为{grain}桶边界")

    if aligned_start is None and wants_yoy:
        # 绝对区间只需为同比扩展基期，无需桶对齐。
        absolute = parse_absolute_range(time_range)
        if absolute is not None:
            aligned_start, aligned_end = absolute

    if aligned_start is None or aligned_end is None:
        return None
    if wants_yoy:
        aligned_start = _shift_months(aligned_start, -12)
        notes.append("同比计算需要上年同期基期，窗口起点已向前扩展12个月")

    rewritten = dict(time_grain)
    rewritten["time_range"] = f"{aligned_start.isoformat()}至{aligned_end.isoformat()}"
    rewritten["window_alignment"] = {
        "derived_from": time_range,
        "reasons": notes,
    }
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
