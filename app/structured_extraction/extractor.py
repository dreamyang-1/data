"""结构化参数提取的出口处理。

模型返回的 extraction 在这里做形态归一、明细查询字段归位、目录单位换算，
再转成下游 mentions 通道使用的“词（角色）”参数。任务拆分与模型调用仍在
planning 层，本模块只负责参数本身的清洗与归一。
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

from app.domain.models import PrimaryIntent

logger = logging.getLogger(__name__)

# 结构化提取JSON的固定键，与解析器规范一一对应。
EXTRACTION_INTENT_KEY = "意图"
_EXTRACTION_LIST_KEYS = ("业务域", "实体", "指标", "维度", "展示字段", "过滤条件", "排序")
_EXTRACTION_LIST_MAX = 20
_EXTRACTION_TEXT_MAX = 200

# 中文意图枚举到系统意图枚举的映射，模型未给出intent时兜底。
CHINESE_INTENT_TO_PRIMARY = {
    "统计查询": PrimaryIntent.METRIC_QUERY,
    "指标查询": PrimaryIntent.METRIC_QUERY,
    "趋势分析": PrimaryIntent.TREND_ANALYSIS,
    "对比分析": PrimaryIntent.COMPARISON_ANALYSIS,
    "占比分析": PrimaryIntent.COMPOSITION_ANALYSIS,
    "排名分析": PrimaryIntent.METRIC_QUERY,
    "明细查询": PrimaryIntent.DETAIL_QUERY,
    "异常分析": PrimaryIntent.ANOMALY_ANALYSIS,
    "归因分析": PrimaryIntent.ROOT_CAUSE_ANALYSIS,
    "预测分析": PrimaryIntent.FORECAST_ANALYSIS,
}
CHINESE_INTENT_LABELS = frozenset(CHINESE_INTENT_TO_PRIMARY)

# 口径/血缘解释意图保留完整提取，只是不走查询执行链路。
NO_DATA_INTENTS = {
    PrimaryIntent.CHAT,
    PrimaryIntent.CAPABILITY_HELP,
    PrimaryIntent.OUT_OF_SCOPE,
}

# 结构化提取会把问题原文片段幻觉成筛选值（Q60 实测：商品名称 EQ
# "2025年销售额占比前3的产品线下各"——原文跨逗号拼接的叙述性片段）。
# 真实业务值（商品/经销商/医院/省份名等）不会同时携带时间前缀、排名
# 短语、指标描述、群扫指代等多重叙述标记；命中特征的筛选值在进入
# 合同、ASL 与 mentions 通道前剥离，不猜标准值，也不依赖向量校验拦停。
_PROSE_VALUE_MARKERS = (
    re.compile(r'\d{4}\s*年'),
    re.compile(r'前\d+'),
    re.compile(r'占比|排名|销量|销售额|金额|总数|数量|总计|合计|平均|最高|最低'),
    re.compile(r'(?:各|每)(?:条|个|笔|项|类|产品|商品|订单|经销商|医院|省份|客户|业务)'),
    re.compile(r'上述|前序|依赖任务|任务\d'),
    re.compile(r'[的与和及或下各]$'),
)


def is_prose_placeholder_value(value: Any) -> bool:
    text = str(value).strip()
    if len(text) < 6:
        return False
    return sum(bool(pattern.search(text)) for pattern in _PROSE_VALUE_MARKERS) >= 2


def strip_prose_placeholder_filters(structured: dict[str, Any] | None) -> None:
    """剥离抽取阶段幻觉出的散文占位筛选值，只删条件不猜值。

    抽取模型偶发把任务问题的叙述片段当成等值过滤值写进过滤条件——
    该值永远无法通过向量标准值绑定，只会把整个任务拦停在澄清/失败。
    剥离等价于按“任务自身无该条件”执行（原问题本就没有这个条件），
    与空列表占位剥离（EMPTY_FILTER_VALUE_DROPPED）同一哲学。模板引用
    （${task_N.字段}）与真实业务值不受影响。
    """
    if not isinstance(structured, dict):
        return
    conditions = structured.get('过滤条件')
    if not isinstance(conditions, list):
        return
    kept = []
    for item in conditions:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        raw = item.get('value')
        if isinstance(raw, list):
            values = [value for value in raw
                      if not (isinstance(value, str) and is_prose_placeholder_value(value))]
            if len(values) != len(raw):
                logger.warning('prose placeholder filter value dropped: field=%r value=%r',
                               item.get('field'), raw)
            if values:
                kept.append(dict(item, value=values))
            continue
        if isinstance(raw, str) and is_prose_placeholder_value(raw):
            logger.warning('prose placeholder filter dropped: field=%r value=%r',
                           item.get('field'), raw)
            continue
        kept.append(item)
    structured['过滤条件'] = kept


def clean_extraction_text(value: Any) -> str:
    text = str(value).strip()
    return text[:_EXTRACTION_TEXT_MAX]


def clean_extraction_item(value: Any) -> Any:
    """列表元素只保留字符串和对象两种形态，超长截断。"""
    if isinstance(value, str):
        return clean_extraction_text(value)
    if isinstance(value, dict):
        return {
            clean_extraction_text(key): clean_extraction_text(item)
            if isinstance(item, (str, int, float)) else item
            for key, item in list(value.items())[:12]
        }
    if isinstance(value, (int, float)):
        return value
    return None


def database_query_choice(extraction: dict[str, Any] | None) -> bool | None:
    if not isinstance(extraction, dict):
        return None
    value = extraction.get("是否查询数据库")
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", "", value).casefold()
    if normalized in {"是", "yes", "true"} or normalized.startswith(("是（", "是(")):
        return True
    if normalized in {"否", "no", "false"} or normalized.startswith(("否（", "否(")):
        return False
    return None


def extraction_to_parameters(extraction: dict[str, Any] | None) -> list[str]:
    """把结构化提取JSON转成“词（角色）”参数，兼容既有mentions通道。

    过滤条件值以字段名作为角色（如 百特（商品品牌）），实体、指标、维度、
    时间范围按固定角色输出，顺序与展示优先级保持一致。
    """
    if not isinstance(extraction, dict):
        return []
    parameters: list[str] = []

    def _add(text: Any, role: str) -> None:
        if text is None:
            return
        label = str(text).strip()
        if not label:
            return
        rendered = f"{label}（{role}）"
        if rendered not in parameters:
            parameters.append(rendered)

    for value in extraction.get("实体") or []:
        if isinstance(value, str):
            _add(value, "实体")
    for value in extraction.get("指标") or []:
        if isinstance(value, dict):
            _add(value.get("name"), "指标")
        elif isinstance(value, str):
            _add(value, "指标")
    for value in extraction.get("维度") or []:
        if isinstance(value, str):
            _add(value, "维度")
    for item in extraction.get("过滤条件") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        raw_value = item.get("value")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            if isinstance(value, (str, int, float)):
                _add(value, field or "筛选值")
    time_granularity = extraction.get("时间粒度")
    if isinstance(time_granularity, dict):
        _add(time_granularity.get("time_range"), "时间范围")
    return parameters[:10]


def normalize_metric_name(
    name: str, registered: frozenset[str], aliases: dict[str, str]
) -> str:
    """指标名归一到规范登记原名：登记名 > 别名 > 唯一包含登记名的派生说法。"""
    if name in registered:
        return name
    if name in aliases:
        return aliases[name]
    candidates = [item for item in registered if len(item) >= 2 and item in name]
    if candidates:
        return max(candidates, key=len)
    return name


def normalize_extraction_metrics(
    extraction: Any,
    registered: frozenset[str],
    aliases: dict[str, str],
    *,
    report_off_catalog: bool,
) -> list[str]:
    """extraction 的指标名原地归一为规范登记原名，返回归一后仍目录外的名字。

    排序字段同样归一（增长率、占比等派生说法归到登记指标；维度名不在
    指标目录时原样保留），不参与目录外上报。
    report_off_catalog=False（“不再查询数据库”的计算任务）只归一不上报：
    计算任务的指标名按规则可以带用户原文叫法。
    """
    if not isinstance(extraction, dict) or not registered:
        return []
    sort_items = extraction.get("排序")
    if isinstance(sort_items, list):
        for sort_item in sort_items:
            if not isinstance(sort_item, dict):
                continue
            field = sort_item.get("field")
            if isinstance(field, str) and field.strip():
                sort_item["field"] = normalize_metric_name(
                    field.strip(), registered, aliases
                )
    metrics = extraction.get("指标")
    if not isinstance(metrics, list):
        return []
    off_catalog: list[str] = []
    for index, item in enumerate(metrics):
        if isinstance(item, dict):
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            normalized = normalize_metric_name(name.strip(), registered, aliases)
            item["name"] = normalized
            if report_off_catalog and normalized not in registered:
                off_catalog.append(normalized)
            metrics[index] = item
        elif isinstance(item, str) and item.strip():
            normalized = normalize_metric_name(item.strip(), registered, aliases)
            metrics[index] = normalized
            if report_off_catalog and normalized not in registered:
                off_catalog.append(normalized)
    return off_catalog


def sanitize_extraction(
    value: dict[str, Any] | None,
    *,
    intent: PrimaryIntent | None,
    metric_catalog: tuple[frozenset[str], dict[str, str]] | None = None,
    metric_units: dict[str, str] | None = None,
    required_metrics: tuple[str, ...] = (),
    preserve_query_fields: bool = False,
) -> dict[str, Any] | None:
    """清洗模型输出的结构化提取：纯闲聊类意图强制为空，键与取值做形态归一。

    口径/血缘解释任务保留完整提取：参数照常进mentions通道供下游解析，
    展示层单独决定是否输出，指标不存在时回复“未定义”仍依赖这些参数。
    """
    if not isinstance(value, dict):
        return None
    if intent is not None and intent in NO_DATA_INTENTS:
        return None
    structured: dict[str, Any] = {}
    intent_label = clean_extraction_text(value.get(EXTRACTION_INTENT_KEY) or "")
    if intent == PrimaryIntent.METRIC_DEFINITION:
        # 口径解释不在六类查询枚举里，意图键固定与任务意图显示一致
        structured[EXTRACTION_INTENT_KEY] = "解释口径"
    elif intent == PrimaryIntent.DATA_LINEAGE:
        structured[EXTRACTION_INTENT_KEY] = "数据血缘"
    else:
        structured[EXTRACTION_INTENT_KEY] = (
            intent_label if intent_label in CHINESE_INTENT_LABELS else None
        )
    for key in _EXTRACTION_LIST_KEYS:
        raw = value.get(key)
        items: list[Any] = []
        if isinstance(raw, list):
            for item in raw[:_EXTRACTION_LIST_MAX]:
                cleaned = clean_extraction_item(item)
                if cleaned is not None and cleaned != "":
                    items.append(cleaned)
        elif isinstance(raw, (str, dict)):
            cleaned = clean_extraction_item(raw)
            if cleaned is not None and cleaned != "":
                items.append(cleaned)
        structured[key] = items
    if not preserve_query_fields and (intent_label == "明细查询" or (
        intent == PrimaryIntent.DETAIL_QUERY
        and intent_label not in CHINESE_INTENT_LABELS
    )):
        # 明细查询只按展示字段出列：模型把返回列误提进指标/维度时，
        # 迁入展示字段后再清空，避免下游丢列或误走聚合口径。
        # 例外：排序、极值等条件依赖的注册指标保留在指标通道，供
        # ASL按登记口径计算（如“均价最低”的订单均价）；未登记的
        # 提法仍迁展示字段。维度无计算语义，照旧清空。
        # 中文意图键与英文枚举冲突时以中文键为准，与下游意图回填规则一致。
        display = structured["展示字段"]
        known_fields = {
            str(item.get("field") or item.get("name") or "").strip()
            if isinstance(item, dict) else str(item).strip()
            for item in display
        }
        registered = metric_catalog[0] if metric_catalog else frozenset()
        aliases = metric_catalog[1] if metric_catalog else {}
        # 豁免收紧：只有排序、过滤条件实际引用到的注册指标才保留在指标通道，
        # 模型顺手多提的指标（如“含税单价最低”带上含税销售总额）在这里丢弃，
        # 未被引用的注册指标也不迁展示字段，保持指标[]与平台口径一致
        referenced_names: set[str] = set(required_metrics)
        for ref_items in (structured["排序"], structured["过滤条件"]):
            for ref in ref_items:
                ref_name = (
                    str(ref.get("field") or "").strip()
                    if isinstance(ref, dict) else str(ref).strip()
                )
                if not ref_name:
                    continue
                referenced_names.add(ref_name)
                referenced_names.add(
                    normalize_metric_name(ref_name, registered, aliases)
                )
        for key in ("指标", "维度"):
            kept: list[Any] = []
            for item in structured[key]:
                name = (
                    str(item.get("name") or item.get("field") or "").strip()
                    if isinstance(item, dict) else str(item).strip()
                )
                normalized = (
                    normalize_metric_name(name, registered, aliases)
                    if key == "指标" and name
                    else ""
                )
                if key == "维度" and required_metrics:
                    kept.append(item)
                    continue
                if key == "指标" and normalized in registered:
                    if name in referenced_names or normalized in referenced_names:
                        kept.append(item)
                    continue
                if name and name not in known_fields:
                    display.append({"field": name})
                    known_fields.add(name)
            structured[key] = kept
    time_granularity = value.get("时间粒度")
    structured["时间粒度"] = (
        time_granularity if isinstance(time_granularity, dict) else None
    )
    limit = value.get("限制")
    structured["限制"] = limit if isinstance(limit, (int, float)) else None
    output_requirement = value.get("输出要求")
    # 输出要求只保留用户明确说出的格式；旧模型习惯性写出的
    # “默认输出表格”属于编造的默认格式，按未声明处理（2026-10-21）。
    if isinstance(output_requirement, str) and output_requirement.strip() == "默认输出表格":
        output_requirement = None
    structured["输出要求"] = (
        clean_extraction_text(output_requirement)
        if isinstance(output_requirement, str) and output_requirement.strip()
        else None
    )
    dedup = clean_extraction_text(value.get("是否去重") or "")
    structured["是否去重"] = dedup if dedup in ("是", "否") else None
    query_choice = database_query_choice(value)
    structured["是否查询数据库"] = (
        "是" if query_choice is True else "否" if query_choice is False else None
    )
    for key in ('计算绑定', '结果操作', '计算待确认'):
        if isinstance(value.get(key), dict) and value[key]:
            structured[key] = copy.deepcopy(value[key])
    from app.planning.metric_thresholds import normalize_metric_units
    normalize_metric_units(structured, metric_units)
    return structured
