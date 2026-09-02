from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ConversationControl,
    MetricRef,
    PrimaryIntent,
    TimeRange,
    TrustedIdentity,
)


_YEAR_MONTH_SEPARATOR = r"(?:年|[-/.])"
_MONTH_DAY_SEPARATOR = r"(?:月|[-/.])"
_DAY_SUFFIX = r"(?:日|号)?"
_RANGE_SEPARATOR = r"(?:到|至|~|～|—|–|－|-)"
_WORD_RANGE_SEPARATOR = r"(?:到|至|~|～|—|–|－)"
_NATIVE_DATE_CLASS = date
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


_EXECUTION_ACTIONS = {
    PrimaryIntent.METRIC_QUERY: "查询指标",
    PrimaryIntent.DETAIL_QUERY: "查询明细",
    PrimaryIntent.TREND_ANALYSIS: "分析趋势",
    PrimaryIntent.COMPARISON_ANALYSIS: "执行比较分析",
    PrimaryIntent.COMPOSITION_ANALYSIS: "分析构成占比",
    PrimaryIntent.ANOMALY_ANALYSIS: "检测异常",
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: "执行归因分析",
    PrimaryIntent.FORECAST_ANALYSIS: "执行预测",
    PrimaryIntent.REPORT_GENERATION: "生成分析报告",
    PrimaryIntent.METRIC_DEFINITION: "查询指标口径",
    PrimaryIntent.DATA_LINEAGE: "查询数据血缘",
    PrimaryIntent.DATA_QUALITY: "检查数据质量",
}


def render_execution_question(
    request: CanonicalAnalysisRequest, *, confirmation: str | None = None
) -> str:
    """Render one conflict-free downstream question from canonical slots.

    ``original_question`` remains the audit trail. This renderer is the only
    text sent to semantic/ASL services after clarification merging, so removed
    or corrected values cannot leak from an earlier user utterance.
    """
    action = _EXECUTION_ACTIONS.get(request.primary_intent)
    if action is None:
        return request.original_question
    parts = [action]
    if request.metrics:
        metrics = [item.canonical_name or item.input for item in request.metrics]
        parts.append("指标：" + "、".join(dict.fromkeys(metrics)))
    if request.entity:
        parts.append("对象：" + request.entity)
    if request.fields:
        parts.append("返回字段：" + "、".join(dict.fromkeys(request.fields)))
    if request.dimensions:
        parts.append("分析维度：" + "、".join(dict.fromkeys(request.dimensions)))
    if request.time_range:
        end_inclusive = request.time_range.end_exclusive - timedelta(days=1)
        parts.append(
            f"时间范围：{request.time_range.start.isoformat()} 至 "
            f"{end_inclusive.isoformat()}（含首尾）"
        )
    elif "TIME_SCOPE=ALL_AVAILABLE_HISTORY" in request.assumptions:
        parts.append("时间范围：数据源全部可用历史")
    if "TRANSACTION_TIME_SCOPE=SALES_RECORD" in request.assumptions:
        parts.append("时间口径：按销售记录时间统计")
    granularity_labels = {
        "day": "日", "week": "周", "month": "月",
        "quarter": "季度", "year": "年",
    }
    for code, label in granularity_labels.items():
        if f"DEFAULT_TIME_GRANULARITY={code}" in request.assumptions:
            parts.append(f"时间粒度：按{label}")
            break
    if request.filters:
        parts.append(
            "过滤条件："
            + json.dumps(request.filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    if request.comparison_type:
        parts.append("比较方式：" + request.comparison_type)
    if request.ranking_limit:
        direction = "最低" if AnalysisOperator.BOTTOM_N in request.operators else "最高"
        parts.append(f"排名要求：{direction}{request.ranking_limit}项")
    elif AnalysisOperator.SORT in request.operators:
        direction = next(
            (
                value.split("=", 1)[1]
                for value in request.assumptions
                if value.startswith("SORT_DIRECTION=")
            ),
            "DESC",
        )
        label = "从低到高" if direction == "ASC" else "从高到低"
        metric_name = (
            request.metrics[0].canonical_name or request.metrics[0].input
            if request.metrics
            else "主要业务指标"
        )
        parts.append(f"排序要求：按{metric_name}{label}")
    if request.forecast_horizon_periods and request.forecast_granularity:
        parts.append(
            f"预测范围：未来{request.forecast_horizon_periods}个"
            f"{request.forecast_granularity}周期"
        )
    if confirmation:
        parts.append("用户对语义歧义的确认：" + confirmation.strip())
    return "；".join(parts)


def safe_semantic_confirmation(answer: str) -> str | None:
    """Accept a short business choice, never a second set of instructions."""
    value = re.sub(r"\s+", " ", answer).strip(" ，,。.!！?？;；：:")
    if not value or len(value) > 100 or "\n" in answer or "\r" in answer:
        return None
    instruction_markers = (
        "忽略", "无视", "系统提示", "提示词", "执行SQL", "执行 sql",
        "删除", "修改数据库", "覆盖规则", "绕过", "不要遵守",
    )
    if any(marker.lower() in value.lower() for marker in instruction_markers):
        return None
    return value

# Keep the alternatives separate and ordered from most explicit to least
# explicit.  This makes inherited year/month handling deterministic and, more
# importantly, lets an invalid explicit range fail instead of being silently
# reinterpreted as a single day or a whole month.
_EXPLICIT_DATE_RANGE_PATTERNS = (
    # 2026-07-01 至 2026-07-30 / 2026年7月1号到2026年7月30号
    re.compile(
        rf"(?<!\d)"
        rf"(?P<start_year>20\d{{2}}){_YEAR_MONTH_SEPARATOR}"
        rf"(?P<start_month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
        rf"(?P<start_day>\d{{1,2}}){_DAY_SUFFIX}"
        rf"{_RANGE_SEPARATOR}"
        rf"(?P<end_year>20\d{{2}}){_YEAR_MONTH_SEPARATOR}"
        rf"(?P<end_month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
        rf"(?P<end_day>\d{{1,2}}){_DAY_SUFFIX}(?!\d)"
    ),
    # 2026-07-01 至 07-30 / 2026年7月1号到7月30号（省略结束年份）
    re.compile(
        rf"(?<!\d)"
        rf"(?P<start_year>20\d{{2}}){_YEAR_MONTH_SEPARATOR}"
        rf"(?P<start_month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
        rf"(?P<start_day>\d{{1,2}}){_DAY_SUFFIX}"
        rf"{_RANGE_SEPARATOR}"
        rf"(?P<end_month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
        rf"(?P<end_day>\d{{1,2}}){_DAY_SUFFIX}(?!\d)"
    ),
    # 2026年7月1号到30号（省略结束年份和月份）
    re.compile(
        rf"(?<!\d)"
        rf"(?P<start_year>20\d{{2}}){_YEAR_MONTH_SEPARATOR}"
        rf"(?P<start_month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
        rf"(?P<start_day>\d{{1,2}}){_DAY_SUFFIX}"
        rf"{_RANGE_SEPARATOR}"
        rf"(?P<end_day>\d{{1,2}}){_DAY_SUFFIX}(?!\d)"
    ),
)

_SINGLE_EXPLICIT_DATE_PATTERN = re.compile(
    rf"(?<!\d)"
    rf"(?P<year>20\d{{2}}){_YEAR_MONTH_SEPARATOR}"
    rf"(?P<month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
    rf"(?P<day>\d{{1,2}}){_DAY_SUFFIX}(?!\d)"
)

_EXPLICIT_YEAR_MONTH_PATTERN = re.compile(
    r"(?<!\d)(?P<year>20\d{2})(?:年(?P<month_cn>\d{1,2})月份?|[-/.](?P<month_num>\d{1,2}))"
    r"(?![\d月日号./-])"
)

_EXPLICIT_MONTH_RANGE_PATTERNS = (
    # 2026年7月至8月 / 2026年7月份到2026年8月份
    re.compile(
        rf"(?<!\d)(?P<start_year>20\d{{2}})年"
        rf"(?P<start_month>\d{{1,2}})月份?{_RANGE_SEPARATOR}"
        rf"(?:(?P<end_year>20\d{{2}})年)?(?P<end_month>\d{{1,2}})月份?(?!\d)"
    ),
    # 2026-7 至 2026-8 / 2026/07~08
    re.compile(
        rf"(?<!\d)(?P<start_year>20\d{{2}})[-/.](?P<start_month>\d{{1,2}})"
        rf"{_WORD_RANGE_SEPARATOR}(?:(?P<end_year>20\d{{2}})[-/.])?"
        rf"(?P<end_month>\d{{1,2}})(?![\d日号./-])"
    ),
)

_START_TO_NOW_PATTERN = re.compile(
    rf"(?<!\d)(?P<year>20\d{{2}}){_YEAR_MONTH_SEPARATOR}"
    rf"(?P<month>\d{{1,2}}){_MONTH_DAY_SEPARATOR}"
    rf"(?P<day>\d{{1,2}}){_DAY_SUFFIX}(?:起)?(?:到|至)(?:今|今天|今日|现在)"
)

_EXPLICIT_QUARTER_PATTERN = re.compile(
    r"(?<!\d)(?P<year>20\d{2})(?:年)?[Qq](?P<quarter>[1-4])(?!\d)"
)

_EXPLICIT_YEAR_RANGE_PATTERN = re.compile(
    rf"(?<!\d)(?P<start_year>(?:19|20)\d{{2}})年(?:度)?{_RANGE_SEPARATOR}"
    rf"(?P<end_year>(?:19|20)\d{{2}})年(?:度)?(?!\d)"
)

_EXPLICIT_YEAR_PATTERN = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})年(?:度)?(?!\d|[Qq])"
)

_NAMED_QUARTER_PATTERN = re.compile(
    r"(?:(?P<year>20\d{2})年|(?P<relative>今年|本年|去年|明年))?"
    r"第(?P<quarter>[一二三四1234])季度"
)

_HALF_YEAR_PATTERN = re.compile(
    r"(?:(?P<year>20\d{2})年|(?P<relative>今年|本年|去年|明年))?"
    r"(?P<half>[上下])半年"
)

_BARE_MONTH_PATTERN = re.compile(
    r"(?<![\d年])(?:(?P<relative>今年|本年|去年|明年))?"
    r"(?P<month>\d{1,2})月份?(?!\d)"
)


class RuleBasedIntentClassifier:
    """Safe baseline classifier. An LLM classifier can enrich this output, never bypass it."""

    _intent_rules = [
        (PrimaryIntent.OUT_OF_SCOPE, ("修改数据库", "删除数据", "写入数据库", "破解", "绕过权限", "清空测试库")),
        (
            PrimaryIntent.REPORT_GENERATION,
            (
                "报表", "报告", "月报", "周报", "季报", "日报",
                "导出excel", "导出xlsx", "导出pdf", "导出word", "导出docx",
                "生成excel", "生成xlsx", "生成pdf", "生成word", "生成docx",
                "下载excel", "下载xlsx", "下载pdf", "下载word", "下载docx",
            ),
        ),
        (PrimaryIntent.DATA_LINEAGE, ("血缘", "来自哪", "来自哪些表", "来源表", "哪个字段", "表和字段", "数据来源", "源表", "数据链路", "指标链路", "哪张表", "加工节点", "语义指标的链路")),
        (PrimaryIntent.METRIC_DEFINITION, ("口径", "定义", "怎么算", "怎么计算", "如何计算", "计算公式", "指标含义", "指标解释", "具体指什么", "是否包含")),
        (PrimaryIntent.DATA_QUALITY, ("数据质量", "数据不全", "还不全", "数据不完整", "是否完整", "有没有缺失", "缺了一部分", "重复记录", "有没有重复", "没更新", "未更新", "更新到", "刷新", "延迟", "对账", "对不上", "空值", "脏数据")),
        (PrimaryIntent.ROOT_CAUSE_ANALYSIS, ("归因", "量价", "下降原因", "增长原因", "波动原因", "为什么下降", "为什么增长", "为什么变少", "为什么变多", "是什么原因", "哪个区域造成", "原因分析", "影响因素", "因素影响", "主要原因", "主要因素", "因素导致")),
        (PrimaryIntent.FORECAST_ANALYSIS, ("预测", "预估", "预计", "推算", "推测", "估一下", "估算", "未来趋势", "趋势预测", "能不能突破")),
        (PrimaryIntent.ANOMALY_ANALYSIS, ("异常", "突增", "突降", "突然上涨", "突然下跌", "突然上升", "突然下降", "暴增", "暴跌", "离群", "不正常", "不太对")),
        (PrimaryIntent.COMPOSITION_ANALYSIS, ("占比", "占多少", "占总额", "构成", "份额", "比例", "漏斗", "结构变化", "结构变动")),
        (PrimaryIntent.COMPARISON_ANALYSIS, ("同比", "环比", "对比", "比较", "增长率", "同期比", "较上", "较工作日", "谁更高", "谁更低", "哪个更高", "谁卖得更多", "卖得更多", "差多少", "多了多少", "变化多少")),
        (PrimaryIntent.DETAIL_QUERY, ("明细", "名单", "清单", "订单列表", "记录", "列出", "逐笔", "每一笔", "每一条", "摊开", "原始数据", "联系方式")),
        (PrimaryIntent.TREND_ANALYSIS, ("趋势", "走势", "怎么走", "变化情况", "历史变化", "按月变化", "按日变化", "按周变化", "是涨是跌", "升了还是降", "上升还是下降", "上涨还是下跌", "持续上升", "持续下降", "下降月份")),
        (PrimaryIntent.CAPABILITY_HELP, ("能做什么", "支持什么", "支持哪些", "分析能力", "有哪些功能", "提问的例子", "怎么问", "怎么向你提问", "能分析哪些", "可以分析哪些", "可以查什么", "有哪些指标")),
        (PrimaryIntent.CHAT, (
            "你好", "您好", "早上好", "下午好", "晚上好", "嗨", "谢谢", "再见",
            "想吃", "想喝", "饿了", "好累", "累了", "困了", "无聊", "心情不好",
            "不开心", "很开心", "高兴", "难过", "想聊天", "陪我聊",
        )),
    ]
    _known_metrics = (
        "含税销售总额", "累计销售额", "销售总额", "订单总金额", "订单笔数",
        "经销商近一年销售额", "近三月业绩增长率", "整体业务规模",
        "业绩增长率", "合作时长", "合作次数",
        "已合作医院数", "已合作经销商数", "已合作供应商数", "已合作客户数", "已合作门店数",
        "销售情况", "销售额", "销售总数量", "订单量", "销售量", "客户数", "客单价", "退款额", "退款率",
        "科室匹配度", "科室覆盖率",
    )
    _known_dimensions = (
        "订单状态", "支付方式", "区域", "省份", "城市", "门店", "产品", "商品",
        "品牌", "品类", "渠道", "客户", "医院", "医院等级", "供应商", "经销商",
    )
    _period_independent_snapshot_metrics = {
        "整体业务规模",
        "合作时长",
        "合作次数",
    }
    _metric_window_assumption_prefix = "METRIC_WINDOW_IS_DEFINITION="
    _EXPLICIT_FOLLOWUP_METRIC = re.compile(
        r"销售额|销售总额|订单量|订单笔数|销售量|客户数|客单价|退款额|退款率|"
        r"业务规模|增长率|合作时长|合作次数|金额|销量"
    )

    @staticmethod
    def _slot_is_negated(text: str, name: str, *, dimension: bool = False) -> bool:
        compact = re.sub(r"\s+", "", text)
        prefixes = r"不看|不查|不要|别看|去掉|移除|删掉|删除|取消"
        if re.search(
            rf"(?:{prefixes})(?:原来|之前|上次|旧的)?(?:的)?{re.escape(name)}"
            rf"(?:指标|维度|分组|筛选|过滤|条件)?",
            compact,
        ):
            return True
        return dimension and bool(re.search(rf"不按{re.escape(name)}(?:维度)?", compact))

    @staticmethod
    def _clear_all_filters(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"(?:去掉|移除|删掉|删除|取消|不要)(?:所有|全部|原来|之前|上次|旧的)?"
            r"(?:的)?(?:筛选|过滤|过滤条件|筛选条件|条件)",
            compact,
        ))

    @staticmethod
    def _clear_all_metrics(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"(?:去掉|移除|删掉|删除|取消|不要)(?:所有|全部|原来|之前|上次|旧的)?"
            r"(?:的)?(?:指标|指标项)",
            compact,
        ))

    @staticmethod
    def _clear_all_dimensions(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"(?:去掉|移除|删掉|删除|取消|不要|不按)(?:所有|全部|原来|之前|上次|旧的)?"
            r"(?:的)?(?:维度|分组)",
            compact,
        ))

    @staticmethod
    def _replacement_filter_value(text: str) -> str | None:
        compact = re.sub(r"\s+", "", text)
        match = re.search(
            r"(?:，|,|；|;|而是)(?:改成|改为|换成|改看|只看|看|查)?"
            r"([^，,。.!！?？;；]{1,50})$"
            r"|(?:改成|改为|换成|改看|只看)([^，,。.!！?？;；]{1,50})$",
            compact,
        )
        if not match:
            return None
        value = next((group for group in match.groups() if group), "")
        value = re.sub(r"(?:区域|地区|条件|的数据|数据)$", "", value).strip("的了呢")
        if not value or any(
            marker in value
            for marker in ("指标", "维度", "分组", "筛选", "过滤", "时间", "执行", "查询")
        ):
            return None
        return value

    @classmethod
    def _filter_is_negated(cls, text: str, item: dict) -> bool:
        values: list[str] = []
        for value in item.values():
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                candidate = str(value).strip()
                if len(candidate) >= 2:
                    values.append(candidate)
            elif isinstance(value, list):
                values.extend(
                    str(part).strip()
                    for part in value
                    if isinstance(part, (str, int, float))
                    and not isinstance(part, bool)
                    and len(str(part).strip()) >= 2
                )
        return any(cls._slot_is_negated(text, value) for value in values)

    def classify(
        self, question: str, identity: TrustedIdentity, conversation_id: str
    ) -> CanonicalAnalysisRequest:
        # Intent keywords include file suffixes that users often type with
        # arbitrary casing (Excel/XLSX/PDF/Word). Chinese is unaffected by
        # lower-casing, while ASCII delivery markers become deterministic.
        normalized = re.sub(r"\s+", "", question).lower()
        intent = PrimaryIntent.METRIC_QUERY
        dangerous_write = re.search(
            r"(?:修改|删除|写入|更新|清空).*(?:数据库|测试库|数据|记录|订单|指标|销售额|客户)"
            r"|(?:把|将).*(?:指标|销售额|订单量|订单金额|退款率).*(?:改成|调整为)\s*(?:[-+]?\d+(?:\.\d+)?|[零一二三四五六七八九十百千万亿两]+)"
            r"|(?:drop|truncate|delete\s+from|update\s+\w+\s+set|insert\s+into|alter\s+table)",
            normalized,
            flags=re.IGNORECASE,
        )
        forecast_negated = any(
            term in normalized for term in ("不是要预测", "不要预测", "无需预测", "不需要预测", "不预测未来", "不做预测")
        )
        planned_value_query = any(term in normalized for term in ("计划值", "预算值", "目标值", "已录入", "已制定"))
        future_period = any(
            term in normalized
            for term in ("下月", "下个月", "下季度", "明年", "未来", "月底", "季末", "年末")
        )
        implicit_forecast = future_period and not planned_value_query and not forecast_negated and any(
            term in normalized
            for term in (
                "趋势", "走势", "是多少", "会不会", "会怎么", "怎么变化",
                "能否达到", "能达到", "能不能突破",
            )
        )
        matched = [candidate for candidate, terms in self._intent_rules if any(term in normalized for term in terms)]
        causal_pattern = (
            any(term in normalized for term in ("为什么", "为何", "怎么会", "什么因素导致"))
            and any(
                term in normalized
                for term in ("下降", "下跌", "上涨", "上升", "增长", "减少", "变少", "变多", "波动", "低", "高")
            )
        )
        detail_negated = bool(re.search(
            r"(?:不要|不需要|无需|不用|不看|别给|不是(?:要|查|看)?).{0,8}明细",
            normalized,
        ))
        if detail_negated:
            matched = [item for item in matched if item != PrimaryIntent.DETAIL_QUERY]
        causal_negated = any(
            term in normalized
            for term in ("不是分析下降原因", "不是分析原因", "不要分析原因", "不用分析原因", "不是要看原因")
        )
        if causal_negated:
            matched = [item for item in matched if item != PrimaryIntent.ROOT_CAUSE_ANALYSIS]
            causal_pattern = False
        if re.search(r"按(?:日|周|月|季度|年).{0,20}(?:变化|涨跌|波动)", normalized):
            matched = [PrimaryIntent.TREND_ANALYSIS] + [
                item for item in matched if item != PrimaryIntent.TREND_ANALYSIS
            ]
        # The primary intent follows the requested deliverable.  A request for
        # rows remains a detail query even when the rows happen to be abnormal.
        explicit_detail_output = not detail_negated and any(
            term in normalized
            for term in (
                "明细", "名单", "清单", "订单列表", "记录", "逐笔", "每一条",
                "原始数据", "联系方式", "属于哪些", "适用于哪些", "筛选出供应商",
                "筛选出经销商",
            )
        ) or bool(
            "医院" in normalized
            and "地址" in normalized
            and any(term in normalized for term in ("哪里", "哪儿", "在哪", "那里", "查询", "查找"))
        )
        capability_question = bool(re.search(
            r"(?:你|系统)?(?:会|能|可以|支持)(?:做|查|分析|生成)?.{0,16}(?:吗|么|哪些|什么)"
            r"|怎么(?:让你|用你|向你).{0,20}", normalized
        ))
        explicit_lineage = any(
            term in normalized for term in ("数据源是什么", "数据来源是什么", "来自哪张表", "哪个字段", "源表", "数据链路")
        )
        sudden_anomaly = (
            any(term in normalized for term in ("突然", "骤然", "一下子"))
            and any(term in normalized for term in ("变高", "变低", "升高", "降低", "正常吗", "正常不正常"))
        )
        explicit_quality = any(
            term in normalized
            for term in ("数据质量", "是否完整", "有没有缺失", "有没有重复", "重复记录", "缺了一部分", "没更新", "未更新", "更新到", "延迟", "对不上", "空值", "脏数据")
        )
        explicit_object_comparison = bool(re.search(
            r"(?:谁|哪个|哪家)(?:的)?.{0,16}(?:更高|更低|更多|更少|较高|较低)",
            normalized,
        ))
        explicit_trend_series = bool(
            "趋势" in normalized
            and re.search(r"(?:按|各|每)?(?:日|周|月|季度|年)(?:度|份)?", normalized)
            and not explicit_object_comparison
            and not self._has_explicit_comparison_pair(normalized)
            and not any(term in normalized for term in ("同比", "环比"))
        )
        explicit_file_delivery = bool(
            re.search(r"(?:导出|生成|下载).{0,24}(?:excel|xlsx|pdf|word|docx)", normalized)
        )
        if dangerous_write:
            intent = PrimaryIntent.OUT_OF_SCOPE
            matched = [PrimaryIntent.OUT_OF_SCOPE] + [i for i in matched if i != PrimaryIntent.OUT_OF_SCOPE]
        elif explicit_quality:
            intent = PrimaryIntent.DATA_QUALITY
            matched = [PrimaryIntent.DATA_QUALITY] + [i for i in matched if i != PrimaryIntent.DATA_QUALITY]
        elif implicit_forecast:
            intent = PrimaryIntent.FORECAST_ANALYSIS
            matched = [PrimaryIntent.FORECAST_ANALYSIS] + [i for i in matched if i != PrimaryIntent.FORECAST_ANALYSIS]
        elif explicit_trend_series:
            intent = PrimaryIntent.TREND_ANALYSIS
            matched = [PrimaryIntent.TREND_ANALYSIS] + [
                i for i in matched if i != PrimaryIntent.TREND_ANALYSIS
            ]
        elif explicit_object_comparison:
            intent = PrimaryIntent.COMPARISON_ANALYSIS
            matched = [PrimaryIntent.COMPARISON_ANALYSIS] + [i for i in matched if i != PrimaryIntent.COMPARISON_ANALYSIS]
        elif capability_question:
            intent = PrimaryIntent.CAPABILITY_HELP
            matched = [PrimaryIntent.CAPABILITY_HELP] + [i for i in matched if i != PrimaryIntent.CAPABILITY_HELP]
        elif explicit_lineage:
            intent = PrimaryIntent.DATA_LINEAGE
            matched = [PrimaryIntent.DATA_LINEAGE] + [i for i in matched if i != PrimaryIntent.DATA_LINEAGE]
        elif explicit_file_delivery:
            # The requested deliverable is authoritative. Query/detail/root-cause
            # semantics remain in secondary_intents and drive the report content.
            intent = PrimaryIntent.REPORT_GENERATION
            matched = [PrimaryIntent.REPORT_GENERATION] + [
                i for i in matched if i != PrimaryIntent.REPORT_GENERATION
            ]
        elif causal_pattern:
            intent = PrimaryIntent.ROOT_CAUSE_ANALYSIS
            matched = [PrimaryIntent.ROOT_CAUSE_ANALYSIS] + [i for i in matched if i != PrimaryIntent.ROOT_CAUSE_ANALYSIS]
        elif explicit_detail_output:
            intent = PrimaryIntent.DETAIL_QUERY
            matched = [PrimaryIntent.DETAIL_QUERY] + [
                i for i in matched if i != PrimaryIntent.DETAIL_QUERY
            ]
        elif sudden_anomaly:
            intent = PrimaryIntent.ANOMALY_ANALYSIS
            matched = [PrimaryIntent.ANOMALY_ANALYSIS] + [i for i in matched if i != PrimaryIntent.ANOMALY_ANALYSIS]
        elif forecast_negated and any(term in normalized for term in ("趋势", "走势", "历史")):
            intent = PrimaryIntent.TREND_ANALYSIS
            matched = [i for i in matched if i != PrimaryIntent.FORECAST_ANALYSIS]
        else:
            if matched:
                intent = matched[0]

        metrics = self._extract_metric_refs(normalized)
        # “请列出某产品的销售额/订单数” asks for governed measures, not raw
        # row detail. The verb “列出” alone must not override explicit metric
        # nouns; true detail requests are already captured by名单、清单、明细、
        # 逐笔 and similar result-shape markers above.
        if (
            intent == PrimaryIntent.DETAIL_QUERY
            and metrics
            and not explicit_detail_output
        ):
            intent = PrimaryIntent.METRIC_QUERY
            matched = [
                PrimaryIntent.METRIC_QUERY,
                *(
                    item for item in matched
                    if item not in {
                        PrimaryIntent.METRIC_QUERY,
                        PrimaryIntent.DETAIL_QUERY,
                    }
                ),
            ]
        operators = self._operators(intent, normalized)
        time_range = self._time_range(normalized)
        entity = (
            "销售订单"
            if intent == PrimaryIntent.DETAIL_QUERY and "销售明细" in normalized
            else "订单"
            if "订单" in normalized and intent == PrimaryIntent.DETAIL_QUERY
            else None
        )
        fields = (
            [f for f in ("订单号", "金额", "状态", "门店") if f in normalized]
            if intent == PrimaryIntent.DETAIL_QUERY
            else []
        )
        dimensions = [d for d in self._known_dimensions if d in normalized]
        # “科室匹配度”中的“科室”是指标语义，不代表用户要求按科室
        # 分组。只有出现明确的分组表达时才加入科室维度。
        if re.search(r"按科室|各科室|每个科室|科室维度|分科室", normalized):
            dimensions.append("科室")
        comparison_type = (
            "同比"
            if "同比" in normalized
            else "环比"
            if "环比" in normalized
            else "指定时段对比"
            if intent == PrimaryIntent.COMPARISON_ANALYSIS
            and self._has_explicit_comparison_pair(normalized)
            else None
        )
        if (
            intent == PrimaryIntent.COMPARISON_ANALYSIS
            and self._has_explicit_object_comparison(normalized)
            and not self._has_explicit_comparison_pair(normalized)
        ):
            # “比较上海和江苏”“费森尤斯与贝朗对比”已经给出了两个
            # 业务对象，不应再追问比较方式。即使句中同时含有两个年份，
            # 主比较轴仍是对象，年份作为各对象下的时间维度执行。
            comparison_type = "对象间比较"
        ranking_limit = self._ranking_limit(normalized)
        singular_extrema = bool(re.search(
            r"(?:其中|这些|哪个|哪一个|谁)?.{0,20}"
            r"(?:最高|最低|最大|最小)(?:的是)?(?:哪个|哪一个|谁)?",
            normalized,
        ))
        if ranking_limit is None and singular_extrema and metrics and dimensions:
            ranking_limit = 1
            intent = PrimaryIntent.METRIC_QUERY
            operators = [
                AnalysisOperator.AGGREGATE,
                AnalysisOperator.GROUP_BY,
                (
                    AnalysisOperator.BOTTOM_N
                    if re.search(r"最低|最小", normalized)
                    else AnalysisOperator.TOP_N
                ),
            ]

        forecast_horizon, forecast_granularity = self._forecast_target(normalized)
        forecast_history_provided = self._forecast_history_provided(normalized)

        request = CanonicalAnalysisRequest(
            conversation_id=conversation_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            original_question=question,
            rewritten_question=question,
            primary_intent=intent,
            secondary_intents=[candidate for candidate in matched if candidate != intent],
            conversation_control=self._conversation_control(normalized),
            operators=operators,
            metrics=metrics,
            entity=entity,
            fields=fields,
            dimensions=dimensions,
            time_range=time_range,
            comparison_type=comparison_type,
            ranking_limit=ranking_limit,
            forecast_horizon_periods=forecast_horizon,
            forecast_granularity=forecast_granularity,
            forecast_history_provided=forecast_history_provided,
            risk_level="HIGH" if intent == PrimaryIntent.DETAIL_QUERY else "MEDIUM",
        )
        if AnalysisOperator.SORT in request.operators:
            ascending = bool(re.search(r"升序|从低到高|由低到高", normalized))
            request.assumptions.append(
                "SORT_DIRECTION=" + ("ASC" if ascending else "DESC")
            )
        self.apply_business_query_shapes(request, question)
        self._apply_default_time_range(request)
        request.missing_slots = self.required_missing_slots(request)
        return request

    @classmethod
    def _apply_default_time_range(cls, request: CanonicalAnalysisRequest) -> None:
        """Use the latest year when an analytical request omits its period.

        Explicit periods and explicit lifetime/cumulative scopes always win. A
        precomputed snapshot metric also keeps its registered window semantics
        instead of receiving an unrelated fact-table date filter.
        """
        if (
            request.time_range is not None
            or cls._uses_all_time_scope(request)
            or cls._uses_precomputed_metric_window(request)
        ):
            return
        if request.primary_intent == PrimaryIntent.DETAIL_QUERY:
            detail_text = re.sub(
                r"\s+", "", request.original_question or request.rewritten_question or ""
            )
            transaction_markers = (
                "销售", "订单", "交易", "退款", "流水", "逐笔",
                "最近", "过去", "近一", "期间", "活跃", "合作", "覆盖",
            )
            master_list = any(
                marker in detail_text
                for marker in ("医院名单", "医院清单", "医院列表")
            )
            if master_list and not any(
                marker in detail_text for marker in transaction_markers
            ):
                request.assumptions.append("TIME_SCOPE=ALL_TIME")
                return
        if request.primary_intent not in {
            PrimaryIntent.METRIC_QUERY,
            PrimaryIntent.DETAIL_QUERY,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            PrimaryIntent.REPORT_GENERATION,
            PrimaryIntent.DATA_QUALITY,
            PrimaryIntent.FORECAST_ANALYSIS,
        }:
            return

        request.time_range = cls._time_range("最近一年")
        if request.time_range is None:  # Defensive: the canonical parser must support this.
            return
        request.assumptions.append("DEFAULT_TIME_RANGE=LATEST_ONE_YEAR")
        if request.primary_intent == PrimaryIntent.FORECAST_ANALYSIS:
            request.forecast_history_provided = True

    @classmethod
    def _extract_metric_refs(cls, text: str) -> list[MetricRef]:
        """Extract the longest explicit metric spans without nested duplicates."""
        selected: list[str] = []
        for name in sorted(cls._known_metrics, key=len, reverse=True):
            if name in text and not any(name in existing for existing in selected):
                selected.append(name)
        aliases = (
            # Preserve the governed metric's exact name when the user states
            # it.  Convenience wording remains backward compatible across
            # semantic models that register ``销售量`` directly.
            ("销量", "销售量"),
            ("销售数量", "销售量"),
            ("销售总数量", "销售总数量"),
            ("总销售数量", "销售总数量"),
            ("订单总额（含税）", "含税销售总额"),
            ("订单总额(含税)", "含税销售总额"),
            ("订单含税总额", "含税销售总额"),
            ("下单次数", "订单量"),
            ("订单次数", "订单量"),
            ("合作医院最多", "已合作医院数"),
            ("合作医院最高", "已合作医院数"),
            ("合作医院数量", "已合作医院数"),
            ("合作医院数", "已合作医院数"),
            ("合作经销商数量", "已合作经销商数"),
            ("合作经销商数", "已合作经销商数"),
            ("经销商数量", "已合作经销商数"),
            ("经销商数", "已合作经销商数"),
            ("合作供应商数量", "已合作供应商数"),
            ("合作供应商数", "已合作供应商数"),
            ("供应商数量", "已合作供应商数"),
            ("供应商数", "已合作供应商数"),
            ("销售订单数据", "订单笔数"),
        )
        for surface, canonical in aliases:
            position = text.find(surface)
            # “合作经销商数据/合作医院数据” describes a dataset or detail
            # facet, not a scalar count.  Avoid treating the overlapping
            # suffix “...数” as the count alias when it is immediately
            # followed by “据”.
            followed_by_data = (
                position >= 0
                and surface.endswith("数")
                and text[position + len(surface):position + len(surface) + 1] == "据"
            )
            if (
                position >= 0
                and not followed_by_data
                and surface not in selected
                and canonical not in selected
            ):
                selected.append(canonical)
        selected.sort(key=text.find)
        return [MetricRef(input=name) for name in selected]

    @staticmethod
    def _chinese_window_count(value: str) -> int | None:
        if value.isdigit():
            return int(value)
        digits = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9,
        }
        if value == "十":
            return 10
        if "十" in value:
            tens, ones = value.split("十", 1)
            if tens not in {"", *digits} or ones not in {"", *digits}:
                return None
            return (digits.get(tens, 1) * 10) + digits.get(ones, 0)
        return digits.get(value)

    @classmethod
    def _window_metric_definitions(cls) -> list[tuple[str, str, int, str, str]]:
        """Return registered metrics whose name embeds a fixed rolling window."""
        definitions: list[tuple[str, str, int, str, str]] = []
        pattern = re.compile(
            r"近(?P<count>\d{1,3}|[一二两三四五六七八九十]+)(?:个)?"
            r"(?P<unit>天|日|周|星期|月|年)(?P<base>.+)$"
        )
        for canonical_name in cls._known_metrics:
            match = pattern.search(canonical_name)
            if not match:
                continue
            count = cls._chinese_window_count(match.group("count"))
            if count is None:
                continue
            definitions.append((
                canonical_name,
                canonical_name[:match.start()],
                count,
                match.group("unit"),
                match.group("base"),
            ))
        return definitions

    @staticmethod
    def _same_window_unit(left: str, right: str) -> bool:
        aliases = {"天": "day", "日": "day", "周": "week", "星期": "week"}
        return aliases.get(left, left) == aliases.get(right, right)

    @classmethod
    def _apply_precomputed_metric_windows(
        cls, request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Separate a metric's fixed window from an external reporting period.

        A registered metric such as ``近三月业绩增长率`` is a snapshot field.
        Its ``三月`` must not become an additional fact-table date filter.  A
        separate period (for example ``2025年10月至12月的近三月业绩增长率``)
        remains an ordinary request time range.
        """
        compact = re.sub(r"\s+", "", text)
        assumption_prefix = cls._metric_window_assumption_prefix
        request.assumptions = [
            value for value in request.assumptions
            if not value.startswith(assumption_prefix)
        ]
        matched: list[tuple[int, int, str, str]] = []
        for canonical_name, subject_prefix, count, unit, base_name in (
            cls._window_metric_definitions()
        ):
            count_token = r"\d{1,3}|[一二两三四五六七八九十]+"
            prefix = re.escape(subject_prefix) if subject_prefix else ""
            pattern = re.compile(
                rf"{prefix}(?:最近|近|过去)?(?P<count>{count_token})(?:个)?"
                rf"(?P<unit>天|日|周|星期|月|年)(?:的)?{re.escape(base_name)}"
            )
            for item in pattern.finditer(compact):
                parsed_count = cls._chinese_window_count(item.group("count"))
                if parsed_count != count or not cls._same_window_unit(
                    item.group("unit"), unit
                ):
                    continue
                matched.append((item.start(), item.end(), canonical_name, base_name))
                break

        if not matched:
            return

        replacements = {base: canonical for _, _, canonical, base in matched}
        request.metrics = [
            MetricRef(input=replacements.get(metric.input, metric.input))
            if metric.input in replacements and metric.metric_id is None
            else metric
            for metric in request.metrics
        ]
        request.metrics = list({metric.input: metric for metric in request.metrics}.values())

        canonical_windows = {item[2] for item in matched}
        metric_names = {
            metric.canonical_name or metric.input for metric in request.metrics
        }
        period_independent = canonical_windows | cls._period_independent_snapshot_metrics
        if not metric_names or not metric_names.issubset(period_independent):
            return

        # Parse the remaining text, rather than the whole question, so the
        # rolling window embedded in the metric cannot masquerade as a report
        # period.  Invalid or unsupported external time wording must still be
        # clarified instead of being silently discarded.
        remaining = list(compact)
        for start, end, _, _ in matched:
            remaining[start:end] = " " * (end - start)
        remainder = "".join(remaining)
        external_time = cls._time_range(remainder)
        external_time_marker = re.search(
            r"(?:19|20)\d{2}年|最近|过去|本月|这个月|上月|本季度|上季度|"
            r"今年|本年|去年|本周|上周|时间范围|日期范围|期间",
            remainder,
        )
        if external_time is not None:
            request.time_range = external_time
            return
        if external_time_marker:
            request.time_range = None
            return

        request.time_range = None
        request.assumptions.append(
            assumption_prefix + "、".join(sorted(canonical_windows))
        )

    @staticmethod
    def _is_casual_chat(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).lower()
        # Keep this deterministic set deliberately narrow.  It covers common
        # social/emotional turns without swallowing business questions that
        # contain words such as “想查询” or “想分析”.
        return bool(re.search(
            r"(?:^|我)(?:有点|好|很|太)?(?:饿了|累了|困了|无聊|开心|高兴|难过|不开心)"
            r"|(?:我)?(?:还)?想(?:吃|喝|聊天|休息|睡觉)"
            r"|你(?:喜欢吃|喜欢喝|在干嘛|在做什么)"
            r"|[^，,。；;？?]{1,20}(?:和|与|配)[^，,。；;？?]{1,20}"
            r"(?:可以|能不能|能否|能)?一起(?:吃|喝)(?:吗|么|嘛)?"
            r"|(?:给我)?讲个笑话"
            r"|(?:陪我聊|聊聊天|辛苦了|谢谢你|晚安|早安)[呀啊吗呢。！!?？]*$",
            compact,
        ))

    @classmethod
    def apply_business_query_shapes(
        cls, request: CanonicalAnalysisRequest, question: str
    ) -> None:
        """Apply deterministic shapes for common list, relation and ranking requests.

        These phrases describe the result form, not a numeric metric.  Keeping
        them deterministic prevents a model from turning a supplier list or a
        product-category lookup into an unrelated metric clarification.
        """
        compact = re.sub(r"\s+", "", question).lower()
        if cls._is_casual_chat(compact):
            request.primary_intent = PrimaryIntent.CHAT
            request.secondary_intents = []
            request.metrics = []
            request.entity = None
            request.fields = []
            request.dimensions = []
            request.filters = []
            request.time_range = None
            request.operators = []
            request.missing_slots = []
            request.risk_level = "LOW"
            request.asl_template = None
            return
        cls._apply_metric_subject_scope(request, compact)
        transaction_activity = cls._uses_transaction_activity_definition(compact)
        cooperation_activity = cls._uses_cooperation_activity_definition(compact)
        if (
            (transaction_activity or cooperation_activity)
            and "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
            not in request.assumptions
        ):
            request.assumptions.append(
                "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
            )
        if (
            transaction_activity
            and request.time_range is None
            and "正在销售" in compact
        ):
            # “正在销售” needs a bounded transaction window.  In an analytical
            # list request, default to the latest year relative to the request
            # date and disclose it, rather than blocking an otherwise complete
            # query.  Explicit user periods always take precedence.
            request.time_range = cls._time_range("最近一年")
            request.assumptions.append(
                "ACTIVE_TIME_DEFAULT=LATEST_ONE_YEAR_FROM_REQUEST_DATE"
            )
        cls._apply_report_business_scope(request, compact)
        cls._apply_department_partner_scope(request, compact)
        cls._apply_common_region_filter(request, compact)
        cls._apply_hospital_level_scope(request, compact)
        cls._apply_explicit_grouping_scope(request, compact)
        cls._apply_multidimensional_product_scope(request, compact)
        cls._apply_brand_comparison_scope(request, compact)
        cls._apply_transaction_partner_scope(request, compact)
        cls._apply_relationship_product_scope(request, compact)
        cls._apply_interrogative_relationship_scope(
            request, re.sub(r"\s+", "", question)
        )
        cls._apply_brand_product_scope(request, compact)
        cls._apply_explicit_dealer_metric_scope(request, question)
        cls._apply_time_grouped_metric_scope(request, compact)
        if (
            cls._uses_all_time_scope(request)
            and "TIME_SCOPE=ALL_TIME" not in request.assumptions
        ):
            request.assumptions.append("TIME_SCOPE=ALL_TIME")
        recommendation = cls._is_ranked_partner_recommendation(compact)
        department_to_dealer = bool(re.search(
            r"(?:根据|按照|按|基于)?(?:上述|对应|其)?适用科室.{0,20}"
            r"(?:筛选|查找|匹配|推荐)(?:出)?(?:合适的)?经销商",
            compact,
        ))
        product_department_lookup = (
            "科室" in compact
            and any(
                marker in compact
                for marker in (
                    "适用于哪些", "适用哪些科室", "适用的科室", "适用科室", "对应科室",
                )
            )
            and not department_to_dealer
        )
        hospital_address_lookup = (
            "医院" in compact
            and "地址" in compact
            and any(marker in compact for marker in ("哪里", "哪儿", "在哪", "那里", "查询", "查找"))
        )
        product_to_hospital_lookup = bool(re.search(
            r"(?:它|该产品|这个产品|这些产品)?.{0,12}(?:卖给|销售给)(?:了)?哪些医院"
            r"|哪些医院.{0,12}(?:购买|采购|使用)(?:了)?(?:它|该产品|这个产品)",
            compact,
        ))
        hospital_level_lookup = bool(re.search(
            r"(?:这些|上述|前述)?医院.{0,16}(?:什么|哪些|分别是)?(?:等级|级别)",
            compact,
        )) and not (
            re.search(r"(?:统计|汇总|计算|分析)(?:每家|各|按)?.{0,24}医院", compact)
            or any(metric in compact for metric in cls._known_metrics)
        )
        partner_product_lookup = re.search(
            r"(?P<partner>[^，,。；;？?]{2,100}?(?:有限责任公司|股份有限公司|有限公司|公司))"
            r"(?:都|主要)?(?:销售过?|卖过?|经营)(?:(?:的)?(?:什么|哪些)|的)(?:产品|商品)",
            compact,
        )
        brand_value_lookup = bool(re.search(
            r"(?:母品牌|品牌)(?:值|名称)?(?:都)?(?:有|包括|包含)(?:哪些|什么)"
            r"|(?:查询|列出|展示)(?:所有|全部)?(?:母品牌|品牌)(?:值|名称)?",
            compact,
        ))
        relation_detail = bool(re.search(
            r"(?:哪些|提供|列出|找出|清单|名单|联系方式|属于哪些|适用于哪些|"
            r"筛选出)(?:.{0,40})(?:供应商|经销商|商品分类|品类|产品|商品|医院|科室|门店|地址)",
            compact,
        )) or bool(re.search(
            r"(?:供应商|经销商|商品).{0,20}(?:清单|名单|联系方式|地址)", compact
        )) or bool(re.search(
            r"(?:匹配|查询|查找|确认|识别).{0,30}(?:所属)?(?:商品分类|品类|类目)",
            compact,
        )) or product_department_lookup or department_to_dealer or hospital_address_lookup \
            or partner_product_lookup is not None or product_to_hospital_lookup or hospital_level_lookup \
            or brand_value_lookup or bool(re.search(
                r"(?:展示|显示|查询|查看|列出)?(?:所有|全部)"
                r"(?:合作)?(?:经销商|供应商|医院|客户|门店)",
                compact,
            ))
        metric_only_projection = bool(
            request.metrics
            and (
                re.search(
                    r"最高|最低|最大|最小|前\d+|后\d+|排名|排行|排序|"
                    r"趋势|走势|同比|环比|占比|平均|汇总|合计",
                    compact,
                )
                or not any(
                    marker in compact
                    for marker in (
                        "名单", "清单", "哪些", "明细", "逐笔", "每一条",
                        "联系方式", "属于哪些", "适用于哪些", "适用的科室",
                    )
                )
            )
        )

        protected_intents = {
            PrimaryIntent.REPORT_GENERATION,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            PrimaryIntent.FORECAST_ANALYSIS,
            PrimaryIntent.DATA_LINEAGE,
            PrimaryIntent.DATA_QUALITY,
        }
        if recommendation and request.primary_intent not in (
            protected_intents - {PrimaryIntent.COMPARISON_ANALYSIS}
        ):
            request.primary_intent = PrimaryIntent.COMPARISON_ANALYSIS
            request.comparison_type = "对象间比较"
            request.risk_level = "MEDIUM"
            for operator in (
                AnalysisOperator.FILTER,
                AnalysisOperator.GROUP_BY,
                AnalysisOperator.COMPARE,
                AnalysisOperator.SORT,
                AnalysisOperator.TOP_N,
                AnalysisOperator.RENDER_TABLE,
            ):
                if operator not in request.operators:
                    request.operators.append(operator)
        elif (
            relation_detail
            and not metric_only_projection
            and request.primary_intent not in protected_intents
        ):
            request.primary_intent = PrimaryIntent.DETAIL_QUERY
            request.risk_level = "HIGH"
            request.operators = [
                operator for operator in request.operators
                if operator != AnalysisOperator.AGGREGATE
            ]
            if AnalysisOperator.FILTER not in request.operators:
                request.operators.append(AnalysisOperator.FILTER)
            if AnalysisOperator.RENDER_TABLE not in request.operators:
                request.operators.append(AnalysisOperator.RENDER_TABLE)
            if brand_value_lookup:
                request.entity = "母品牌"
                request.fields = ["母品牌"]
                request.dimensions = ["母品牌"]
                request.assumptions = [
                    value for value in request.assumptions
                    if not value.startswith("DEFAULT_TIME_RANGE=")
                ]
                request.assumptions.append("TIME_SCOPE=ALL_TIME")
                request.time_range = None

        explicit_metrics = cls._extract_metric_refs(compact)
        if explicit_metrics:
            request.metrics = explicit_metrics
        hospital_level_count = bool(re.search(
            r"(?:各|按|每个)(?:医院)?(?:等级|级别)"
            r"(?:对应的|下|中)?(?:医院)?(?:数量|个数|家数)"
            r"|(?:医院)?(?:等级|级别)(?:对应的|下|中|分别)?"
            r"医院(?:数量|个数|家数)",
            compact,
        ))
        if hospital_level_count:
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.metrics = [MetricRef(input="医院数量")]
            request.entity = None
            request.fields = []
            request.dimensions = ["医院等级"]
            request.risk_level = "MEDIUM"
            request.operators = [
                AnalysisOperator.GROUP_BY,
                AnalysisOperator.AGGREGATE,
                AnalysisOperator.RENDER_TABLE,
            ]
            if "NULL_DIMENSION_BUCKET=医院等级:未填写" not in request.assumptions:
                request.assumptions.append(
                    "NULL_DIMENSION_BUCKET=医院等级:未填写"
                )
            if "STRICT_GROUPING_DIMENSIONS" not in request.assumptions:
                request.assumptions.append("STRICT_GROUPING_DIMENSIONS")
        if (
            request.primary_intent == PrimaryIntent.TREND_ANALYSIS
            and re.search(r"(?:全部|所有)(?:产品|商品)", compact)
        ):
            request.dimensions = [
                value for value in request.dimensions
                if value not in {"产品", "商品"}
            ]
            if "ALL_PRODUCTS_SCOPE" not in request.assumptions:
                request.assumptions.append("ALL_PRODUCTS_SCOPE")
        if (
            any(metric.input == "订单笔数" for metric in request.metrics)
            and re.search(r"(?:各|按|分)(?:个)?省份", compact)
            and not any(
                owner in compact
                for owner in ("医院省份", "经销商省份", "供应商省份", "客户省份")
            )
        ):
            request.dimensions = [
                "业务省份" if value == "省份" else value
                for value in request.dimensions
            ]
            if "GEOGRAPHIC_ROLE=SALES_ORDER_BUSINESS_PROVINCE" not in request.assumptions:
                request.assumptions.append(
                    "GEOGRAPHIC_ROLE=SALES_ORDER_BUSINESS_PROVINCE"
                )
        relationship_count_dimensions = {
            "已合作医院数": "医院",
            "已合作经销商数": "经销商",
            "已合作供应商数": "供应商",
            "已合作客户数": "客户",
            "已合作门店数": "门店",
        }
        for metric_name, dimension_name in relationship_count_dimensions.items():
            if not any(metric.input == metric_name for metric in request.metrics):
                continue
            if re.search(
                rf"按{dimension_name}|各{dimension_name}|每(?:家|个){dimension_name}|"
                rf"{dimension_name}维度|分{dimension_name}",
                compact,
            ):
                continue
            # Relationship counts are scalar distinct-count metrics.  A model
            # may echo the relationship noun as a grouping dimension, which
            # changes one total into one row per object.
            request.dimensions = [
                dimension for dimension in request.dimensions
                if dimension != dimension_name
            ]

        partner_result_shape = any(
            shape in compact for shape in ("名单", "清单", "列出", "找出")
        ) or bool(re.search(
            r"(?:的)?(?:经销商|供应商).{0,80}?(?:按|根据)"
            r".{0,30}?(?:排序|排名)",
            compact,
        ))
        partner_metric_list = bool(
            request.primary_intent in {
                PrimaryIntent.DETAIL_QUERY, PrimaryIntent.METRIC_QUERY,
            }
            and request.metrics
            and any(partner in compact for partner in ("经销商", "供应商"))
            and partner_result_shape
        )
        if partner_metric_list:
            # A partner name accompanied by SUM/count/snapshot measures is a
            # grouped aggregate result, not row-level detail.  Keeping it as a
            # DETAIL_QUERY would cause the execution adapter to (correctly for
            # real detail) remove all generated metrics.  Make the executable
            # shape explicit instead: one row per requested partner plus the
            # requested measures.
            partner = "经销商" if "经销商" in compact else "供应商"
            request.primary_intent = PrimaryIntent.METRIC_QUERY
            request.entity = partner
            request.fields = []
            request.risk_level = "MEDIUM"
            request.dimensions = [
                dimension for dimension in request.dimensions
                if dimension == partner
            ]
            if partner not in request.dimensions:
                request.dimensions.append(partner)
            for operator in (
                AnalysisOperator.FILTER,
                AnalysisOperator.GROUP_BY,
                AnalysisOperator.AGGREGATE,
                AnalysisOperator.RENDER_TABLE,
            ):
                if operator not in request.operators:
                    request.operators.append(operator)
            if re.search(r"(?:按|根据).{0,80}(?:排序|排名)", compact):
                cls._apply_ranked_partner_scope(request, compact)

        ranked_partner_list = bool(
            request.primary_intent not in (
                protected_intents - {PrimaryIntent.COMPARISON_ANALYSIS}
            )
            and request.metrics
            and any(partner in compact for partner in ("经销商", "供应商"))
            and any(shape in compact for shape in ("名单", "清单", "列出", "找出"))
            and re.search(r"(?:按|根据).{0,30}(?:排序|排名)", compact)
        )
        if ranked_partner_list:
            partner = "经销商" if "经销商" in compact else "供应商"
            request.primary_intent = PrimaryIntent.COMPARISON_ANALYSIS
            request.comparison_type = "对象间比较"
            request.risk_level = "MEDIUM"
            request.dimensions = [
                dimension for dimension in request.dimensions
                if dimension == partner
                or re.search(
                    rf"(?:按|各|每个|分){re.escape(dimension)}(?:维度)?",
                    compact,
                )
            ]
            if partner not in request.dimensions:
                request.dimensions.append(partner)
            for operator in (
                AnalysisOperator.FILTER,
                AnalysisOperator.GROUP_BY,
                AnalysisOperator.COMPARE,
                AnalysisOperator.SORT,
                AnalysisOperator.RENDER_TABLE,
            ):
                if operator not in request.operators:
                    request.operators.append(operator)
            cls._apply_ranked_partner_scope(request, compact)

        # A phrase such as ``<brand>品牌<category>的经销商清单`` contains
        # catalog filters, not one long product name.  Apply this after the
        # generic report/ranking scope extractors so it can replace their
        # necessarily broader product guess with the canonical slots.
        cls._apply_catalog_partner_scope(request, question)

        if (
            request.primary_intent == PrimaryIntent.TREND_ANALYSIS
            and not request.metrics
            and re.search(r"(?:整体)?销售(?:趋势|走势|变化)", compact)
        ):
            # “销售趋势” has a stable business meaning in the registered
            # semantic layer: observe sales amount over time.  Record the
            # deterministic expansion so it remains visible to audit instead
            # of asking the user to repeat the noun as a metric.
            request.metrics = [MetricRef(input="销售额")]
            request.assumptions.append("SALES_TREND_METRIC=销售额")

        if (
            request.primary_intent == PrimaryIntent.TREND_ANALYSIS
            and [metric.input for metric in request.metrics] == ["销售额"]
            and "SALES_TREND_METRIC=销售额" in request.assumptions
            and request.rewritten_question
        ):
            # Keep the model-completed executable question consistent with the
            # canonical implicit metric.  The raw user question remains intact
            # in ``original_question`` for audit.
            request.rewritten_question = re.sub(
                r"销售(?=趋势|走势|变化)",
                "销售额",
                request.rewritten_question,
            )

        if (
            re.search(r"销售额(?:下降|减少|下跌)(?:幅度)?(?:最大|最多)", compact)
            and any(token in compact for token in ("产品", "商品"))
        ):
            request.primary_intent = PrimaryIntent.COMPARISON_ANALYSIS
            request.comparison_type = "指定时段对比"
            if "产品" not in request.dimensions and "商品" not in request.dimensions:
                request.dimensions.append("产品")
            if "DEFAULT_TIME_GRANULARITY=month" not in request.assumptions:
                request.assumptions.append("DEFAULT_TIME_GRANULARITY=month")

        if request.primary_intent == PrimaryIntent.DETAIL_QUERY:
            coverage_shape = cls._coverage_detail_shape(compact)
            if "销售明细" in compact:
                # Keep the implicit preview projection limited to columns
                # guaranteed by the current order semantic model.  Optional
                # relationship/date columns may be requested explicitly, but
                # forcing all of them here makes the ASL service reject an
                # otherwise valid detail query as ASL_DETAIL_FIELDS_INCOMPLETE.
                request.entity = "订单"
                request.fields = ["订单号", "金额"]
            if partner_product_lookup is not None:
                if "SET_RELATIONSHIP_PROJECTION" not in request.assumptions:
                    request.assumptions.append("SET_RELATIONSHIP_PROJECTION")
                partner_name = partner_product_lookup.group("partner").strip()
                partner_name = re.sub(
                    r"^(?:请|麻烦|帮我|给我|查询|查找|查看)?"
                    r"(?:最近一年|近一年|过去一年|本年度|今年)?",
                    "",
                    partner_name,
                ).strip("的，,；;")
                request.entity = "商品"
                request.metrics = []
                request.fields = ["商品名称"]
                request.filters = [
                    item for item in request.filters
                    if str(item.get("field") or "") != "经销商名称"
                ]
                request.filters.append({
                    "field": "经销商名称",
                    "operator": "EQ",
                    "value": partner_name,
                })
                if (
                    "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
                    not in request.assumptions
                ):
                    request.assumptions.append(
                        "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
                    )
            elif product_to_hospital_lookup:
                request.entity = "医院"
                request.metrics = []
                request.fields = ["医院名称"]
            elif hospital_level_lookup:
                request.entity = "医院"
                request.metrics = []
                request.fields = ["医院名称", "医院等级"]
            elif coverage_shape is not None:
                entity, primary_field = coverage_shape
                request.entity = entity
                generic_fields = {
                    "覆盖", "覆盖数据", "覆盖明细", "分布", "分布明细",
                    f"{entity}覆盖", f"{entity}数据", entity,
                }
                existing = [
                    field for field in request.fields
                    if field not in generic_fields
                    and "覆盖" not in field
                    and "名单" not in field
                    and "清单" not in field
                ]
                inferred = [primary_field]
                if "医院等级" in compact or "医院级别" in compact:
                    inferred.append("医院等级")
                if "所在省市" in compact:
                    inferred.extend(("所在省份", "所在城市"))
                request.fields = list(dict.fromkeys([*existing, *inferred]))
            elif department_to_dealer:
                request.entity = "经销商"
                generic_fields = {"供应商", "经销商", "联系方式", "科室"}
                existing = [field for field in request.fields if field not in generic_fields]
                request.fields = list(dict.fromkeys([
                    *existing, "经销商名称", "适用科室",
                ]))
            elif product_department_lookup:
                request.entity = "产品"
                generic_fields = {"产品", "商品", "科室"}
                existing = [field for field in request.fields if field not in generic_fields]
                request.fields = list(dict.fromkeys([
                    *existing, "商品名称", "适用科室",
                ]))
            elif hospital_address_lookup:
                request.entity = "医院"
                generic_fields = {"医院", "地址"}
                existing = [field for field in request.fields if field not in generic_fields]
                request.fields = list(dict.fromkeys([
                    *existing, "医院名称", "地址",
                ]))
            elif any(term in compact for term in ("供应商", "经销商")):
                request.entity = request.entity or (
                    "供应商" if "供应商" in compact else "经销商"
                )
                is_supplier = "供应商" in compact
                inferred = ["供应商名称" if is_supplier else "经销商名称"]
                if "联系方式" in compact:
                    inferred.extend(
                        ("联系人姓名", "联系人手机号")
                        if is_supplier
                        else ("邮箱", "地址")
                    )
                if "所在省市" in compact:
                    inferred.extend(("所在省份", "所在城市"))
                if "合作状态" in compact or (
                    "活跃" in compact and not transaction_activity
                ):
                    inferred.append("合作状态")
                # A structured model may emit result-shape phrases such as
                # "供应商" and "联系方式" as if they were physical fields.
                # Replace them with the concrete projections required by the
                # query instead of making downstream completeness validation
                # look for non-existent duplicate columns.
                generic_fields = {
                    "供应商", "经销商", "联系方式", "名单", "清单",
                    "供应商名单", "供应商清单", "经销商名单", "经销商清单",
                }
                existing = [field for field in request.fields if field not in generic_fields]
                request.fields = list(dict.fromkeys([*existing, *inferred]))
            elif any(term in compact for term in ("商品分类", "品类", "类目", "属于哪些")):
                request.entity = request.entity or "商品"
                request.fields = list(dict.fromkeys([*request.fields, "商品名称", "商品分类"]))
            elif "地址" in compact and "门店" in compact:
                request.entity = request.entity or "门店"
                request.fields = list(dict.fromkeys([*request.fields, "门店名称", "地址"]))

        if request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS:
            partner_dimension = (
                "经销商" if "经销商" in compact
                else "供应商" if "供应商" in compact
                else None
            )
            if recommendation and partner_dimension:
                # Brand/category/channel words in a recommendation normally
                # describe filters. Keep them as grouping dimensions only
                # when the user explicitly asks to group by them.
                explicit_dimensions = [
                    dimension
                    for dimension in request.dimensions
                    if dimension == partner_dimension
                    or re.search(
                        rf"(?:按|各|每个|分){re.escape(dimension)}(?:维度)?",
                        compact,
                    )
                ]
                request.dimensions = list(dict.fromkeys(explicit_dimensions))
            if partner_dimension and partner_dimension not in request.dimensions:
                request.dimensions.append(partner_dimension)
            if recommendation and partner_dimension == "经销商" and not request.metrics:
                # One ranking metric is sufficient to answer a recommendation
                # and keeps the aggregate shape valid.  Additional profile
                # metrics are available on explicit request; forcing four
                # unrelated measures here made a simple recommendation fail
                # semantic metric selection and grouping validation.
                request.metrics = [MetricRef(input="经销商近一年销售额")]
                request.ranking_limit = request.ranking_limit or 10
                request.assumptions.append(
                    "DEALER_RECOMMENDATION_DEFAULT_RANKING=经销商近一年销售额"
                )

        if (
            request.primary_intent in {
                PrimaryIntent.METRIC_QUERY,
                PrimaryIntent.TREND_ANALYSIS,
            }
            and re.search(r"分析(?:下|一下)?.{2,80}(?:产品|商品)?的?销售情况", compact)
        ):
            # “销售情况” is a conventional overview request.  Use the
            # registered amount measure over the default time window and show
            # it monthly; record both defaults instead of asking the user
            # to restate a standard analytical shape.
            request.primary_intent = PrimaryIntent.TREND_ANALYSIS
            request.metrics = [MetricRef(input="含税销售总额")]
            request.dimensions = [
                dimension for dimension in request.dimensions
                if dimension not in {"产品", "商品"}
            ]
            request.assumptions.extend(
                assumption for assumption in (
                    "DEFAULT_TIME_GRANULARITY=month",
                    "SALES_OVERVIEW_METRIC=含税销售总额",
                )
                if assumption not in request.assumptions
            )
        cls._apply_precomputed_metric_windows(request, compact)

        if (
            request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
            and request.comparison_type is None
            and re.search(r"(?:对比|比较).{0,20}(?:家|个|名|供应商|经销商|门店)", compact)
        ):
            request.comparison_type = "对象间比较"
        cls._apply_name_projection_non_null_constraint(request)
        cls._drop_invalid_filter_values(request)

    @staticmethod
    def _apply_name_projection_non_null_constraint(
        request: CanonicalAnalysisRequest,
    ) -> None:
        """Keep master-data list projections free of missing object names.

        A relationship row can legitimately exist while its display-name join
        is missing or stale.  For a user-facing list such as ``合作医院名单``
        that row is not a valid list member: rendering it as a dash changes a
        data-quality defect into an apparent business object.  Bind the
        non-null requirement to the requested semantic role so the current
        semantic layer, rather than a physical table name, resolves it.
        """

        if request.primary_intent != PrimaryIntent.DETAIL_QUERY:
            return
        entity_name_fields = {
            "医院": "医院名称",
            "经销商": "经销商名称",
            "供应商": "供应商名称",
            "厂家": "厂家名称",
            "制造商": "制造商名称",
            "商品": "商品名称",
            "产品": "商品名称",
            "客户": "客户名称",
            "门店": "门店名称",
            "科室": "科室名称",
            "品牌": "品牌名称",
            "母品牌": "母品牌",
        }
        name_field = entity_name_fields.get(str(request.entity or "").strip())
        if name_field is None:
            projected_names = [
                field
                for field in request.fields
                if field in set(entity_name_fields.values())
            ]
            if len(projected_names) != 1:
                return
            name_field = projected_names[0]
        elif name_field not in request.fields:
            return

        assumption = f"REQUIRED_NAME_NON_NULL={name_field}"
        if assumption not in request.assumptions:
            request.assumptions.append(assumption)

    @staticmethod
    def _drop_invalid_filter_values(request: CanonicalAnalysisRequest) -> None:
        """Reject punctuation-only slot values before they enter memory/ASL."""

        def valid(value: object) -> bool:
            if value is None:
                return False
            text = str(value).strip()
            return bool(text and re.search(r"[\w\u4e00-\u9fff]", text))

        cleaned: list[dict[str, object]] = []
        removed = False
        for item in request.filters:
            if not isinstance(item, dict):
                removed = True
                continue
            if str(item.get("operator") or "").upper() in {
                "IS_NULL", "IS_NOT_NULL",
            }:
                cleaned.append(item)
                continue
            value = item.get("value")
            if isinstance(value, list):
                values = [candidate for candidate in value if valid(candidate)]
                if not values:
                    removed = True
                    continue
                cleaned.append({**item, "value": values})
            elif valid(value):
                cleaned.append(item)
            else:
                removed = True
        request.filters = cleaned
        if removed and "INVALID_FILTER_VALUE_DROPPED" not in request.assumptions:
            request.assumptions.append("INVALID_FILTER_VALUE_DROPPED")

    @staticmethod
    def _apply_metric_subject_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Turn ``<specific product>产品的<metric>`` into an exact filter."""
        if request.primary_intent not in {
            PrimaryIntent.METRIC_QUERY,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            PrimaryIntent.REPORT_GENERATION,
        }:
            return
        match = re.search(
            r"(?:查询|统计|分析|查看|看看|按(?:日|周|月|季度|年)(?:统计|汇总|分析)?)"
            r"(?P<subject>[^，,。；;？?]{2,100}?)(?:产品|商品)的?"
            r"(?:含税销售总额|销售总额|销售额|订单量|订单笔数|销售量)",
            text,
        )
        if match is None:
            # Concrete medical-device names are commonly used without the
            # redundant “产品/商品” suffix.  Let the semantic layer validate the
            # provisional catalog value, but keep the complete current-turn
            # entity span so SQL cannot execute without a product filter.
            match = re.search(
                r"(?:查询|统计|分析|查看|看看|按(?:日|周|月|季度|年)(?:统计|汇总|分析)?)"
                r"(?P<subject>[^，,。；;？?]{2,100}?(?:导管|透析器|口罩|套件|球囊|"
                r"支架|导丝|耗材|器械|设备))的?"
                r"(?:含税销售总额|销售总额|销售额|订单量|订单笔数|销售量)",
                text,
            )
        if match is None:
            return
        subject = match.group("subject").strip("的")
        subject = re.sub(r"^(?:最近|过去).{1,8}(?:年|月|周|天)", "", subject)
        subject = re.sub(
            r"^(?:(?:北京|上海|天津|重庆)(?:市|地区)?|"
            r"(?:香港|澳门)特别行政区|"
            r"[\u4e00-\u9fff]{2,12}(?:省|自治区|市|地区))",
            "",
            subject,
        )
        if (
            not 2 <= len(subject) <= 100
            or subject in {"全部", "所有", "各类", "每个", "这个", "该"}
            or any(marker in subject for marker in ("趋势", "报表", "报告", "维度"))
        ):
            return
        request.entity = request.entity or "产品"
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") != "商品名称"
        ]
        request.filters = [
            {
                **item,
                "field": "业务城市",
            }
            if str(item.get("field") or "") in {"地区", "区域", "城市"}
            else item
            for item in request.filters
        ]
        request.filters.append({
            "field": "商品名称", "operator": "EQ", "value": subject,
        })

    @staticmethod
    def _apply_common_region_filter(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Ground common province/municipality shorthand as one replaceable filter."""
        regions = {
            "北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市",
            "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省", "福建": "福建省",
            "江西": "江西省", "山东": "山东省", "河南": "河南省", "湖北": "湖北省",
            "湖南": "湖南省", "广东": "广东省", "海南": "海南省", "四川": "四川省",
            "贵州": "贵州省", "云南": "云南省", "陕西": "陕西省", "甘肃": "甘肃省",
            "青海": "青海省", "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省",
            "吉林": "吉林省", "黑龙江": "黑龙江省",
        }
        matches = [
            (text.rfind(surface), canonical)
            for surface, canonical in regions.items()
            if surface in text
            and not re.search(
                rf"{re.escape(surface)}[^，,。；;]{{1,50}}(?:有限责任公司|股份有限公司|有限公司)",
                text,
            )
        ]
        if not matches:
            return
        ordered_regions = list(dict.fromkeys(
            region for _, region in sorted(matches, key=lambda item: item[0])
        ))
        region_role = (
            "业务城市"
            if request.entity == "产品" and bool(request.metrics)
            else "地区"
        )
        if (
            request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
            and len(ordered_regions) >= 2
        ):
            replacement = {
                "field": region_role, "operator": "IN", "value": ordered_regions,
            }
            if region_role not in request.dimensions:
                request.dimensions.append(region_role)
        else:
            region = ordered_regions[-1]
            replacement = {"field": region_role, "operator": "EQ", "value": region}
        region_fields = {
            "地区", "省份", "城市", "业务省份", "业务城市",
            "医院省份", "医院城市", "经销商省份", "经销商城市",
        }
        updated: list[dict] = []
        replaced = False
        for item in request.filters:
            if str(item.get("field") or "") not in region_fields:
                updated.append(item)
            elif not replaced:
                updated.append(replacement)
                replaced = True
        if not replaced:
            updated.append(replacement)
        request.filters = updated
        if region_role == "业务城市" and (
            "GEOGRAPHIC_ROLE=SALES_BUSINESS_CITY" not in request.assumptions
        ):
            request.assumptions.append("GEOGRAPHIC_ROLE=SALES_BUSINESS_CITY")

    @staticmethod
    def _apply_hospital_level_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        match = re.search(r"(?P<level>一|二|三|四|未定)级医院", text)
        if match is None:
            return
        value = f"{match.group('level')}级医院"
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {"医院等级", "医院级别"}
        ]
        request.filters.append({
            "field": "医院等级", "operator": "EQ", "value": value,
        })

    @staticmethod
    def _apply_explicit_grouping_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Keep a nested grouping label from expanding to its parent object.

        In ``按医院等级汇总`` the requested grain is the level, not every
        hospital inside the level.  Substring extraction initially sees both
        nouns; this correction applies to explicit grouping syntax and leaves
        questions that request both hospital and level untouched.
        """

        if re.search(r"(?:按|各|每个|分)(?:医院)?(?:等级|级别)(?:汇总|统计|分析|展示|显示)?", text):
            request.dimensions = [
                value for value in request.dimensions if value != "医院"
            ]
            if "医院等级" not in request.dimensions:
                request.dimensions.append("医院等级")

    @staticmethod
    def _apply_multidimensional_product_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Separate ``<product>在各维度`` into a product filter and dimensions."""
        if request.primary_intent not in {
            PrimaryIntent.METRIC_QUERY,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
        }:
            return
        match = re.search(
            r"^(?:请|帮我|查询|统计|查看|分析|看看)*"
            r"(?P<product>[^，,。；;]{2,80}?)"
            r"在(?:各|按)(?:省市|省份|城市|地区|医院|医院等级|月份|月)",
            text,
        )
        if match is None:
            return
        product = match.group("product").strip("的")
        if not product or any(
            token in product for token in ("销售额", "销售量", "销量", "订单", "客户")
        ):
            return
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") != "商品名称"
        ]
        request.filters = [
            {**item, "field": "业务城市"}
            if str(item.get("field") or "") in {"地区", "区域", "城市"}
            else item
            for item in request.filters
        ]
        request.filters.append({
            "field": "商品名称", "operator": "EQ", "value": product,
        })
        if "省市" in text:
            for dimension in ("省份", "城市"):
                if dimension not in request.dimensions:
                    request.dimensions.append(dimension)
        if "医院等级" in text and "医院等级" not in request.dimensions:
            request.dimensions.append("医院等级")
        if re.search(r"各(?:月|月份)|按月", text):
            if "DEFAULT_TIME_GRANULARITY=month" not in request.assumptions:
                request.assumptions.append("DEFAULT_TIME_GRANULARITY=month")

    @staticmethod
    def _apply_brand_comparison_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Ground two business-facing brand names without exposing maker codes."""
        if request.primary_intent != PrimaryIntent.COMPARISON_ANALYSIS:
            return
        match = re.search(
            r"(?:比较|对比)(?P<left>[^，,。；;与和]{1,30})"
            r"(?:与|和)(?P<right>[^，,。；;]{1,30}?)(?:品牌)?产品",
            text,
        )
        if match is None:
            return
        left = match.group("left").strip("的")
        right = match.group("right").strip("的")
        if not left or not right:
            return
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {
                "品牌", "品牌名称", "厂家", "厂家名称", "厂家编码",
            }
        ]
        request.filters.append({
            "field": "母品牌", "operator": "IN", "value": [left, right],
        })
        if "品牌" not in request.dimensions:
            request.dimensions.append("品牌")
        request.comparison_type = "对象间比较"

    @staticmethod
    def _apply_interrogative_relationship_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Normalize non-numeric relationship questions into detail queries.

        Business users ask for the same relationship list in several word
        orders: ``A产品的经销商有哪些``, ``A产品有哪些经销商``, ``A产品由哪些
        经销商销售`` and ``哪些经销商销售A产品``.  The generic classifier used
        to recognize only the last form (or an explicit ``名单/清单`` suffix),
        so the other forms fell through to a metric query and incorrectly
        requested a metric.  This method only determines the result *shape*;
        the provisional catalog filter is still grounded against the current
        semantic model by the normal semantic entity resolver.

        Numeric counts, rankings, trends and explicitly requested measures are
        deliberately excluded.  They remain aggregate analytical requests.
        """

        compact = re.sub(r"\s+", "", text)
        if (
            request.primary_intent == PrimaryIntent.DETAIL_QUERY
            and "SET_RELATIONSHIP_PROJECTION" in request.assumptions
            and request.entity in {"经销商", "供应商", "医院", "客户", "门店", "厂家"}
            and request.fields == [f"{request.entity}名称"]
            and any(
                str(item.get("field") or "") in {"商品名称", "产品名称"}
                for item in request.filters
            )
        ):
            # A more specific relationship extractor (for example the
            # transaction-activity parser) has already separated its slots.
            # Do not replace its cleaned product literal with the broader raw
            # prefix recognized by the generic word-order parser.
            return
        if request.metrics or request.primary_intent in {
            PrimaryIntent.REPORT_GENERATION,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            PrimaryIntent.FORECAST_ANALYSIS,
            PrimaryIntent.DATA_LINEAGE,
            PrimaryIntent.DATA_QUALITY,
        }:
            return
        if re.search(
            r"多少|几家|数量|个数|总数|销售额|销售量|销量|金额|订单|次数|"
            r"最高|最低|最大|最小|排名|排行|排序|趋势|走势|同比|环比|占比|平均",
            compact,
        ):
            return

        target_pattern = r"经销商|供应商|医院|客户|门店|厂家"
        ask_pattern = r"哪些|哪几家|都有谁|有谁|是谁|什么"
        matches = (
            # A产品的经销商有哪些 / A产品有哪些经销商
            re.search(
                rf"(?P<scope>.+?)(?:产品|商品)(?:对应的|(?:已)?合作(?:的)?|的)?"
                rf"(?P<target>{target_pattern})(?:都)?(?:有|是|包括|包含)?"
                rf"(?:{ask_pattern})[。！!？?]*$",
                compact,
            ),
            re.search(
                rf"(?P<scope>.+?)(?:产品|商品)(?:都)?(?:有|包括|包含|对应)?"
                rf"(?:{ask_pattern})(?P<target>{target_pattern})[。！!？?]*$",
                compact,
            ),
            # A产品由哪些经销商销售
            re.search(
                rf"(?P<scope>.+?)(?:产品|商品)(?:是)?由(?:{ask_pattern})"
                rf"(?P<target>{target_pattern})(?:销售|经销|代理|供应|采购|使用)?"
                r"[。！!？?]*$",
                compact,
            ),
            # 哪些经销商销售A产品
            re.search(
                rf"(?:{ask_pattern})(?P<target>{target_pattern})"
                r"(?:正在|目前|曾经|在)?(?:销售|经销|代理|供应|采购|使用|覆盖)"
                r"(?P<scope>.+?)(?:产品|商品)[。！!？?]*$",
                compact,
            ),
            # A都有哪些经销商在卖 / A产品都通过哪些经销商渠道销售。
            # These colloquial forms express the same product-to-partner set
            # projection as an explicit “经销商名单”.
            re.search(
                rf"(?P<scope>.+?)(?:这个)?(?:产品|商品)?(?:都)?"
                rf"(?:有|通过)?(?:{ask_pattern})(?P<target>{target_pattern})"
                r"(?:渠道)?(?:在卖|销售|经销|代理|供应|合作)?(?:的)?"
                r"[，,]?(?:给我|提供)?(?:一份)?(?:名单|清单|列表)?[。！!？?]*$",
                compact,
            ),
            # 查询A产品合作经销商 / 列出A产品厂家。The explicit query
            # verb already supplies the list action, so no interrogative or
            # 名单 suffix is required.
            re.search(
                rf"^(?:请|麻烦|帮我|给我|请帮我)?"
                rf"(?:查询|查找|找出|列出|展示|显示|查看|筛选|提供)"
                rf"(?P<scope>.+?)(?:产品|商品)"
                rf"(?:对应的|(?:已)?合作(?:的)?|的)?"
                rf"(?P<target>{target_pattern})(?:名单|清单|列表)?[。！!？?]*$",
                compact,
            ),
            # A产品经销商 / 上海A产品合作医院。Short search-box
            # utterances commonly omit both a query verb and an interrogative.
            re.search(
                rf"^(?P<scope>.+?)(?:产品|商品)"
                rf"(?:对应的|(?:已)?合作(?:的)?|的)?"
                rf"(?P<target>{target_pattern})(?:名单|清单|列表)?[。！!？?]*$",
                compact,
            ),
        )
        match = next((candidate for candidate in matches if candidate is not None), None)
        if match is None:
            return

        scope = match.group("scope")
        excluded_maker_match = re.match(
            r"^排除(?P<maker>[^，,。；;]{1,100}?)厂家(?:的)?",
            scope,
        )
        excluded_maker = (
            excluded_maker_match.group("maker").strip("的，,；;、")
            if excluded_maker_match is not None
            else None
        )
        scope = re.sub(
            r"^(?:请|麻烦|帮我|给我|请帮我|我想知道|我想查|我想查询)?"
            r"(?:查询|查找|找出|列出|展示|显示|查看|看看|筛选|提供)?",
            "",
            scope,
        )
        scope = re.sub(
            r"^(?:北京|上海|天津|重庆)(?:市|地区)?",
            "",
            scope,
        )
        scope = re.sub(
            r"^排除[^，,。；;]{1,100}?厂家(?:的)?",
            "",
            scope,
        ).strip("的，,；;、")
        if not 1 <= len(scope) <= 100 or any(ord(char) < 32 for char in scope):
            return

        target = match.group("target")
        request.primary_intent = PrimaryIntent.DETAIL_QUERY
        request.entity = target
        request.metrics = []
        request.fields = [f"{target}名称"]
        request.dimensions = [target]
        request.risk_level = "HIGH"
        request.operators = [
            operator for operator in request.operators
            if operator != AnalysisOperator.AGGREGATE
        ]
        for operator in (AnalysisOperator.FILTER, AnalysisOperator.RENDER_TABLE):
            if operator not in request.operators:
                request.operators.append(operator)
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {
                "商品名称", "产品名称", "商品", "产品",
            }
        ]
        if excluded_maker and not any(
            str(item.get("field") or "") in {"厂家名称", "厂家"}
            and str(item.get("operator") or "").upper() in {"NE", "NOT_EQ"}
            for item in request.filters
        ):
            request.filters.append({
                "field": "厂家名称", "operator": "NE", "value": excluded_maker,
            })
        request.filters.append({
            # Provisional family: semantic grounding may rebind this value to
            # a current brand/category/manufacturer attribute when appropriate.
            "field": "商品名称", "operator": "EQ", "value": scope,
        })
        request.semantic_entity_mentions = list(dict.fromkeys([
            *request.semantic_entity_mentions,
            scope,
        ]))
        if "SET_RELATIONSHIP_PROJECTION" not in request.assumptions:
            request.assumptions.append("SET_RELATIONSHIP_PROJECTION")
        if "TRANSACTION_TIME_SCOPE=SALES_RECORD" not in request.assumptions:
            # Product-to-partner relationships in this agent are defined by
            # sales facts in the requested/default active period. Preserve
            # that trusted time meaning when an entity follow-up forces a new
            # ASL plan; otherwise the semantic service can ask an unnecessary
            # transaction-time clarification.
            request.assumptions.append(
                "TRANSACTION_TIME_SCOPE=SALES_RECORD"
            )

    @staticmethod
    def _apply_relationship_product_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        relationship_count = any(
            metric.input in {
                "已合作医院数", "已合作经销商数", "已合作供应商数",
                "已合作客户数", "已合作门店数",
            }
            for metric in request.metrics
        )
        if request.primary_intent != PrimaryIntent.DETAIL_QUERY and not relationship_count:
            return
        match = re.search(
            r"^(?:(?:请|帮我|给我)?(?:查询|查找|列出|展示|显示|统计|计算)?)"
            r"(?:上海|北京|天津|重庆)(?:市|地区)?"
            r"(?P<product>[^，,。；;]{2,80}?)(?:已)?合作(?:的)?(?:医院|经销商|供应商|客户|门店)(?:数量|数|名单|清单)?"
            r"|^(?:(?:请|帮我|给我)?(?:查询|查找|列出|展示|显示|统计|计算)?)"
            r"(?P<plain>[^，,。；;]{2,80}?)"
            r"(?:已)?合作(?:的)?(?:医院|经销商|供应商|客户|门店)(?:数量|数|名单|清单)?",
            text,
        )
        if match is None:
            return
        product = (match.group("product") or match.group("plain") or "").strip("的")
        if not product:
            return
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") != "商品名称"
        ]
        request.filters = [
            {**item, "field": "业务城市"}
            if str(item.get("field") or "") in {"地区", "区域", "城市"}
            else item
            for item in request.filters
        ]
        request.filters.append({
            "field": "商品名称", "operator": "EQ", "value": product,
        })

    @classmethod
    def _apply_transaction_partner_scope(
        cls, request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Separate an activity predicate from its catalog entity literal.

        ``最近一年销售过费森尤斯产品的经销商`` contains three independent
        slots: a time range, transaction activity and a catalog value.  Model
        completion may occasionally concatenate the first two into the value
        (``最近一年销售过费森尤斯``).  Keep only the catalog literal here; the
        current semantic-model entity search subsequently decides whether it
        is a brand, manufacturer, category or exact product field.
        """

        if not cls._uses_transaction_activity_definition(text):
            return
        match = re.search(
            r"(?P<scope>[^，,。；;？?]{2,120}?)(?:产品|商品)(?:的)?"
            r"(?P<partner>经销商|供应商)(?:名单|清单|列表)?",
            text,
        )
        if match is None or "品牌" in match.group("scope"):
            # Explicit ``<brand>品牌<product>`` is handled by the catalog
            # splitter, which must retain both filters.
            return
        scope = re.sub(
            r"^(?:请|麻烦|帮我|给我|请帮我)?"
            r"(?:查询|查找|找出|列出|展示|显示|查看|看看|筛选|提供)?",
            "",
            match.group("scope"),
        )
        scope = re.sub(
            r"^(?:(?:最近|近|过去)"
            r"(?:\d+|[一二两三四五六七八九十]+)(?:个)?(?:天|日|周|月|季度|年)"
            r"(?:内|期间)?|本年度|今年)?"
            r"(?:正在销售|销售过|曾经销售|发生过销售|卖过|经营过?)",
            "",
            scope,
        ).strip("的，,；;、")
        if not 1 <= len(scope) <= 80 or any(ord(char) < 32 for char in scope):
            return

        partner = match.group("partner")
        request.primary_intent = PrimaryIntent.DETAIL_QUERY
        request.entity = partner
        request.metrics = []
        request.fields = [f"{partner}名称"]
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {
                "商品名称", "产品名称", "商品品牌", "品牌名称", "母品牌",
                "厂家", "厂家名称",
            }
        ]
        request.filters.append({
            # This provisional family is intentionally rebound by
            # QuestionRewriter.ground_request_dimensions from the latest
            # semantic entity catalog when a stronger brand/category match is
            # available.
            "field": "商品名称", "operator": "EQ", "value": scope,
        })
        if "SET_RELATIONSHIP_PROJECTION" not in request.assumptions:
            request.assumptions.append("SET_RELATIONSHIP_PROJECTION")
        if (
            "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
            not in request.assumptions
        ):
            request.assumptions.append(
                "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
            )

    @staticmethod
    def _apply_brand_product_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        if request.primary_intent not in {
            PrimaryIntent.METRIC_QUERY, PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS, PrimaryIntent.REPORT_GENERATION,
        }:
            return
        match = re.search(
            r"(?:北京|上海|天津|重庆)(?:市|地区)?(?P<brand>[^，,。；;]{2,24}?)产品"
            r"(?=(?:最近|近|过去|本|20\d{2}|的)?(?:一|二|三|四|五|六|七|八|九|十|\d+)?"
            r"(?:年|月|季度)?(?:的)?(?:销售|含税|订单|趋势|$))",
            text,
        )
        if match is None:
            return
        brand = match.group("brand").strip("的")
        if not brand:
            return
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {"商品名称", "品牌名称", "母品牌"}
        ]
        # Keep the brand phrase in the natural-language semantic query. Model
        # 81 has multiple governed brand/manufacturer name attributes; forcing
        # a generic caller field here creates an artificial equal-priority
        # ambiguity. The important deterministic correction is to remove the
        # incorrect ``商品名称=<地区+品牌+时间>`` filter.
        request.dimensions = [
            item for item in request.dimensions if item not in {"产品", "商品"}
        ]

    @staticmethod
    def _apply_explicit_dealer_metric_scope(
        request: CanonicalAnalysisRequest, question: str
    ) -> None:
        """Bind a named dealer in a possessive metric question.

        Follow-ups such as ``那上海某某有限公司最近一个月的销售额呢``
        select one row from the previous dealer table.  The company name is a
        filter value, not another free-text product/category phrase.  Only the
        current user utterance is inspected so an appended context block cannot
        manufacture an explicit dealer selection.
        """

        if request.primary_intent not in {
            PrimaryIntent.METRIC_QUERY,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
        }:
            return
        user_text = question.split("\n", 1)[0]
        match = re.search(
            r"(?:那|那么|查询|查|看)?(?P<name>[^，,。；;？?\n]{2,80}?"
            r"(?:有限责任公司|股份有限公司|有限公司))"
            r"(?:的)?(?:最近|近|过去|本月|上月|今年|去年|20\d{2}年|"
            r"含税|销售|订单|趋势|同比|环比)",
            re.sub(r"\s+", "", user_text),
        )
        if match is None:
            return
        dealer_name = match.group("name").lstrip("那那么查询查看").strip("的")
        if not dealer_name:
            return
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") != "经销商名称"
        ]
        request.filters.append({
            "field": "经销商名称", "operator": "EQ", "value": dealer_name,
        })

    @staticmethod
    def _apply_time_grouped_metric_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Keep time-bucket statistics separate from trend interpretation."""

        match = re.search(
            r"(?:改成|换成|还是)?按(?P<grain>日|天|周|月|季度|年)"
            r"(?:统计|汇总|展示|显示|查看|给我)?",
            text,
        )
        if match is None:
            return
        if any(
            marker in text
            for marker in (
                "趋势", "走势", "变化", "涨跌", "上升", "下降",
                "增长", "波动", "分析趋势", "趋势分析",
            )
        ):
            return
        grain = {
            "日": "day", "天": "day", "周": "week", "月": "month",
            "季度": "quarter", "年": "year",
        }[match.group("grain")]
        request.primary_intent = PrimaryIntent.METRIC_QUERY
        request.risk_level = "MEDIUM"
        request.operators = [
            AnalysisOperator.AGGREGATE,
            AnalysisOperator.TIME_BUCKET,
            AnalysisOperator.GROUP_BY,
            AnalysisOperator.RENDER_TABLE,
        ]
        request.assumptions = [
            value for value in request.assumptions
            if not value.startswith("DEFAULT_TIME_GRANULARITY=")
        ]
        request.assumptions.append(f"DEFAULT_TIME_GRANULARITY={grain}")

    @staticmethod
    def _coverage_detail_shape(text: str) -> tuple[str, str] | None:
        """Infer the row entity and minimum projection of a coverage facet."""
        shapes = (
            (r"医院(?:覆盖|分布)|医院数据|(?:哪些|合作(?:的|了)?)医院|医院(?:名单|清单)", "医院", "医院名称"),
            (r"经销商(?:覆盖|分布)|(?:合作)?经销商数据", "经销商", "经销商名称"),
            (r"供应商(?:覆盖|分布)|(?:合作)?供应商数据", "供应商", "供应商名称"),
            (r"客户(?:覆盖|分布)|客户数据", "客户", "客户名称"),
            (r"门店(?:覆盖|分布)|门店数据", "门店", "门店名称"),
        )
        return next(
            ((entity, field) for pattern, entity, field in shapes if re.search(pattern, text)),
            None,
        )

    @staticmethod
    def _apply_department_partner_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Ground department-led dealer recommendations as name filters.

        Users commonly state the product's applicable departments before the
        recommendation verb (``适用于泌尿外科、肾脏内科，推荐经销商``).
        Treating those names as a vague comparison loses the relationship
        predicate and makes the semantic service guess between a code and a
        display name.  Only explicit department suffixes are accepted here.
        """
        if not (
            "经销商" in text
            and any(marker in text for marker in ("推荐", "筛选", "匹配", "查找"))
        ):
            return
        match = re.search(
            r"适用于(?P<departments>[^，,。；;]{2,80}?)(?=，|,|。|；|;|帮我|请|推荐|筛选|匹配|查找|$)",
            text,
        )
        if match is None:
            return
        departments = [
            value.strip()
            for value in re.split(r"[、和及与/]", match.group("departments"))
            if value.strip().endswith(("科", "科室", "中心"))
        ]
        if not departments:
            return

        request.entity = "经销商"
        request.dimensions = list(dict.fromkeys([*request.dimensions, "经销商"]))
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {"适用科室", "科室名称"}
        ]
        request.filters.append({
            "field": "科室名称",
            "operator": "IN",
            "value": departments,
        })
        if "上海" in text and not any(
            str(item.get("field") or "") == "地区" for item in request.filters
        ):
            request.filters.append({
                "field": "地区", "operator": "EQ", "value": "上海市",
            })

    @classmethod
    def _apply_report_business_scope(
        cls, request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Keep an explicit region/product scope across a time clarification.

        Canonical clarification merging deliberately rebuilds executable text
        from slots.  Report wording often carries its scope only in a sentence
        such as “在上海卖某产品” or immediately before a requested report facet.
        Ground those two literals as ordinary filters before the request is
        persisted, so the later time answer cannot erase them.
        """
        if request.primary_intent not in {
            PrimaryIntent.REPORT_GENERATION,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.METRIC_QUERY,
            PrimaryIntent.DETAIL_QUERY,
        }:
            return

        region_pattern = (
            r"(?:北京|上海|天津|重庆)(?:市)?|"
            r"(?:香港|澳门)特别行政区|"
            r"[\u4e00-\u9fff]{2,12}(?:省|自治区|市|地区)"
        )
        region: str | None = None
        subject: str | None = None

        selling = re.search(
            rf"(?:我)?在(?P<region>{region_pattern})(?:地区)?"
            r"(?:卖|销售|经营)(?P<subject>[^，,。；;]{2,100}?)"
            r"(?=给我|帮我|请|生成|输出|做|，|,|。|；|;|$)",
            text,
        )
        if selling:
            region = selling.group("region")
            subject = selling.group("subject")
        else:
            body = re.sub(r"^(?:请|帮我|给我)?(?:分析|列出|查询|统计|查看|看看)", "", text)
            facet = re.search(
                r"(?:整体)?销售(?:趋势|走势|变化)|"
                r"(?:医院|经销商|供应商|客户|门店)(?:覆盖|分布)|"
                r"(?:合作)?(?:经销商|供应商)(?:数据|情况|清单|名单|画像|表现)|"
                r"已合作(?:医院|经销商|供应商|客户|门店)数",
                body,
            )
            if facet:
                scoped = body[:facet.start()].strip("，,；;、")
                located = re.match(rf"^(?P<region>{region_pattern})(?P<subject>.+)$", scoped)
                if located:
                    region = located.group("region")
                    subject = located.group("subject")

        if not region or not subject:
            return
        subject = subject.strip("的，,；;、")
        if (
            not 2 <= len(subject) <= 100
            or any(marker in subject for marker in ("报告", "报表", "趋势", "覆盖", "明细"))
            or any(ord(char) < 32 for char in subject)
        ):
            return
        if region in {"北京", "上海", "天津", "重庆"}:
            region += "市"

        def put_filter(field: str, value: str) -> None:
            request.filters = [
                item for item in request.filters
                if str(item.get("field") or "") != field
            ]
            request.filters.append({"field": field, "operator": "EQ", "value": value})

        put_filter("地区", region)
        put_filter("商品名称", subject)
        if request.primary_intent in {
            PrimaryIntent.REPORT_GENERATION,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.METRIC_QUERY,
        }:
            request.entity = "产品"

    @staticmethod
    def _apply_catalog_partner_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Split a branded partner scope into region, brand and catalog item.

        The split is intentionally lexical and conservative.  Values ending
        in an explicit catalog-class marker (for example ``低值耗材`` or
        ``办公用品品类``) become ``商品分类`` filters.  Other values remain an
        exact ``商品名称`` filter, so a concrete product following a brand is
        never weakened into a broad category merely because both appear in
        the same phrase.  Brand aliases themselves are left untouched here;
        the semantic service must ground them against source catalog fields.
        """
        compact = re.sub(r"\s+", "", text)
        if "品牌" not in compact or not any(
            partner in compact for partner in ("经销商", "供应商")
        ):
            return

        partner_match = re.search(r"(?:产品)?的?(?:经销商|供应商)", compact)
        if partner_match is None:
            return
        scope = compact[:partner_match.start()]
        scope = re.sub(
            r"^(?:请|麻烦|帮我|给我|请帮我)?"
            r"(?:查询|查找|找出|列出|展示|显示|统计|计算|筛选|提供|推荐|看看|查看)?",
            "",
            scope,
        )
        # The partner matcher may begin at the target noun in colloquial
        # questions such as “振德品牌的医用外科口罩有哪些经销商”.  Remove the
        # interrogative tail before splitting brand and catalog slots; otherwise
        # ``医用外科口罩有哪些`` becomes a bogus product master-data value and
        # later triggers SQL_QUERY_ENTITY_ALIGNMENT_FAILED.
        scope = re.sub(
            r"(?:都)?(?:有|包括|包含)?(?:哪些|哪几家|都有谁|有谁|是谁|什么)$",
            "",
            scope,
        ).strip("的，,；;、")

        region_match = re.match(
            r"(?P<region>"
            r"(?:北京|上海|天津|重庆)(?:市|地区)?|"
            r"(?:香港|澳门)特别行政区|"
            r"[\u4e00-\u9fff]{2,12}(?:省|自治区|市|地区)"
            r")",
            scope,
        )
        region: str | None = None
        if region_match:
            region = re.sub(r"地区$", "", region_match.group("region"))
            if region in {"北京", "上海", "天津", "重庆"}:
                region += "市"
            scope = scope[region_match.end():]

        brand_marker = scope.find("品牌")
        if brand_marker < 0:
            return
        brand = scope[:brand_marker]
        brand = re.sub(
            r"^(?:限定|在|做|经营|经销|代理|供应|正在销售|目前销售|销售)+",
            "",
            brand,
        ).strip("的，,；;、")
        if not 1 <= len(brand) <= 80 or any(ord(char) < 32 for char in brand):
            return

        catalog_value = scope[brand_marker + len("品牌"):]
        catalog_value = catalog_value.strip("的，,；;、")
        catalog_value = re.sub(r"(?:的)?(?:所有|全部)$", "", catalog_value).strip("的")
        catalog_value = re.sub(r"产品$", "", catalog_value).strip("的")

        category: str | None = None
        product: str | None = None
        explicit_category = re.fullmatch(
            r"(?P<value>.{1,100}?)(?P<marker>商品分类|产品分类|品类|类目|类别)",
            catalog_value,
        )
        if explicit_category:
            category = explicit_category.group("value").strip("的")
        elif catalog_value.endswith("耗材"):
            category = catalog_value
        elif catalog_value not in {"", "产品", "商品", "货品", "物料"}:
            product = catalog_value

        def replace_filter(field: str, value: str, aliases: set[str]) -> None:
            request.filters = [
                item for item in request.filters
                if str(item.get("field") or "") not in aliases
            ]
            request.filters.append({"field": field, "operator": "EQ", "value": value})

        if region:
            replace_filter(
                "城市", region, {"地区", "区域", "省份", "城市", "业务城市"}
            )
        replace_filter(
            "商品品牌", brand, {"品牌", "品牌名称", "商品品牌", "母品牌"}
        )

        # Remove the broad ``商品名称=<brand>品牌<category>`` guess produced by
        # a preceding generic scope extractor, even when the user only said
        # ``品牌产品`` and therefore supplied no concrete catalog value.
        request.filters = [
            item for item in request.filters
            if str(item.get("field") or "") not in {
                "商品名称", "产品名称", "商品分类", "产品分类", "商品品类", "品类",
            }
        ]
        if category:
            replace_filter(
                "商品品类",
                category,
                {"商品分类", "产品分类", "商品品类", "品类", "类目", "类别"},
            )
        elif product:
            replace_filter(
                "商品名称", product, {"商品名称", "产品名称", "商品", "产品"}
            )

        # These are semantic dimension roles, not database columns.  Oagnet
        # resolves each role against the dimensions and relationship graph of
        # the *current* semantic-model version.  Keeping every explicit scope
        # role visible prevents a compound partner query from being flattened
        # to only ``经销商`` or from inventing one synthetic product dimension.
        partner = "经销商" if "经销商" in compact else "供应商"
        scalar_relationship_count = any(
            metric.input in {"已合作经销商数", "已合作供应商数"}
            for metric in request.metrics
        ) and not re.search(
            rf"(?:按|各|每(?:家|个)|分){partner}|{partner}维度",
            compact,
        )
        scoped_dimensions: list[str] = []
        if not scalar_relationship_count:
            scoped_dimensions.append(partner)
            if region:
                scoped_dimensions.append("城市")
            scoped_dimensions.append("商品品牌")
            if category:
                scoped_dimensions.append("商品品类")
            elif product:
                scoped_dimensions.append("商品名称")
        catalog_dimension_aliases = {
            "地区", "区域", "省份", "城市", "业务城市",
            "品牌", "品牌名称", "商品品牌", "母品牌",
            "商品分类", "产品分类", "商品品类", "品类", "类目", "类别",
            "商品", "产品", "商品名称", "产品名称",
            "经销商", "供应商",
        }
        remaining_dimensions = [
            value for value in request.dimensions
            if value not in catalog_dimension_aliases
        ]
        request.dimensions = list(dict.fromkeys([
            *scoped_dimensions,
            *remaining_dimensions,
        ]))

    @staticmethod
    def _apply_ranked_partner_scope(
        request: CanonicalAnalysisRequest, text: str
    ) -> None:
        """Persist grounded partner-ranking filters across a time follow-up."""
        scope_text = re.sub(
            r"^(?:请|麻烦|帮我|给我|请帮我)?(?:查询|查找|找出|列出|筛选|看看|查看)?",
            "",
            text,
        )
        region_match = re.search(
            r"(?:北京|上海|天津|重庆)(?:市|地区)?|"
            r"(?:香港|澳门)特别行政区|"
            r"[\u4e00-\u9fff]{2,12}(?:省|自治区|市|地区)",
            scope_text,
        )
        brand_match = re.search(
            r"(?:正在|目前)?销售(?P<value>[^，,。；;]{2,40}?)品牌",
            scope_text,
        )
        excluded_maker = re.search(
            r"排除(?P<value>[^，,。；;]{2,40}?)(?:厂家|制造商)",
            scope_text,
        )
        partner_scope = re.search(
            r"(?P<value>[^，,。；;]{2,180}?)(?:产品)?的?"
            r"(?:经销商|供应商)(?:名单|清单)?(?=，|,|并|按|$)",
            scope_text,
        )

        def put_filter(field: str, operator: str, value: str) -> None:
            request.filters = [
                item for item in request.filters
                if str(item.get("field") or "") != field
            ]
            request.filters.append({
                "field": field, "operator": operator, "value": value,
            })

        region: str | None = None
        if region_match:
            region = region_match.group(0)
            region = re.sub(r"地区$", "", region)
            if region in {"北京", "上海", "天津", "重庆"}:
                region += "市"
            put_filter("地区", "EQ", region)
        if brand_match:
            brand = brand_match.group("value").strip("的")
            if 2 <= len(brand) <= 40:
                put_filter("品牌名称", "EQ", brand)
        if excluded_maker:
            maker = excluded_maker.group("value").strip("的")
            if 2 <= len(maker) <= 40:
                put_filter("厂家名称", "NE", maker)

        if partner_scope:
            product = partner_scope.group("value")
            if region_match and region_match.group(0) in product:
                product = product.split(region_match.group(0), 1)[1]
            if brand_match and brand_match.group(0) in product:
                product = product.split(brand_match.group(0), 1)[1]
            product = re.sub(
                r"^(?:请|帮我|给我|找出|列出|查询|正在|目前|销售|的)+",
                "",
                product,
            ).strip("的，,；;、")
            product = re.sub(r"产品$", "", product)
            if (
                2 <= len(product) <= 100
                and not any(ord(char) < 32 for char in product)
            ):
                put_filter("商品名称", "EQ", product)

    @classmethod
    def _is_ranked_partner_recommendation(cls, text: str) -> bool:
        """Recognize partner recommendations without confusing sales rankings."""
        compact = re.sub(r"\s+", "", text).lower()
        if not any(partner in compact for partner in ("经销商", "供应商")):
            return False
        if "推荐" in compact or "自动筛选" in compact:
            return True
        return bool(
            cls._ranking_limit(compact) is not None
            and not cls._extract_metric_refs(compact)
            and any(
                signal in compact
                for signal in (
                    "适用科室", "对应科室", "医院渠道", "医院等级",
                    "产品适用", "合适的", "画像",
                )
            )
        )

    @staticmethod
    def _generic_partner_count_needs_names(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"(?:对比|比较)(?:这|上述|以下)?"
            r"(?:\d{1,2}|两|二|三|四|五|六|七|八|九|十)家(?:供应商|经销商)",
            compact,
        ))

    @staticmethod
    def _expected_partner_count(text: str) -> int | None:
        match = re.search(
            r"(?:对比|比较)(?:这|上述|以下)?"
            r"(?P<count>\d{1,2}|两|二|三|四|五|六|七|八|九|十)"
            r"家(?:供应商|经销商)",
            re.sub(r"\s+", "", text),
        )
        if not match:
            return None
        value = match.group("count")
        chinese = {
            "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }
        return int(value) if value.isdigit() else chinese[value]

    @staticmethod
    def _comparison_object_filter_present(
        filters: list[dict], partner: str | None = None
    ) -> bool:
        expected_fields = (
            {f"{partner}名称"} if partner else {"经销商名称", "供应商名称"}
        )
        return any(
            str(item.get("field") or "") in expected_fields
            and str(item.get("operator") or "").upper() == "IN"
            and isinstance(item.get("value"), list)
            and len(item["value"]) >= 2
            for item in filters
        )

    @staticmethod
    def _comparison_object_candidates(
        answer: str, expected_count: int | None
    ) -> list[str]:
        value = re.sub(r"[\r\n]+", "、", answer).strip(" ，,。.!！?？;；：:")
        value = re.sub(
            r"^(?:要对比的)?(?:经销商|供应商)?(?:名称)?"
            r"(?:分别)?(?:是|为|包括|选择|选用)?[：:]?",
            "",
            value,
        )
        values = [
            item.strip(" '\"“”‘’()（）[]【】")
            for item in re.split(r"[、,，;；/]+", value)
        ]
        values = list(dict.fromkeys(item for item in values if item))
        invalid = {
            "这几家", "这三家", "上述几家", "上述三家", "经销商", "供应商",
            "不知道", "不清楚", "随便", "任意",
        }
        if any(item in invalid or len(item) > 100 for item in values):
            return []
        if expected_count is not None and len(values) != expected_count:
            return []
        return values if len(values) >= 2 else []

    @staticmethod
    def _has_explicit_comparison_pair(text: str) -> bool:
        """Require two concrete periods before accepting a specified-period comparison."""
        compact = re.sub(r"\s+", "", text)
        month_tokens = re.findall(r"(?:(?:19|20)\d{2}年)?(?:1[0-2]|0?[1-9])月", compact)
        if len(month_tokens) >= 2:
            return True
        date_tokens = re.findall(
            r"(?:19|20)\d{2}[-/.年](?:1[0-2]|0?[1-9])[-/.月](?:3[01]|[12]\d|0?[1-9])日?",
            compact,
        )
        if len(date_tokens) >= 2:
            return True
        relative_periods = re.findall(
            r"本月|这个月|上月|上个月|本季度|上季度|今年|去年|本周|上周",
            compact,
        )
        return len(set(relative_periods)) >= 2

    @staticmethod
    def _has_explicit_object_comparison(text: str) -> bool:
        """Recognise two named business objects joined by 和/与/及/跟."""
        compact = re.sub(r"\s+", "", text)
        if not re.search(r"对比|比较", compact):
            return False
        # Keep the operands bounded by common metric/time words so a long
        # analytical sentence cannot accidentally become one opaque object.
        operand = r"[^，,。；;？?]{1,32}?"
        pair = re.search(
            rf"(?:对比|比较)?(?P<left>{operand})(?:和|与|及|跟)(?P<right>{operand})"
            r"(?=最近|过去|本年|今年|去年|20\d{2}年|的|在|按|销售|订单|客户|覆盖|$)",
            compact,
        )
        if pair is None:
            return False
        left = pair.group("left").lstrip("对比比较")
        right = pair.group("right")
        invalid = {"销售额", "订单量", "客户数", "数量", "销量", "金额", "趋势"}
        return bool(left and right and left not in invalid and right not in invalid)

    def merge_clarification(
        self, pending: CanonicalAnalysisRequest, answer: str
    ) -> CanonicalAnalysisRequest:
        prior_missing = set(pending.missing_slots)
        parsed = self.classify(
            answer,
            TrustedIdentity(tenant_id=pending.tenant_id, user_id=pending.user_id),
            pending.conversation_id,
        )
        if parsed.conversation_control == ConversationControl.CANCEL:
            pending.conversation_control = ConversationControl.CANCEL
            pending.missing_slots = []
            return pending
        pending.conversation_control = (
            parsed.conversation_control
            if parsed.conversation_control != ConversationControl.NEW_REQUEST
            else ConversationControl.CLARIFICATION_RESPONSE
        )
        # Preserve the raw turn sequence for audit only. The executable text is
        # rebuilt from final canonical slots below and must never be old text
        # with a correction appended to it.
        original_question = pending.original_question
        pending.original_question = f"{original_question}\n补充：{answer}"
        negated_metrics = {
            metric.input
            for metric in pending.metrics
            if self._slot_is_negated(answer, metric.canonical_name or metric.input)
            or self._slot_is_negated(answer, metric.input)
        }
        parsed_metrics = [
            metric for metric in parsed.metrics
            if not self._slot_is_negated(answer, metric.input)
        ]
        if self._clear_all_metrics(answer):
            pending.metrics = []
        elif negated_metrics:
            pending.metrics = [
                metric for metric in pending.metrics
                if metric.input not in negated_metrics
            ]
        if parsed_metrics:
            metrics = parsed_metrics
            if parsed.conversation_control == ConversationControl.CORRECTION:
                normalized_answer = re.sub(r"\s+", "", answer)
                replacement = next(
                    (
                        match.group(1)
                        for pattern in (
                            r"不是.+?(?:而是|是)(.+)$",
                            r"(?:改成|改为|更正为|应该是)(.+)$",
                        )
                        if (match := re.search(pattern, normalized_answer))
                    ),
                    None,
                )
                if replacement:
                    replacement_names = [
                        name for name in self._known_metrics if name in replacement
                    ]
                    if replacement_names:
                        metrics = [MetricRef(input=name) for name in replacement_names]
            additive_metric_change = bool(re.search(
                r"(?:再|同时|并)?(?:加上|增加|新增|补充|带上|显示|返回).{0,20}"
                r"(?:指标|金额|销售|数量|笔数|次数|均价|单价|利润|成本|收入)",
                answer,
            ))
            if additive_metric_change:
                by_name = {
                    metric.canonical_name or metric.input: metric
                    for metric in [*pending.metrics, *metrics]
                }
                pending.metrics = list(by_name.values())
            else:
                pending.metrics = metrics
        parsed_uses_default_time = (
            "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in parsed.assumptions
        )
        if parsed.time_range and not parsed_uses_default_time:
            pending.time_range = parsed.time_range
            pending.assumptions = [
                value for value in pending.assumptions
                if value != "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"
            ]
        if parsed.entity:
            pending.entity = parsed.entity
        if parsed.fields:
            pending.fields = parsed.fields
        explicit_relationship_detail = bool(re.search(
            r"(?:它|该产品|这个产品|这些产品)?.{0,16}(?:卖给|销售给)(?:了)?哪些医院"
            r"|哪些医院.{0,16}(?:购买|采购|使用)(?:了)?(?:它|该产品|这个产品)"
            r"|(?:这些|上述|前述)?医院.{0,16}(?:什么|哪些|分别是)?(?:等级|级别)"
            r"|(?:哪些|什么)(?:产品|商品|医院|经销商|供应商)(?:名单|清单)?[？?。]?$",
            re.sub(r"\s+", "", answer),
        ))
        if (
            parsed.primary_intent == PrimaryIntent.DETAIL_QUERY
            and explicit_relationship_detail
        ):
            # A relationship question is an intentional shape switch, not a
            # request to repeat the preceding aggregate/ranking result.
            pending.primary_intent = PrimaryIntent.DETAIL_QUERY
            pending.secondary_intents = list(parsed.secondary_intents)
            pending.metrics = []
            pending.operators = list(parsed.operators)
            pending.risk_level = "HIGH"
            # The previous ASL describes an aggregate/ranking projection and
            # must never be reused after switching to a relationship-detail
            # shape.  Reusing it repeats the old product metric table even
            # though the visible intent has changed to DETAIL_QUERY.
            pending.asl_template = None
            pending.source_dataset_id = None
        negated_dimensions = {
            dimension for dimension in pending.dimensions
            if self._slot_is_negated(answer, dimension, dimension=True)
        }
        parsed_dimensions = [
            dimension for dimension in parsed.dimensions
            if not self._slot_is_negated(answer, dimension, dimension=True)
        ]
        if self._clear_all_dimensions(answer):
            pending.dimensions = []
        elif negated_dimensions:
            pending.dimensions = [
                dimension for dimension in pending.dimensions
                if dimension not in negated_dimensions
            ]
        if parsed_dimensions:
            pending.dimensions = parsed_dimensions
        if self._clear_all_filters(answer):
            pending.filters = []
        elif pending.filters:
            retained_filters = [
                item for item in pending.filters
                if not self._filter_is_negated(answer, item)
            ]
            removed_filters = [
                item for item in pending.filters if item not in retained_filters
            ]
            replacement_value = self._replacement_filter_value(answer)
            if (
                len(removed_filters) == 1
                and replacement_value
                and "value" in removed_filters[0]
            ):
                replacement = dict(removed_filters[0])
                replacement["value"] = replacement_value
                retained_filters.append(replacement)
            pending.filters = retained_filters
        # Explicit filters extracted from the current utterance override the
        # same slot from short-term context.  Previously merge_clarification
        # copied metrics/time/entity but silently discarded parsed filters, so
        # “那上海某某有限公司的销售额” retained only the prior brand/category
        # scope and never constrained the named dealer.
        for parsed_filter in parsed.filters:
            field = str(parsed_filter.get("field") or "")
            if not field:
                continue
            pending.filters = [
                item for item in pending.filters
                if str(item.get("field") or "") != field
            ]
            pending.filters.append(dict(parsed_filter))
        if parsed.comparison_type:
            pending.comparison_type = parsed.comparison_type
        if "comparison_objects" in prior_missing:
            partner = next(
                (
                    item for item in ("经销商", "供应商")
                    if item in pending.dimensions
                    or item == pending.entity
                    or item in (pending.original_question or "")
                ),
                None,
            )
            names = self._comparison_object_candidates(
                answer,
                self._expected_partner_count(pending.original_question or ""),
            )
            if partner and names:
                field = f"{partner}名称"
                pending.filters = [
                    item for item in pending.filters
                    if str(item.get("field") or "") != field
                ]
                pending.filters.append({
                    "field": field,
                    "operator": "IN",
                    "value": names,
                })
        if parsed.ranking_limit:
            pending.ranking_limit = parsed.ranking_limit
        if parsed.forecast_horizon_periods:
            pending.forecast_horizon_periods = parsed.forecast_horizon_periods
            pending.forecast_granularity = parsed.forecast_granularity
        if parsed.forecast_history_provided:
            pending.forecast_history_provided = True
        # “改成按季度/按月统计” changes only the grouping grain.  It is a
        # grouped metric query, not permission to retain the previous trend
        # analysis intent and produce an interpretive trend conclusion.
        if (
            parsed.primary_intent == PrimaryIntent.METRIC_QUERY
            and any(
                value.startswith("DEFAULT_TIME_GRANULARITY=")
                for value in parsed.assumptions
            )
            and re.search(r"(?:改成|换成|还是)?按(?:日|天|周|月|季度|年)", answer)
        ):
            pending.primary_intent = PrimaryIntent.METRIC_QUERY
            pending.operators = list(parsed.operators)
            pending.assumptions = [
                value for value in pending.assumptions
                if not value.startswith("DEFAULT_TIME_GRANULARITY=")
            ]
            pending.assumptions.extend(
                value for value in parsed.assumptions
                if value.startswith("DEFAULT_TIME_GRANULARITY=")
            )
        pending.missing_slots = self.required_missing_slots(pending)
        represents_structured_change = bool(
            parsed.metrics
            or parsed.time_range
            or parsed.entity
            or parsed.fields
            or parsed.dimensions
            or parsed.comparison_type
            or parsed.ranking_limit
            or parsed.forecast_horizon_periods
        )
        confirmation = (
            safe_semantic_confirmation(answer)
            if "semantic_ambiguity" in prior_missing
            and not represents_structured_change
            and parsed.conversation_control == ConversationControl.NEW_REQUEST
            else None
        )
        if (
            "semantic_ambiguity" in prior_missing
            and not represents_structured_change
            and confirmation is None
            and "semantic_ambiguity" not in pending.missing_slots
        ):
            pending.missing_slots.append("semantic_ambiguity")
        pending.rewritten_question = render_execution_question(
            pending, confirmation=confirmation
        )
        return pending

    def required_missing_slots(self, request: CanonicalAnalysisRequest) -> list[str]:
        intent = request.primary_intent
        missing: list[str] = []
        normalized_question = re.sub(r"\s+", "", request.original_question or "")
        recommendation_like = self._is_ranked_partner_recommendation(normalized_question)
        data_intents = {
            PrimaryIntent.METRIC_QUERY,
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
            PrimaryIntent.COMPOSITION_ANALYSIS,
            PrimaryIntent.ANOMALY_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            PrimaryIntent.FORECAST_ANALYSIS,
            PrimaryIntent.METRIC_DEFINITION,
            PrimaryIntent.DATA_LINEAGE,
            PrimaryIntent.DATA_QUALITY,
        }
        quality_object_provided = (
            intent == PrimaryIntent.DATA_QUALITY
            and (
                bool(request.entity or request.fields or request.dimensions)
                or any(
                    term in (request.original_question or "")
                    for term in ("数据", "订单", "明细", "报表", "数据源", "表", "字段")
                )
            )
        )
        if intent in data_intents and not request.metrics and not quality_object_provided:
            missing.append("metric")
        if intent in data_intents - {
            PrimaryIntent.METRIC_DEFINITION,
            PrimaryIntent.DATA_LINEAGE,
            PrimaryIntent.FORECAST_ANALYSIS,
        } and not recommendation_like:
            if (
                not request.time_range
                and not self._uses_all_time_scope(request)
                and (
                    self._has_activity_time_filter(normalized_question)
                    or not self._uses_precomputed_metric_window(request)
                )
            ):
                missing.append("time_range")
        if intent == PrimaryIntent.DETAIL_QUERY:
            if not request.entity:
                missing.append("entity")
            # Master-data and relationship lists are valid without a time
            # window.  Require one only when the requested rows explicitly
            # depend on transactions or historical activity.
            detail_text = re.sub(r"\s+", "", request.original_question or "")
            time_dependent_detail = any(
                marker in detail_text
                for marker in (
                    "销售", "订单", "退款", "交易", "近一年", "近一月",
                    "最近", "历史", "期间", "活跃", "覆盖",
                    "合作经销商数据", "合作供应商数据",
                )
            )
            if (
                time_dependent_detail
                and not request.time_range
                and not self._uses_all_time_scope(request)
            ):
                missing.append("time_range")
            if not request.fields:
                missing.append("fields")
        if intent == PrimaryIntent.COMPARISON_ANALYSIS and not request.comparison_type:
            missing.append("comparison_type")
        if (
            intent == PrimaryIntent.COMPARISON_ANALYSIS
            and request.comparison_type == "对象间比较"
            and self._generic_partner_count_needs_names(normalized_question)
            and not self._comparison_object_filter_present(request.filters)
        ):
            missing.append("comparison_objects")
        if intent == PrimaryIntent.COMPOSITION_ANALYSIS and not request.dimensions:
            missing.append("dimension")
        if AnalysisOperator.TOP_N in request.operators:
            if not request.dimensions:
                missing.append("dimension")
            if any(
                token in (request.original_question or "")
                for token in ("某商品", "某个商品", "指定商品")
            ):
                missing.append("product")
        if (
            intent == PrimaryIntent.REPORT_GENERATION
            and not request.time_range
            and not self._uses_all_time_scope(request)
        ):
            missing.append("time_range")
        if intent == PrimaryIntent.FORECAST_ANALYSIS:
            if request.forecast_horizon_periods is None or request.forecast_granularity is None:
                missing.append("forecast_horizon")
            if not request.forecast_history_provided:
                missing.append("forecast_history_range")
        return missing

    @staticmethod
    def _uses_all_time_scope(request: CanonicalAnalysisRequest) -> bool:
        """Return whether the user explicitly requested a lifetime aggregate.

        A measure such as ``销售额`` still needs a period.  Cumulative and total
        measures instead mean all available history up to the source watermark,
        so asking for an arbitrary period would change the requested business
        meaning rather than clarify it.
        """
        if any(
            assumption in {"TIME_SCOPE=ALL_TIME", "TIME_SCOPE=ALL_AVAILABLE_HISTORY"}
            for assumption in request.assumptions
        ):
            return True
        text = re.sub(
            r"\s+", "", request.original_question or request.rewritten_question or ""
        )
        if re.search(
            r"历史累计|历年累计|累计|全部历史|全量历史|全历史|"
            r"自有记录以来|从有记录以来|成立以来|截至目前|截至当前|截至现在|至今|"
            r"总计|合计",
            text,
        ):
            return True
        # “总额/总数” describes aggregation, not an explicit lifetime period.
        # Only user wording or a recorded semantic assumption may opt into
        # all-history scope.
        return False

    @staticmethod
    def _forecast_target(text: str) -> tuple[int | None, str | None]:
        unit_map = {
            "天": "day", "日": "day", "周": "week", "星期": "week",
            "个月": "month", "月": "month", "季度": "quarter", "季": "quarter",
            "年": "year",
        }
        if match := re.search(
            r"(?:未来|接下来|往后)(\d{1,2}|[一二两三四五六七八九十]{1,3})"
            r"(天|日|周|星期|个月|月|季度|季|年)", text
        ):
            count = RuleBasedIntentClassifier._chinese_window_count(match.group(1))
            if count is not None:
                return count, unit_map[match.group(2)]
        fixed = (
            (("下个月", "下月"), "month"),
            (("下周", "下个星期"), "week"),
            (("明天", "次日"), "day"),
            (("下季度", "下个季度"), "quarter"),
            (("明年", "下一年"), "year"),
        )
        for markers, granularity in fixed:
            if any(marker in text for marker in markers):
                return 1, granularity
        # A concrete target period is already a complete one-period horizon.
        # Do not ask "how many periods" again for questions such as
        # "基于过去12个月，预测2026年8月销售额".
        forecast_tail = re.search(
            r"(?:预测|预计|预估|推算|估算)(?P<tail>.{0,30})", text
        )
        if forecast_tail:
            tail = forecast_tail.group("tail")
            if re.search(r"20\d{2}(?:年|[-/.])\d{1,2}(?:月份?)?", tail):
                return 1, "month"
            if re.search(r"20\d{2}(?:年)?[Qq][1-4]|20\d{2}年第[一二三四1234]季度", tail):
                return 1, "quarter"
            if re.search(r"20\d{2}年", tail):
                return 1, "year"
        return None, None

    @staticmethod
    def _ranking_limit(text: str) -> int | None:
        prefix = r"(?:前|后|top\s*|最(?:多|少|高|低|大|小)(?:的)?)"
        match = re.search(prefix + r"\s*(\d{1,3})\s*(?:名|个|家)?", text, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            return value if 1 <= value <= 100 else None
        match = re.search(prefix + r"\s*([一二三四五六七八九十]{1,3})(?:名|个|家|的)?", text, re.IGNORECASE)
        if not match:
            return None
        numeral = match.group(1)
        digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9}
        if numeral == "十":
            value = 10
        elif "十" in numeral:
            tens, ones = numeral.split("十", maxsplit=1)
            value = digits.get(tens, 1) * 10 + digits.get(ones, 0)
        else:
            value = digits.get(numeral, 0)
        return value if 1 <= value <= 100 else None

    @staticmethod
    def _forecast_history_provided(text: str) -> bool:
        return bool(
            re.search(
                r"(?:基于|使用|参考)?(?:过去|近|最近|历史|此前|前)(?:\d{1,3}|一|二|三|六|十二)?(?:个)?(?:天|日|周|星期|月|季度|季|年|周期|数据)",
                text,
            )
        )

    @classmethod
    def _uses_precomputed_metric_window(
        cls, request: CanonicalAnalysisRequest
    ) -> bool:
        if request.time_range is not None:
            return False
        metric_names = {
            metric.canonical_name or metric.input for metric in request.metrics
        }
        if metric_names and metric_names.issubset(
            cls._period_independent_snapshot_metrics
        ):
            return True
        if not any(
            value.startswith(cls._metric_window_assumption_prefix)
            for value in request.assumptions
        ):
            return False
        registered_windows = {
            definition[0] for definition in cls._window_metric_definitions()
        }
        allowed = registered_windows | cls._period_independent_snapshot_metrics
        return bool(metric_names & registered_windows) and metric_names.issubset(allowed)

    @staticmethod
    def _has_activity_time_filter(text: str) -> bool:
        """Return whether row eligibility depends on dated business activity.

        Snapshot profile metrics can be read without a reporting period, but a
        qualifier such as “正在销售某产品” asks which partners had transactions
        in a period.  The metric window must never silently supply that separate
        filter window.
        """
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"正在销售|销售过|曾经销售|(?:有|存在)(?:过)?销售记录|"
            r"(?:期间|范围内).{0,12}(?:存在|有|发生).{0,12}销售|"
            r"发生过销售|"
            r"活跃(?:经销商|供应商|客户|门店)",
            compact,
        ))

    @staticmethod
    def _uses_transaction_activity_definition(text: str) -> bool:
        """Distinguish fact-row activity from a stored partner status."""
        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"正在销售|销售过|曾经销售|(?:有|存在)(?:过)?销售记录|"
            r"(?:期间|范围内).{0,12}(?:存在|有|发生).{0,12}销售|"
            r"发生过销售|"
            r"活跃(?:经销商|供应商|客户|门店).{0,20}(?:在)?销售",
            compact,
        ))

    @staticmethod
    def _uses_cooperation_activity_definition(text: str) -> bool:
        """Return whether cooperation is scoped to a product relationship.

        Product cooperation lists and their distinct-count metrics must share
        the same positive-sales fact-row definition.  Otherwise a static
        relationship table can return a different list size from the governed
        ``已合作…数`` metric.
        """

        compact = re.sub(r"\s+", "", text)
        return bool(re.search(
            r"(?:产品|商品|导管|耗材|口罩|透析器).{0,40}"
            r"(?:已)?合作(?:的)?(?:医院|经销商|供应商|客户|门店)"
            r"(?:数量|数|名单|清单|列表)?",
            compact,
        ))

    @staticmethod
    def _operators(intent: PrimaryIntent, text: str) -> list[AnalysisOperator]:
        mapping = {
            PrimaryIntent.METRIC_QUERY: [AnalysisOperator.AGGREGATE],
            PrimaryIntent.DETAIL_QUERY: [AnalysisOperator.FILTER, AnalysisOperator.RENDER_TABLE],
            PrimaryIntent.TREND_ANALYSIS: [AnalysisOperator.TIME_BUCKET],
            PrimaryIntent.COMPARISON_ANALYSIS: [AnalysisOperator.COMPARE],
            PrimaryIntent.COMPOSITION_ANALYSIS: [AnalysisOperator.GROUP_BY, AnalysisOperator.RATIO],
            PrimaryIntent.ANOMALY_ANALYSIS: [AnalysisOperator.ANOMALY_DETECT],
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: [AnalysisOperator.DECOMPOSE],
            PrimaryIntent.FORECAST_ANALYSIS: [AnalysisOperator.FORECAST],
            PrimaryIntent.METRIC_DEFINITION: [AnalysisOperator.EXPLAIN],
        }
        result = list(mapping.get(intent, []))
        if RuleBasedIntentClassifier._ranking_limit(text) is not None:
            compact = re.sub(r"\s+", "", text)
            result.append(AnalysisOperator.TOP_N)
            if re.search(r"(?:后\s*(?:\d|[一二三四五六七八九十])|最少(?:的)?)", compact):
                result.append(AnalysisOperator.BOTTOM_N)
        if re.search(r"排序|排名|升序|降序|从高到低|从低到高|由高到低|由低到高", text):
            result.append(AnalysisOperator.SORT)
        return result

    @staticmethod
    def _conversation_control(text: str) -> ConversationControl:
        compact = re.sub(r"\s+", "", text).strip("，,。.!！?？;；")
        if re.search(
            r"(?:算了|不想问|不问了|不用了|换个问题|新问题|新任务)"
            r".{0,40}(?:查询|统计|分析|解释|生成|列出|预测)",
            compact,
        ):
            return ConversationControl.NEW_REQUEST
        if re.search(
            r"(?:取消|去掉|移除|删掉|删除|不要).{0,30}"
            r"(?:指标|维度|分组|筛选|过滤|条件)",
            text,
        ) or any(term in text for term in ("不看", "不查", "不按", "别看", "不要")):
            return ConversationControl.CORRECTION
        if compact == "取消" or any(
            term in text for term in ("取消查询", "取消任务", "不用了", "停止", "算了")
        ):
            return ConversationControl.CANCEL
        if any(term in text for term in ("改成", "更正", "不是", "我说错了")):
            return ConversationControl.CORRECTION
        if any(term in text for term in (
            "继续", "再看", "再查", "再查询", "那查询", "接着", "刚才", "同样", "换成"
        )):
            return ConversationControl.FOLLOW_UP
        if (
            re.search(
                r"(?:^|[，,])(?:再)?按[^，,。]{1,30}(?:拆分|分组|排序|展示|显示|查看|统计)"
                r"|(?:展示|显示|只看|保留)(?:前|后)?(?:\d+|[一二三四五六七八九十]{1,3})(?:名|个|条)?"
                r"|^(?:前|后)(?:\d+|[一二三四五六七八九十]{1,3})(?:名|个|条)?$",
                compact,
            )
            and not RuleBasedIntentClassifier._EXPLICIT_FOLLOWUP_METRIC.search(compact)
        ):
            return ConversationControl.FOLLOW_UP
        if text.startswith(("那", "那么")) and (
            text.endswith("呢")
            or len(text) <= 12
            or RuleBasedIntentClassifier._time_range(text) is not None
            or RuleBasedIntentClassifier._EXPLICIT_FOLLOWUP_METRIC.search(compact)
        ):
            return ConversationControl.FOLLOW_UP
        return ConversationControl.NEW_REQUEST

    @staticmethod
    def _time_range(text: str) -> TimeRange | None:
        text = re.sub(r"\s+", "", text)
        text = re.sub(
            r"(?<!\d)(?P<year>\d{2})年(?=\d{1,2}月)",
            lambda match: f"20{match.group('year')}年",
            text,
        )
        # Production uses the business timezone instead of the host's local
        # timezone.  The compatibility branch keeps the existing test seam
        # where ``classifier.date`` is replaced with a frozen date class.
        today = (
            date.today()
            if date is not _NATIVE_DATE_CLASS
            else datetime.now(_SHANGHAI_TZ).date()
        )

        def next_month_start(year: int, month: int) -> date:
            return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        def relative_year(value: str | None) -> int:
            if value in {"去年"}:
                return today.year - 1
            if value in {"明年"}:
                return today.year + 1
            return today.year

        # These expressions have an open-ended boundary and must be handled
        # before ordinary single-date/month parsing.  Otherwise a phrase such
        # as "2026年7月1日起至今" would silently degrade to one day.
        start_to_now = _START_TO_NOW_PATTERN.search(text)
        if start_to_now:
            try:
                start = date(
                    int(start_to_now.group("year")),
                    int(start_to_now.group("month")),
                    int(start_to_now.group("day")),
                )
            except ValueError:
                return None
            if start > today:
                return None
            return TimeRange(start=start, end_exclusive=today + timedelta(days=1))

        period_to_date = re.search(
            r"(?P<period>本月|这个月|今年|本年)"
            r"(?P<cutoff>至今|截至今天|截止今天|截至今日|截止今日|截至昨天|截止昨天|截至昨日|截止昨日)",
            text,
        )
        if period_to_date:
            period = period_to_date.group("period")
            start = today.replace(day=1) if period in {"本月", "这个月"} else date(today.year, 1, 1)
            cutoff = period_to_date.group("cutoff")
            end = today if cutoff.endswith(("昨天", "昨日")) else today + timedelta(days=1)
            if end <= start:
                return None
            return TimeRange(start=start, end_exclusive=end)

        # A bare “截至今天” is a common answer to a time clarification. With
        # no earlier boundary supplied, normalize it to the current
        # year-to-date window instead of repeatedly asking for time.
        if re.search(r"(?:截至今天|截止今天|截至今日|截止今日)", text):
            return TimeRange(
                start=date(today.year, 1, 1),
                end_exclusive=today + timedelta(days=1),
            )

        # A known-but-unimplemented temporal modifier is safer to reject than
        # to partially parse.  Silent partial parsing changes the user's time
        # window and can produce a plausible but materially wrong answer.
        if re.search(
            r"(?:截至|截止|至今|到现在|至现在|起(?:到|至)|以来|以前|以后|之前|之后)",
            text,
        ):
            return None

        recent_days = re.search(
            r"(?:最近|近|过去)(?P<days>\d+)个?(?:自然)?(?:天|日)(?!\d)", text
        )
        if recent_days:
            days = int(recent_days.group("days"))
            # Do not silently clamp a wider request to 366 days.  The caller
            # can clarify/split a range that exceeds the supported window.
            if not 1 <= days <= 366:
                return None
            return TimeRange(
                start=today - timedelta(days=days - 1),
                end_exclusive=today + timedelta(days=1),
            )

        # Natural-language rolling windows are common in real questions but
        # cannot be interpreted by the numeric-day rule above.  Use an
        # inclusive window ending today, consistent with "近7日". Calendar
        # months preserve the day-of-month where possible instead of silently
        # treating every month as 30 days.
        count_words = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十二": 12,
        }

        def parse_count(raw: str) -> int | None:
            if raw.isdigit():
                return int(raw)
            return count_words.get(raw)

        recent_weeks = re.search(
            r"(?:最近|近|过去)(?P<count>\d{1,2}|一|二|两|三|四|五|六|七|八|九|十|十二)个?(?:自然)?(?:周|星期)(?![\d期])",
            text,
        )
        if recent_weeks:
            weeks = parse_count(recent_weeks.group("count"))
            if weeks is None or not 1 <= weeks <= 52:
                return None
            return TimeRange(
                start=today - timedelta(days=weeks * 7 - 1),
                end_exclusive=today + timedelta(days=1),
            )

        recent_months = re.search(
            r"(?:最近|近|过去)(?P<count>\d{1,2}|一|二|两|三|四|五|六|七|八|九|十|十二)个?月(?!份)",
            text,
        )
        if recent_months or re.search(r"(?:最近|近|过去)半年", text):
            months = 6 if recent_months is None else parse_count(recent_months.group("count"))
            if months is None or not 1 <= months <= 36:
                return None
            absolute_month = today.year * 12 + today.month - 1 - months
            start_year, month_index = divmod(absolute_month, 12)
            start_month = month_index + 1
            start_day = min(today.day, monthrange(start_year, start_month)[1])
            return TimeRange(
                start=date(start_year, start_month, start_day),
                end_exclusive=today + timedelta(days=1),
            )

        recent_years = re.search(
            r"(?:最近|近|过去)(?P<count>\d{1,2}|一|二|两|三|四|五|六|七|八|九|十)年",
            text,
        )
        if recent_years:
            years = parse_count(recent_years.group("count"))
            if years is None or not 1 <= years <= 10:
                return None
            start_year = today.year - years
            start_day = min(today.day, monthrange(start_year, today.month)[1])
            return TimeRange(
                start=date(start_year, today.month, start_day),
                end_exclusive=today + timedelta(days=1),
            )

        for pattern in _EXPLICIT_DATE_RANGE_PATTERNS:
            explicit = pattern.search(text)
            if not explicit:
                continue
            try:
                start_year = int(explicit.group("start_year"))
                start_month = int(explicit.group("start_month"))
                start = date(start_year, start_month, int(explicit.group("start_day")))
                end_year_text = explicit.groupdict().get("end_year")
                end_month_text = explicit.groupdict().get("end_month")
                end = date(
                    int(end_year_text) if end_year_text is not None else start_year,
                    int(end_month_text) if end_month_text is not None else start_month,
                    int(explicit.group("end_day")),
                )
            except ValueError:
                return None
            if end < start:
                return None
            return TimeRange(start=start, end_exclusive=end + timedelta(days=1))

        for pattern in _EXPLICIT_MONTH_RANGE_PATTERNS:
            explicit_month_range = pattern.search(text)
            if not explicit_month_range:
                continue
            try:
                start_year = int(explicit_month_range.group("start_year"))
                start_month = int(explicit_month_range.group("start_month"))
                end_year_text = explicit_month_range.groupdict().get("end_year")
                end_year = int(end_year_text) if end_year_text is not None else start_year
                end_month = int(explicit_month_range.group("end_month"))
                start = date(start_year, start_month, 1)
                end_start = date(end_year, end_month, 1)
                end = next_month_start(end_start.year, end_start.month)
            except ValueError:
                return None
            if end_start < start:
                return None
            return TimeRange(start=start, end_exclusive=end)

        explicit_quarter = _EXPLICIT_QUARTER_PATTERN.search(text)
        if explicit_quarter:
            year = int(explicit_quarter.group("year"))
            quarter = int(explicit_quarter.group("quarter"))
            month = (quarter - 1) * 3 + 1
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if quarter == 4 else date(year, month + 3, 1)
            return TimeRange(start=start, end_exclusive=end)

        named_quarter = _NAMED_QUARTER_PATTERN.search(text)
        if named_quarter:
            quarter_values = {"一": 1, "二": 2, "三": 3, "四": 4}
            raw_quarter = named_quarter.group("quarter")
            quarter = quarter_values.get(raw_quarter, int(raw_quarter) if raw_quarter.isdigit() else 0)
            year_text = named_quarter.groupdict().get("year")
            year = int(year_text) if year_text else relative_year(named_quarter.groupdict().get("relative"))
            month = (quarter - 1) * 3 + 1
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if quarter == 4 else date(year, month + 3, 1)
            return TimeRange(start=start, end_exclusive=end)

        half_year = _HALF_YEAR_PATTERN.search(text)
        if half_year:
            year_text = half_year.groupdict().get("year")
            year = int(year_text) if year_text else relative_year(half_year.groupdict().get("relative"))
            if half_year.group("half") == "上":
                return TimeRange(start=date(year, 1, 1), end_exclusive=date(year, 7, 1))
            return TimeRange(start=date(year, 7, 1), end_exclusive=date(year + 1, 1, 1))

        explicit_year_range = _EXPLICIT_YEAR_RANGE_PATTERN.search(text)
        if explicit_year_range:
            start_year = int(explicit_year_range.group("start_year"))
            end_year = int(explicit_year_range.group("end_year"))
            if end_year < start_year or end_year - start_year > 20:
                return None
            return TimeRange(
                start=date(start_year, 1, 1),
                end_exclusive=date(end_year + 1, 1, 1),
            )

        single_day = _SINGLE_EXPLICIT_DATE_PATTERN.search(text)
        if single_day:
            # If a range marker follows the date but the endpoint did not match
            # one of the supported forms above, do not silently drop it.
            remaining = text[single_day.end() :]
            preceding = text[: single_day.start()]
            if re.match(_RANGE_SEPARATOR, remaining) or re.search(
                rf"{_WORD_RANGE_SEPARATOR}$", preceding
            ):
                return None
            try:
                day = date(
                    int(single_day.group("year")),
                    int(single_day.group("month")),
                    int(single_day.group("day")),
                )
            except ValueError:
                return None
            return TimeRange(start=day, end_exclusive=day + timedelta(days=1))

        explicit_month = _EXPLICIT_YEAR_MONTH_PATTERN.search(text)
        if explicit_month:
            remaining = text[explicit_month.end() :]
            preceding = text[: explicit_month.start()]
            if re.match(_RANGE_SEPARATOR, remaining) or re.search(
                rf"{_WORD_RANGE_SEPARATOR}$", preceding
            ):
                return None
            try:
                year = int(explicit_month.group("year"))
                month = int(explicit_month.group("month_cn") or explicit_month.group("month_num"))
                start = date(year, month, 1)
                end = next_month_start(year, month)
            except ValueError:
                return None
            return TimeRange(start=start, end_exclusive=end)

        explicit_year = _EXPLICIT_YEAR_PATTERN.search(text)
        if explicit_year:
            year = int(explicit_year.group("year"))
            return TimeRange(
                start=date(year, 1, 1),
                end_exclusive=date(year + 1, 1, 1),
            )

        bare_month = _BARE_MONTH_PATTERN.search(text)
        if bare_month:
            try:
                year = relative_year(bare_month.groupdict().get("relative"))
                month = int(bare_month.group("month"))
                start = date(year, month, 1)
                end = next_month_start(year, month)
            except ValueError:
                return None
            return TimeRange(start=start, end_exclusive=end)

        if "本周" in text or "这周" in text or "这个星期" in text:
            start = today - timedelta(days=today.weekday())
            return TimeRange(start=start, end_exclusive=start + timedelta(days=7))
        if "上周" in text or "上个星期" in text:
            end = today - timedelta(days=today.weekday())
            return TimeRange(start=end - timedelta(days=7), end_exclusive=end)
        if "本月" in text or "这个月" in text:
            start = today.replace(day=1)
            end = next_month_start(start.year, start.month)
            return TimeRange(start=start, end_exclusive=end)
        if "上月" in text or "上个月" in text:
            end = today.replace(day=1)
            start = (end - timedelta(days=1)).replace(day=1)
            return TimeRange(start=start, end_exclusive=end)
        if "今天" in text:
            return TimeRange(start=today, end_exclusive=today + timedelta(days=1))
        if "昨天" in text:
            day = today - timedelta(days=1)
            return TimeRange(start=day, end_exclusive=today)
        if "今年" in text or "本年" in text:
            return TimeRange(start=date(today.year, 1, 1), end_exclusive=date(today.year + 1, 1, 1))
        if "去年" in text:
            return TimeRange(start=date(today.year - 1, 1, 1), end_exclusive=date(today.year, 1, 1))
        if "本季度" in text or "这个季度" in text:
            month = ((today.month - 1) // 3) * 3 + 1
            start = date(today.year, month, 1)
            end = date(today.year + (month == 10), 1 if month == 10 else month + 3, 1)
            return TimeRange(start=start, end_exclusive=end)
        if "上季度" in text or "上个季度" in text:
            current_month = ((today.month - 1) // 3) * 3 + 1
            end = date(today.year, current_month, 1)
            previous = end - timedelta(days=1)
            start_month = ((previous.month - 1) // 3) * 3 + 1
            return TimeRange(start=date(previous.year, start_month, 1), end_exclusive=end)
        if "下月" in text or "下个月" in text:
            start = next_month_start(today.year, today.month)
            end = next_month_start(start.year, start.month)
            return TimeRange(start=start, end_exclusive=end)
        return None
