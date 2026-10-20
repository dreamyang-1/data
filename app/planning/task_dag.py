"""单一问题链路：任务意图识别与结构化参数提取。

职责边界：
- 本模块只处理“一个可独立验收的业务目标”：意图识别、参数提取、
  指标名归一与带反馈重试；
- “是否拆分、拆成什么子问题”的前置判断与多任务规划在
  compound_question.py
- 本模块同时承载两条链路共用的基础件：提取规范加载、extraction 清洗、
  指标目录解析、语义规范注入。
"""

import copy
import json
import logging
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import PlannerExtraction, PrimaryIntent
from app.planning.dependency_contract import projection
from app.analysis.chain_tools.temporal_window import has_pop_signal, has_yoy_signal

logger = logging.getLogger(__name__)


# 不查业务库的闲聊类意图：参数提取强制为空，与编排器展示守卫保持一致；
# 口径/血缘解释意图保留完整提取，只是不走查询执行链路。
_NO_DATA_INTENTS = {
    PrimaryIntent.CHAT,
    PrimaryIntent.CAPABILITY_HELP,
    PrimaryIntent.OUT_OF_SCOPE,
}
_METADATA_INTENTS = {
    PrimaryIntent.METRIC_DEFINITION,
    PrimaryIntent.DATA_LINEAGE,
}

_PARAMETER_PATTERN = re.compile(r"^(.+?)（(.+?)）$")


def parse_parameter_mentions(parameters: list[str]) -> list[dict[str, str]]:
    """把“词（角色）”格式的参数解析成 mentions 通道需要的字典结构。"""
    mentions: list[dict[str, str]] = []
    for value in parameters:
        match = _PARAMETER_PATTERN.match(value.strip())
        if match is None:
            continue
        text, role = match.group(1).strip(), match.group(2).strip()
        if text:
            mentions.append({"text": text, "role_hint": role})
    return mentions


# 结构化提取JSON的固定键，与国药语义解析器规范一一对应。
_EXTRACTION_INTENT_KEY = "意图"
_EXTRACTION_LIST_KEYS = ("业务域", "实体", "指标", "维度", "展示字段", "过滤条件", "排序")
_EXTRACTION_LIST_MAX = 20
_EXTRACTION_TEXT_MAX = 200

# 中文意图枚举到系统意图枚举的映射，模型未给出intent时兜底。
_CHINESE_INTENT_TO_PRIMARY = {
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
_CHINESE_INTENT_LABELS = frozenset(_CHINESE_INTENT_TO_PRIMARY)

_EXTRACTION_PROMPT_PATH = Path(__file__).with_name("structured_extraction_prompt.txt")
_EXTRACTION_PROMPT_MARKER = "【用户提示词（业务场景上下文）】"


def _ground_location_owners(extraction: dict[str, Any] | None, question: str) -> None:
    """Correct only explicit location modifiers at the NL extraction boundary.

    A model can still pick the wrong catalog parent despite prompt examples.
    Do not infer ownership from grouping alone, and never alter fields/values.
    """
    if not extraction or not question:
        return
    candidates = {item for section in ('实体', '维度') for item in extraction.get(section) or []
                  if isinstance(item, str) and len(item) >= 2}
    candidates.update(item.get('entity') for section in ('过滤条件', '展示字段')
                      for item in extraction.get(section) or []
                      if isinstance(item, dict) and isinstance(item.get('entity'), str))
    for item in extraction.get('过滤条件') or []:
        if not isinstance(item, dict) or not re.search(
                r'省份|城市|地区|区域|所在地|province|city|region', str(item.get('field') or ''), re.I):
            continue
        raw = item.get('value')
        values = raw if isinstance(raw, list) else [raw]
        owners = set()
        for owner in candidates:
            for value in values:
                if not isinstance(value, str) or len(value.strip()) < 2:
                    continue
                place, entity = re.escape(value.strip()), re.escape(owner)
                if re.search(entity + r'(?:的)?(?:所在地|所在省份|所在城市|所在地区|省份|城市)'
                             + r'(?:为|是|在|等于|=|：|:)?\s*' + place, question) or re.search(
                                 place + r'(?:市|省)?(?:的)?(?:各个|各家|所有|每个|每家|各)?' + entity,
                                 question):
                    owners.add(owner)
        if len(owners) == 1:
            item['entity'] = next(iter(owners))


@lru_cache(maxsize=1)
def _load_extraction_prompt_sections() -> tuple[str, str]:
    """读取国药结构化提取提示词，按标记拆成解析器规范与内置语义规范。"""
    try:
        text = _EXTRACTION_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    if _EXTRACTION_PROMPT_MARKER not in text:
        return text.strip(), ""
    parser_part, embedded_part = text.split(_EXTRACTION_PROMPT_MARKER, 1)
    return parser_part.strip(), embedded_part.strip()


def extraction_parser_prompt() -> str:
    """结构化提取解析器规范（键定义、判定规则、示例），不含业务语义快照。"""
    return _load_extraction_prompt_sections()[0]


def extraction_embedded_spec() -> str:
    """提示词文件里内置的医药域语义快照，仅在未配置任何语义来源时兜底。"""
    return _load_extraction_prompt_sections()[1]


def _clean_extraction_text(value: Any) -> str:
    text = str(value).strip()
    return text[:_EXTRACTION_TEXT_MAX]


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


def _strip_json_fences(text: str) -> str:
    """剥离模型输出外层的 markdown 代码围栏。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped


def _clean_extraction_item(value: Any) -> Any:
    """列表元素只保留字符串和对象两种形态，超长截断。"""
    if isinstance(value, str):
        return _clean_extraction_text(value)
    if isinstance(value, dict):
        return {
            _clean_extraction_text(key): _clean_extraction_text(item)
            if isinstance(item, (str, int, float)) else item
            for key, item in list(value.items())[:12]
        }
    if isinstance(value, (int, float)):
        return value
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


_SEMANTIC_SPEC_MARKERS = ("# 业务语义规范", "## 业务语义规范")
_SEMANTIC_SPEC_MAX_CHARS = 12000


def extract_semantic_spec_section(agent_prompt: str) -> str:
    """从核心指令里截取业务语义规范段，供拆分与提取模型对齐参数口径。

    核心指令开头通常是角色设定，只取“业务语义规范”标题起的内容；
    没有该标题时返回空，避免把整段角色设定塞进提示词。
    """
    from app.services.general_rules import rule_section, without_rule_section
    rules = rule_section(agent_prompt or "")
    text = without_rule_section(agent_prompt or "").strip()
    if not text:
        return rules
    positions = [
        pos for marker in _SEMANTIC_SPEC_MARKERS
        if (pos := text.find(marker)) >= 0
    ]
    if not positions:
        return rules
    return text[min(positions):].strip()[:_SEMANTIC_SPEC_MAX_CHARS] + rules


def _semantic_context_section(semantic_context: str, *, origin: str = "智能体配置") -> str:
    return (
        f"\n以下平台业务语义规范摘自{origin}，是当前问题判断指标、实体、维度、"
        "同义词与口径的唯一业务依据；优先于通用知识与示例，不得改写登记名；"
        "规范中未出现的对象、指标、维度或取值不得自行引入，"
        "用户说法是某个登记指标的同义词时归一为该指标的登记原名；"
        "与任何登记指标都对应不上时置空，不得把用户提到的属性说法"
        "归到语义相近的注册指标名上，只有与规范内任何指标都无关、"
        "也无法由规范内指标派生的纯概念词才按指标口径解释处理"
        "（意图输出METRIC_DEFINITION，指标名保留用户原文）：\n"
        + semantic_context.strip()
        + "\n"
    )


@dataclass(frozen=True)
class PlannerOutcome:
    """单一路径一次规划的全部产出：意图、参数与结构化提取。"""

    single_intent: PrimaryIntent | None = None
    single_parameters: tuple[str, ...] = ()
    single_structured: dict[str, Any] | None = None
    # 模型生成的任务名称（陈述句），缺省时展示层按规则从问题文本转换
    single_task_name: str | None = None
    # 拆解模型输出的任务问题文本：追问复用轮带"复用上一轮查询结果"前缀
    # 和具体参数值，展示与任务表按它判断参数提取型复用
    single_task_question: str | None = None
    # 本次注入规范解析出的指标目录（登记名集合 + 别名映射），供归一与重试判定
    metric_catalog: tuple[frozenset[str], dict[str, str]] | None = None

    @property
    def single_extraction(self) -> PlannerExtraction | None:
        if (
            self.single_intent is None
            and not self.single_parameters
            and self.single_structured is None
        ):
            return None
        return PlannerExtraction(
            intent=self.single_intent,
            parameters=list(self.single_parameters),
            structured=self.single_structured,
        )


@dataclass(frozen=True)
class DecomposedTask:
    """任务拆解产出的单个任务：问题文本、任务内依赖与结构化提取。"""

    question: str
    depends_on: tuple[int, ...] = ()
    expected_output: str | None = None
    intent: PrimaryIntent | None = None
    parameters: tuple[str, ...] = ()
    structured: dict[str, Any] | None = None
    name: str | None = None


@dataclass(frozen=True)
class QuestionDecomposition:
    """单个问题的任务拆解结果：问题级意图与1到4个任务。"""

    intent: PrimaryIntent | None = None
    tasks: tuple[DecomposedTask, ...] = ()
    metric_catalog: tuple[frozenset[str], dict[str, str]] | None = None
    # 规范指标表里依赖指标列非空的复合指标：登记名 -> 成分指标
    metric_dependencies: dict[str, tuple[str, ...]] | None = None
    grounding_default_times: tuple[str, ...] = ()
    grounding_registered_conditions: tuple[str, ...] = ()
    shared_region_denominator: bool = False
    boundary_plan_complete: bool = False


_NO_DATABASE_QUERY = re.compile(r"(?:不|无需|无须|别)(?:要|用|得|需要)?(?:再)?(?:查询|访问|查)(?:业务)?(?:数据库|库)")


def _repair_unavailable_local_projection(decomposition, question):
    """A planner-invented local projection cannot supply a new related field."""
    if _NO_DATABASE_QUERY.search(question):
        return decomposition  # An explicit user restriction remains authoritative.
    tasks = list(decomposition.tasks)
    for index, task in enumerate(tasks):
        structured = task.structured or {}
        local = str(structured.get('是否查询数据库') or '').startswith('否') or _NO_DATABASE_QUERY.search(task.question)
        wanted = [(owner, field) for owner, field in projection(structured) if owner]
        if not (local and task.depends_on and wanted) or structured.get('计算绑定'):
            continue
        if structured.get('指标') and not structured.get('结果操作'):
            continue  # Preserve existing metric calculations without a projection operation.
        available = {item for dep in task.depends_on if 0 <= dep < index
                     for item in projection(tasks[dep].structured)}
        if all(item in available for item in wanted):
            continue
        corrected = copy.deepcopy(structured)
        corrected['是否查询数据库'] = '是'
        corrected['意图'] = '明细查询'
        corrected['指标'] = []
        corrected.pop('结果操作', None)
        # Render this child's fields and conditions, not the predecessor's
        # repeated Top N wording (which would become a new ranking constraint).
        text = '查询' + '、'.join(f'{owner}的{field}' for owner, field in wanted)
        period = (corrected.get('时间粒度') or {}).get('time_range')
        if period:
            text += '，时间范围：' + str(period)
        for condition in corrected.get('过滤条件') or []:
            value = condition.get('value')
            values = value if isinstance(value, list) else [value]
            text += '，' + str(condition.get('entity') or '') + str(condition.get('field') or '')
            text += str(condition.get('op') or condition.get('operator') or '=') + '、'.join(map(str, values))
        tasks[index] = replace(task, question='根据上述任务的查询结果，' + text, name=text.split('，', 1)[0],
            intent=PrimaryIntent.DETAIL_QUERY, structured=corrected,
            parameters=tuple(extraction_to_parameters(corrected)))
    return replace(decomposition, tasks=tuple(tasks))


# 指标目录解析要识别的表头词：命中其一视为表头行，防止把下一张表的表头当指标
_TABLE_HEADER_WORDS = {
    "指标", "指标名称", "维度", "维度名称", "实体", "实体名称",
    "业务域", "业务域名称", "属性", "属性名称", "别名", "标准名称",
    "序号", "名称", "关系", "规则", "同义词",
}
# 规范里同义词列的常见占位写法，不是别名
_METRIC_ALIAS_PLACEHOLDERS = {"", "无", "-", "—", "/"}


def _parse_metric_catalog(spec_text: str) -> tuple[frozenset[str], dict[str, str]]:
    """从注入的语义规范解析指标目录：登记名集合 + 别名到登记名的映射。

    识别两类表：指标表（表头首格为“指标/指标名称”，第二格是同义词）和
    别名映射表（第三格为“指标”，第二格登记名、第一格别名列表）。
    """
    registered: set[str] = set()
    aliases: dict[str, str] = {}

    def _split_aliases(cell: str) -> list[str]:
        return [
            alias.strip()
            for alias in re.split(r"[,，、/]", cell)
            if alias.strip() not in _METRIC_ALIAS_PLACEHOLDERS
        ]

    in_metric_table = False
    for raw_line in spec_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            in_metric_table = False
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= {"-", ":", " "}:
            continue
        if cells[0] in _TABLE_HEADER_WORDS:
            in_metric_table = cells[0] in {"指标", "指标名称"}
            continue
        if in_metric_table and cells[0]:
            registered.add(cells[0])
            for alias in _split_aliases(cells[1]):
                aliases.setdefault(alias, cells[0])
            continue
        if len(cells) >= 3 and cells[2] == "指标" and cells[1]:
            registered.add(cells[1])
            for alias in _split_aliases(cells[0]):
                aliases.setdefault(alias, cells[1])
    return frozenset(registered), aliases


def _parse_metric_dependencies(spec_text: str) -> dict[str, tuple[str, ...]]:
    """从注入规范的指标表解析依赖指标列：复合口径指标登记名 -> 成分指标元组。

    只认表头同时含"指标"与"依赖指标"的表；成分按逗号分隔，
    "无"/空等占位不算成分。指标表没有依赖指标列时返回空映射。
    """
    dependencies: dict[str, tuple[str, ...]] = {}
    in_metric_table = False
    dep_index = -1
    for raw_line in spec_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            in_metric_table = False
            dep_index = -1
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= {"-", ":", " "}:
            continue
        if cells[0] in _TABLE_HEADER_WORDS:
            in_metric_table = (
                cells[0] in {"指标", "指标名称"}
                and any("依赖指标" in cell for cell in cells)
            )
            dep_index = next(
                (i for i, cell in enumerate(cells) if "依赖指标" in cell), -1
            )
            continue
        if in_metric_table and cells[0] and 0 < dep_index < len(cells):
            components = [
                dep.strip()
                for dep in re.split(r"[,，、/]", cells[dep_index])
                if dep.strip() not in _METRIC_ALIAS_PLACEHOLDERS
            ]
            if components:
                dependencies[cells[0]] = tuple(components)
    return dependencies


# 环比/同比问题改判趋势分析的序列特征：跨时间范围或逐期措辞说明
# 计算落在多期时间序列上；两期式（如“11月环比10月”）不属于此类。
_PERIOD_SERIES_WORDS = re.compile(
    r"各月|每月|逐月|每个月|哪个月|哪个月份|各期|逐期|趋势|变化情况|增长情况|下降情况"
)
_PERIOD_RANGE_WORDS = re.compile(
    r"到|至|~|～|最近|近[0-9一二两三四五六七八九十]|过去[0-9一二两三四五六七八九十]"
)
# 对象间比较措辞：保持对比分析，不做趋势改判
_OBJECT_COMPARISON_WORDS = re.compile(
    r"对比|比较|差异|谁更高|谁更低|哪个更高|哪个更低"
)


def _period_change_series_question(question: str) -> bool:
    """判断环比/同比问题是否落在多期时间序列上。

    只有同时满足环比/同比措辞、无对象间比较词、且带时间范围或逐期
    序列特征时才改判趋势分析；两期式与对象对比保持原判。
    """
    compact = str(question or "").replace(" ", "")
    if not (has_pop_signal(compact) or has_yoy_signal(compact)):
        return False
    if _OBJECT_COMPARISON_WORDS.search(compact):
        return False
    return bool(
        _PERIOD_RANGE_WORDS.search(compact) or _PERIOD_SERIES_WORDS.search(compact)
    )


# 时间维度字段识别用词；规划器排序归一与引擎的时间列识别保持同一口径
_TEMPORAL_DIMENSION_TOKENS = (
    "日期", "时间", "月份", "季度", "年份", "周期",
    "date", "time", "month", "quarter", "year", "period", "week",
)


def _normalize_trend_time_order(structured: dict[str, Any] | None) -> None:
    """趋势意图下把排序统一改成时间维度升序。

    模型会把环比/趋势类问题的排序写成指标降序，SQL 按指标值返回后
    时间乱序，趋势引擎按相邻行算逐期变化时直接拒算。趋势分析只认
    时间升序，这里在提取出口统一纠正。
    """
    if not structured:
        return
    dimensions = [
        str(item) for item in structured.get("维度") or [] if str(item).strip()
    ]
    temporal = [
        item for item in dimensions
        if any(token in item.lower() for token in _TEMPORAL_DIMENSION_TOKENS)
    ]
    if not temporal:
        return
    time_field = temporal[0]
    sorts = structured.get("排序")
    if isinstance(sorts, list) and any(
        isinstance(item, dict) and item.get("field") == time_field
        and str(item.get("order") or "").lower() == "asc"
        for item in sorts
    ):
        return
    structured["排序"] = [{"field": time_field, "order": "asc"}]
    structured["排序类型"] = "升序"


def _shared_region_denominator_from_spec(spec_text: str) -> bool:
    """Recognize the same published rule after the document is regenerated."""
    if '[dealer_shared_region_denominator:v1]' in spec_text:
        return True
    dependencies = _parse_metric_dependencies(spec_text)
    if '区域全部医院总数' not in dependencies.get('区域医院覆盖率', ()):
        return False
    definitions = {}
    in_metrics = False
    for line in spec_text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if not line.strip().startswith('|'):
            in_metrics = False
        elif cells[0] in _TABLE_HEADER_WORDS:
            in_metrics = cells[0] in {'指标', '指标名称'} and '业务口径' in cells
        elif in_metrics and len(cells) >= 3:
            definitions[cells[0]] = re.sub(r'\s+', '', cells[2])
    return (
        '所有经销商共用同一分母' in definitions.get('区域医院覆盖率', '')
        and '销售订单无关' in definitions.get('区域全部医院总数', '')
        and '不接受经销商条件' in definitions.get('区域全部医院总数', '')
    )


def _confirmed_hospital_department_scope(task_question, source_question, structured, model_id):
    """Carry the user's confirmed model-81 product-applicability meaning.

    Hospital order questions use product applicable departments in this
    business workflow. Explicit order/administrative department requests keep
    their own scope; physical binding remains the original ASL's job.
    """
    if (model_id != 81 or not structured or '医院' not in source_question
            or not re.search(r'订单|销售', source_question)
            or re.search(r'业务科室|business_department|行政科室|院属科室|下辖科室|医院下属科室', source_question, re.I)):
        return task_question
    department_filter = False
    for item in structured.get('过滤条件') or []:
        if isinstance(item, dict) and item.get('field') in {
            '业务科室', '销售订单.业务科室', 'sales_order.business_department',
            '科室', '科室名称', '适用科室',
        }:
            item['entity'] = '商品科室关联'
            item['field'] = '科室编码'
            department_filter = True
    if department_filter:
        task_question = task_question.replace('业务科室', '商品适用科室')
        if '商品适用科室' not in task_question:
            task_question += '，科室按商品适用科室筛选订单'
    return task_question


def _ground_inferred_hospital_location(structured, source_question, model_id):
    """Use hospital geography when the model invents a business-area qualifier."""
    if (model_id != 81 or not structured
            or re.search(r'业务(?:省份?|城?市|地区|区域)', source_question)):
        return
    for item in structured.get('过滤条件') or []:
        if (isinstance(item, dict) and item.get('entity') == '医院'
                and item.get('field') in {'业务市', '业务省'}):
            item['field'] = {'业务市': '城市', '业务省': '省份'}[item['field']]


def _normalize_metric_name(
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


def _normalize_extraction_metrics(
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
                sort_item["field"] = _normalize_metric_name(
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
            normalized = _normalize_metric_name(name.strip(), registered, aliases)
            item["name"] = normalized
            if report_off_catalog and normalized not in registered:
                off_catalog.append(normalized)
            metrics[index] = item
        elif isinstance(item, str) and item.strip():
            normalized = _normalize_metric_name(item.strip(), registered, aliases)
            metrics[index] = normalized
            if report_off_catalog and normalized not in registered:
                off_catalog.append(normalized)
    return off_catalog


class TaskPlanningError(RuntimeError):
    pass


class TaskInputBindingError(TaskPlanningError):
    """A calculation cannot name inputs outside its task dependency contract."""


from app.planning.calculation_bindings import materialize_computation_binding


def _registered_default_times(source: str, spec: str) -> tuple[str, ...]:
    """Only the current model's declared defaults can supplement user text."""
    if re.search(
        r"\d{4}|(?:\d+|[一二三四五六七八九十两]+)(?:年|个月|月份|月|季度|周|日|天)"
        r"|今天|昨天|前天|本周|上周|下周|本月|上月|下月|今年|去年|前年|明年|最近|近期|近来",
        source,
    ):
        return ()
    defaults = re.findall(
        r"未指定时间(?:范围)?[^。；\n]{0,30}?默认(?:取|为|使用)?\s*"
        r"(最近\s*\d+\s*个?\s*(?:月|周|天|年))",
        spec,
    )
    return tuple(re.sub(r"\s+", "", default) for default in defaults)


def _respect_shared_region_denominator(decomposition, source):
    """Apply the existing versioned catalog rule to the proven bad component."""
    if not decomposition.shared_region_denominator or '经销商' not in source or re.search(
            r"按(?:省份|城市)|各(?:省|市)|每(?:个)?(?:省份|城市)", source):
        return decomposition
    tasks = []
    for task in decomposition.tasks:
        data = task.structured
        names = [item.get('name') if isinstance(item, dict) else item
                 for item in (data or {}).get('指标', [])]
        if names != ['区域全部医院总数'] or (data or {}).get('是否查询数据库') == '否':
            tasks.append(task)
            continue
        wrong_grouping = bool(data.get('维度'))
        wrong_entity = any(entity in ('经销商', '销售订单') for entity in data.get('实体', []))
        wrong_filters = any(item.get('entity') == '经销商' for item in data.get('过滤条件', [])
                            if isinstance(item, dict))
        wrong_period = bool((data.get('时间粒度') or {}).get('time_range')) and (
            (data.get('时间粒度') or {}).get('time_range') != '全部历史')
        if not (wrong_grouping or wrong_entity or wrong_filters or wrong_period):
            tasks.append(task)
            continue
        fixed = copy.deepcopy(data)
        fixed['维度'] = []
        fixed['实体'] = [entity for entity in fixed.get('实体', []) if entity not in ('经销商', '销售订单')]
        fixed['过滤条件'] = [item for item in fixed.get('过滤条件', [])
                             if not isinstance(item, dict) or item.get('entity') != '经销商']
        fixed['展示字段'] = [item for item in fixed.get('展示字段', [])
                             if not isinstance(item, dict) or item.get('entity') != '经销商']
        # A master-data denominator is independent of transaction activity time.
        fixed['时间粒度'] = {'unit': None, 'time_range': '全部历史', 'anchor': None}
        period = '全部历史'
        filters = '；'.join(
            f"{item.get('entity') or ''}{item.get('field') or ''}"
            f"{item.get('op') or item.get('operator') or '='}"
            + '、'.join(str(value) for value in (
                item.get('value') if isinstance(item.get('value'), list) else [item.get('value')]))
            for item in fixed.get('过滤条件') or [] if isinstance(item, dict)
        )
        question = f'查询{period}区域全部医院总数'
        if fixed.get('过滤条件'):
            question += '，筛选条件：' + filters
        if task.depends_on:
            question = '根据上述任务的查询结果，' + question
        tasks.append(replace(task, question=question, name=f'查询{period}区域全部医院总数',
                             structured=fixed, parameters=tuple(extraction_to_parameters(fixed))))
    return replace(decomposition, tasks=tuple(tasks))


_EXTRACTION_SCHEMA_DESCRIPTION = (
    "结构化提取JSON，严格按系统提示词中的结构化提取规范输出，"
    "固定键：意图/业务域/实体/指标/维度/展示字段/过滤条件/时间粒度/排序/限制/输出要求/是否去重/是否查询数据库；"
    "意图取值限定为 指标查询、趋势分析、对比分析、占比分析、排名分析、明细查询 之一。"
    "数据查询任务的指标名：用户说法是某个登记指标的同义词时输出该指标的登记原名，"
    "与任何登记指标都对应不上时置空，不得用语义相近的指标顶替，不得自造指标名；"
    "不需要查询业务数据的问题（闲聊、超出业务范围）输出null；"
    "指标口径解释任务按规范完整提取参数，指标名保留用户原文，"
    "意图键固定输出“解释口径”"
)


class _PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=2,
        max_length=1000,
        description="必填；该任务完整可独立执行的问题文本",
    )
    name: str | None = Field(
        default=None,
        min_length=2,
        max_length=60,
        description="该任务的名称：动词+业务指标+过滤条件的陈述句，"
            "概括任务要得到的业务结果，不得照抄用户问题原文，"
            "不带疑问词（是多少、有哪些等），结尾不加标点",
    )
    depends_on: list[int] = Field(
        default_factory=list,
        description="必填；默认空数组；依赖同问题内前序任务时填从0开始的任务下标",
    )
    expected_output: str | None = Field(
        default=None,
        max_length=300,
        description="该任务可独立验收的交付物说明",
    )
    extraction: dict[str, Any] | None = Field(
        default=None,
        description=_EXTRACTION_SCHEMA_DESCRIPTION,
    )
    postprocess: dict[str, Any] | None = None
    clarification: dict[str, Any] | None = None


_CALCULATION_PARAMETER_PROMPT = """
计算执行合同（仅为extraction的内部执行参数，不改变数据库字段绑定）：
每个纯计算任务必须明确实际运算，不能只写“根据结果计算”。
两个指标相除使用extraction.计算绑定：{"operation":"divide","numerator":{"source_task_index":0,"column":"分子指标登记名"},"denominator":{"source_task_index":1,"column":"分母指标登记名"},"result_field":"结果指标名","format":"number或percent"}。
同一查询结果逐行相除时，两操作数引用同一个前序任务。source_task_index为本问题从0开始的任务下标，必须在depends_on中；不能引用自身或猜测数据集ID。分子、分母顺序严格依据当前规范中的公式，不能按依赖指标列的排列顺序猜测。即使复合指标尚未拆分，也在其extraction里给出上述计算绑定的column、operation、result_field和format，此时不填source_task_index，拆分器负责关联成分任务。
其他计算可使用extraction.结果操作：{"source_task_index":0,"operation":{"type":"pipeline","operations":[...]}}。只允许derive（left_field/right_field/operator/result_field，operator为add/subtract/multiply/divide）、aggregate（group_by和aggregations，每项field/function/alias）、filter、select、sort、sort_limit、limit；不得输出SQL、Python或任意表达式。
排序操作格式为{"type":"sort","field":"实际结果列名","descending":true}；取前N格式为{"type":"limit","count":N}；合并排序取前N格式为{"type":"sort_limit","field":"实际结果列名","descending":true,"count":N}。最终按计算结果排名时，成分取数任务不得先按其他指标取前N；只在用户明确先选定对象范围时保留该范围限制。
排名必须保留最终计算字段的排序方向和限制人数；按人或组织计算时，所有相应取数任务都保留对象分组及必要展示字段，不能变成全局总计；共用分母按规范保留原有范围。公式或字段依据不明时不要编造计算绑定。
"""

# Compatibility name for consumers of the extraction specification. It no
# longer includes task-count decisions or the old combined planning prompt.
from app.planning.task_plan_stages import PARAMETER_PROMPT as _PARAMETER_PROMPT
_SINGLE_SYSTEM_PROMPT = (_PARAMETER_PROMPT + '\n结构化提取规范：\n'
                         + extraction_parser_prompt() + _CALCULATION_PARAMETER_PROMPT)


class SingleQuestionPlanner:
    """先规划任务与依赖，再批量提取任务参数；意图随提取返回。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    async def plan_question(
        self,
        question: str,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
    ) -> QuestionDecomposition:
        """问题级入口：任务拆解 + 逐任务意图识别与参数提取，带一次纠错重试。"""
        retry_feedback: str | None = None
        last_result = QuestionDecomposition()
        if not (
            self.settings.intent_model_enabled
            and self.settings.multi_question_model_enabled
            and self.settings.intent_model_api_key
        ):
            # 提取模型未启用时不发请求，直接返回空结果由调用方走兜底
            return last_result
        attempts = 2
        for attempt in range(attempts):
            try:
                decomposition = await self._model_decompose(
                    question,
                    semantic_context=semantic_context,
                    semantic_model_id=semantic_model_id,
                    retry_feedback=retry_feedback,
                )
            except TaskInputBindingError as exc:
                if attempt == attempts - 1:
                    raise
                retry_feedback = '任务规划或计算结构无效，请修正后重新输出完整计划：' + str(exc)
                continue
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                # 传输、Schema 或解析失败先整体重试一次，仍失败返回空结果
                logger.warning(
                    "single planner decompose failed attempt=%s error=%r",
                    attempt, exc,
                )
                if attempt == attempts - 1:
                    return last_result
                continue
            decomposition = _repair_unavailable_local_projection(decomposition, question)
            last_result = decomposition
            off_data, off_meta = self._normalize_decomposition_metrics(decomposition)
            unsplit_compound = self._unsplit_compound_metrics(decomposition)
            empty_extraction = (
                not any(task.structured is not None for task in decomposition.tasks)
                and decomposition.intent not in _NO_DATA_INTENTS
            )
            if attempt == 0 and (empty_extraction or off_data or off_meta):
                # 全部任务提取为空或指标名目录外，说明这次输出不可信，
                # 带纠错说明重试一次
                retry_feedback = self._retry_feedback(
                    off_data, off_meta, empty_extraction=empty_extraction,
                )
                continue
            if unsplit_compound:
                if decomposition.boundary_plan_complete:
                    if attempt == attempts - 1:
                        raise TaskInputBindingError('任务计划遗漏复合指标依赖，未执行不完整计划')
                    retry_feedback = '请在参数提取之前完整规划这些登记指标的成分及计算任务：' + '、'.join(
                        name + '（' + '、'.join(parts) + '）' for name, parts in unsplit_compound)
                    continue
                # 复合指标按登记成分在代码层确定性拆分：每个成分一个查询任务，
                # 再加依赖全部成分的计算任务。不走模型重试——提示词里的拆分
                # 条款会反过来干扰指标选择（实测多次复现）
                decomposition = self._split_compound_metric_tasks(
                    decomposition, unsplit_compound
                )
                last_result = decomposition
            from app.planning.calculation_bindings import materialize_declared_division, materialize_declared_result_operation, materialize_computation_binding
            try:
                if not decomposition.boundary_plan_complete:
                    # Compatibility for legacy combined planners only. New
                    # boundaries already include local result operations.
                    last_result = materialize_declared_result_operation(materialize_declared_division(last_result))
                for index, task in enumerate(last_result.tasks):
                    if task.depends_on:
                        materialize_computation_binding(task.structured, task_index=index,
                            dependencies=[f'task-{dep + 1}' for dep in task.depends_on])
            except TaskInputBindingError as exc:
                if attempt == attempts - 1:
                    raise
                retry_feedback = '计算操作不完整或无效，请修正后重新输出完整计划：' + str(exc)
                continue
            return _respect_shared_region_denominator(last_result, question)
        return last_result

    @staticmethod
    def _unsplit_compound_metrics(
        decomposition: QuestionDecomposition,
    ) -> list[tuple[str, tuple[str, ...]]]:
        """找出数据查询任务里未按公式拆分的复合指标：返回（指标, 成分）列表。

        计算任务（问题含"不再查询数据库"）本来就携带复合指标名，
        口径解释任务保留用户原文，二者都不算未拆分。
        """
        dependencies = decomposition.metric_dependencies
        if not dependencies:
            return []
        catalog = decomposition.metric_catalog
        if catalog is None:
            return []
        registered, aliases = catalog
        findings: list[tuple[str, tuple[str, ...]]] = []
        for task in decomposition.tasks:
            if "不再查询数据库" in task.question or task.intent in _METADATA_INTENTS:
                continue
            structured = task.structured
            if not isinstance(structured, dict):
                continue
            if str(structured.get('是否查询数据库') or '').startswith('否'):
                continue
            metrics = structured.get("指标")
            if not isinstance(metrics, list):
                continue
            for item in metrics:
                name = item.get("name") if isinstance(item, dict) else item
                if not isinstance(name, str) or not name.strip():
                    continue
                normalized = _normalize_metric_name(name.strip(), registered, aliases)
                components = dependencies.get(normalized)
                if components and all(dep in registered for dep in components):
                    findings.append((normalized, components))
        return findings

    @staticmethod
    def _split_compound_metric_tasks(
        decomposition: QuestionDecomposition,
        findings: list[tuple[str, tuple[str, ...]]],
    ) -> QuestionDecomposition:
        """把未拆分的复合指标任务按登记成分拆成查询任务+计算任务。

        成分任务沿用原任务的过滤条件与时间范围，指标换成成分登记原名，
        排序字段同步替换、行数限制清空；计算任务保留原排序与限制，
        依赖全部成分任务，问题文本按原链路口径写"不再查询数据库"。
        """
        if not findings:
            return decomposition
        split_by_metric = dict(findings)
        new_tasks: list[DecomposedTask] = []
        old_to_new: dict[int, int] = {}
        inherited_dependencies: set[int] = set()
        for old_index, task in enumerate(decomposition.tasks):
            structured = task.structured if isinstance(task.structured, dict) else None
            metric = None
            if structured:
                for item in structured.get("指标") or []:
                    name = item.get("name") if isinstance(item, dict) else item
                    if isinstance(name, str) and name.strip() in split_by_metric:
                        metric = name.strip()
                        break
            if metric is None or not structured:
                inherited_dependencies.add(len(new_tasks))
                new_tasks.append(task)
                old_to_new[old_index] = len(new_tasks) - 1
                continue
            components = split_by_metric[metric]

            def _condition_text(base: dict[str, Any]) -> str:
                parts: list[str] = []
                time_granularity = base.get("时间粒度")
                if isinstance(time_granularity, dict) and time_granularity.get("time_range"):
                    parts.append(str(time_granularity["time_range"]))
                for item in base.get("过滤条件") or []:
                    if isinstance(item, dict):
                        values = item.get("value")
                        if isinstance(values, list) and values:
                            parts.append("".join(str(v) for v in values))
                return "".join(parts)

            condition = _condition_text(structured)
            component_start = len(new_tasks)
            for comp in components:
                comp_structured = copy.deepcopy(structured)
                comp_structured.pop('计算绑定', None)
                comp_structured.pop('结果操作', None)
                comp_structured["指标"] = [{"name": comp}]
                comp_structured['是否查询数据库'] = '是'
                for sort_item in comp_structured.get("排序") or []:
                    if isinstance(sort_item, dict) and sort_item.get("field") == metric:
                        sort_item["field"] = comp
                comp_structured["限制"] = None
                inherited_dependencies.add(len(new_tasks))
                dimensions = [str(item.get('name') or item.get('field') or '') if isinstance(item, dict) else str(item)
                              for item in comp_structured.get('维度') or []]
                grouping = '各' + '、'.join(dimensions) + '的' if dimensions else ''
                display = [str(item.get('field') or item.get('name') or '') if isinstance(item, dict) else str(item)
                           for item in comp_structured.get('展示字段') or []]
                new_tasks.append(DecomposedTask(
                    question=("根据上述任务的查询结果，" if task.depends_on else "") + f"统计{condition}{grouping}{comp}"
                        + ('，同时返回' + '、'.join(display) if display else ''),
                    name=f"统计{condition}{grouping}{comp}"[:60],
                    intent=task.intent,
                    depends_on=task.depends_on,
                    parameters=tuple(extraction_to_parameters(comp_structured)),
                    structured=comp_structured,
                ))
            calc_structured = copy.deepcopy(structured)
            calc_structured["指标"] = [{"name": metric}]
            calc_structured["意图"] = "指标计算"
            calc_structured['是否查询数据库'] = '否'
            binding = calc_structured.get('计算绑定')
            if isinstance(binding, dict):
                for role in ('numerator', 'denominator'):
                    operand = binding.get(role)
                    if not isinstance(operand, dict) or operand.get('column') not in components:
                        raise TaskInputBindingError('复合指标运算必须绑定登记的成分指标')
                    operand.pop('source_task_id', None)
                    operand['source_task_index'] = component_start + components.index(operand['column'])
            new_tasks.append(DecomposedTask(
                question=f"根据上述任务的查询结果计算{metric}，完成原要求：{task.question}，不再查询数据库",
                depends_on=tuple(range(component_start, component_start + len(components))),
                name=f"计算{metric}",
                intent=task.intent,
                parameters=tuple(extraction_to_parameters(calc_structured)),
                structured=calc_structured,
            ))
            old_to_new[old_index] = len(new_tasks) - 1
        if len(new_tasks) == len(decomposition.tasks):
            return decomposition
        new_tasks = [replace(task, depends_on=tuple(old_to_new.get(dep, dep) for dep in task.depends_on))
                     if index in inherited_dependencies else task
                     for index, task in enumerate(new_tasks)]
        return replace(decomposition, tasks=tuple(new_tasks))

    async def plan_single(
        self,
        question: str,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
    ) -> PlannerOutcome:
        """兼容入口：问题按单任务消费，返回首个任务的意图与提取。"""
        decomposition = await self.plan_question(
            question,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
        )
        first = next(iter(decomposition.tasks), None)
        if first is None:
            return PlannerOutcome(
                single_intent=decomposition.intent,
                metric_catalog=decomposition.metric_catalog,
            )
        return PlannerOutcome(
            single_intent=first.intent or decomposition.intent,
            single_parameters=first.parameters,
            single_structured=first.structured,
            single_task_name=first.name,
            metric_catalog=decomposition.metric_catalog,
        )

    async def extract_for_task(
        self,
        question: str,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
    ) -> tuple[PrimaryIntent | None, tuple[str, ...], dict[str, Any] | None]:
        """兼容入口：返回首个任务的意图与结构化提取。"""
        decomposition = await self.plan_question(
            question,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
        )
        first = next(iter(decomposition.tasks), None)
        if first is None:
            return decomposition.intent, (), None
        return first.intent, first.parameters, first.structured

    @staticmethod
    def _normalize_decomposition_metrics(
        decomposition: QuestionDecomposition,
    ) -> tuple[list[str], list[str]]:
        """逐任务做指标名归一：返回（数据问题目录外指标，口径问题目录外指标）。"""
        off_data: list[str] = []
        off_meta: list[str] = []
        for task in decomposition.tasks:
            task_off_data, task_off_meta = SingleQuestionPlanner._normalize_single_metrics(
                task.intent, task.structured, decomposition.metric_catalog
            )
            off_data.extend(task_off_data)
            off_meta.extend(task_off_meta)
        return off_data, off_meta

    @staticmethod
    def _normalize_single_metrics(
        intent: PrimaryIntent | None,
        structured: dict[str, Any] | None,
        catalog: tuple[frozenset[str], dict[str, str]] | None,
    ) -> tuple[list[str], list[str]]:
        """单一路径的指标名归一：返回（数据问题目录外指标，口径问题目录外指标）。

        计算/筛选任务（“不再查询数据库”）的指标名允许带用户原文叫法，
        只归一不计入目录外。
        """
        if catalog is None or structured is None:
            return [], []
        registered, aliases = catalog
        question_text = str(structured.get(_EXTRACTION_INTENT_KEY) or "")
        if "指标计算" in question_text:
            _normalize_extraction_metrics(
                structured, registered, aliases, report_off_catalog=False
            )
            return [], []
        off_catalog = _normalize_extraction_metrics(
            structured, registered, aliases, report_off_catalog=True
        )
        if intent in _METADATA_INTENTS:
            return [], off_catalog
        return off_catalog, []

    @staticmethod
    def _retry_feedback(
        off_data: list[str],
        off_meta: list[str],
        *,
        empty_extraction: bool,
    ) -> str | None:
        """重试时附带的纠错说明：指出问题，给两条合法出路，不点名该选哪个指标。"""
        parts: list[str] = []
        if empty_extraction:
            parts.append(
                "上一轮没有输出任何结构化提取参数。该问题属于业务问题，"
                "必须按结构化提取规范完整输出参数；仅当问题确与业务无关时才允许输出null。"
            )
        if off_data:
            parts.append(
                "指标名“"
                + "、".join(off_data)
                + "”不在注入规范指标层级表中。确属某个登记指标的同义词时，"
                "输出该指标的登记原名；与任何登记指标都对应不上时，指标通道置空，"
                "不得用语义相近的指标顶替，不得自造指标名。"
            )
        if off_meta:
            parts.append(
                "指标名“"
                + "、".join(off_meta)
                + "”不在注入规范指标层级表中。若该说法可由规范内指标派生，"
                "按正常任务意图处理并输出派生来源指标的登记原名，派生口径写入“输出要求”；"
                "仅当该说法与规范内任何指标都无关、也无法派生时，才按指标口径解释处理"
                "（意图输出METRIC_DEFINITION，指标名保留用户原文）。"
            )
        return "\n".join(parts) if parts else None

    async def _task_model_json(self, name, system_content, user_content, *, timeout=None):
        """One model operation, shared transport/settings and no public I/O change."""
        body = {
            'model': self.settings.intent_model_name,
            'messages': [{'role': 'system', 'content': system_content},
                         {'role': 'user', 'content': user_content}],
            'temperature': 0, 'enable_thinking': False,
        }
        headers = {'Authorization': 'Bearer ' + self.settings.intent_model_api_key.get_secret_value(),
                   'Content-Type': 'application/json'}
        with trace_generation(name=name, model=body['model'], messages=body['messages']) as generation:
            async with httpx.AsyncClient(base_url=self.settings.intent_model_base_url.rstrip('/'),
                    timeout=timeout or self.settings.multi_question_model_timeout_seconds, transport=self._transport) as client:
                response = await client.post('/chat/completions', headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
                generation.set_response(payload)
        content = payload['choices'][0]['message'].get('content')
        if not content:
            raise ValueError(name + ' returned empty content')
        result = json.loads(_strip_json_fences(content))
        if not isinstance(result, dict):
            raise ValueError(name + ' must return a JSON object')
        return result

    async def _model_decompose(
        self,
        question: str,
        *,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        retry_feedback: str | None = None,
    ) -> QuestionDecomposition:
        from app.domain.semantic_description import load_semantic_description

        from app.services.general_rules import rule_section, without_rule_section
        general_rules = rule_section(semantic_context)
        semantic_context = without_rule_section(semantic_context)
        # 上一轮任务表块不是业务语义规范：远程规范存在时上面会跳过
        # semantic_context 的注入，追问轮的复用规则与上轮采样不能跟着
        # 一起被丢掉，单独拆出来无条件送达
        prior_block = ""
        prior_marker = "[上一轮任务表]"
        if prior_marker in semantic_context:
            prior_start = semantic_context.index(prior_marker)
            prior_block = semantic_context[prior_start:]
            semantic_context = semantic_context[:prior_start].rstrip()

        system_content = ""
        # 通用规则块放在前部：贴着Schema末尾时会干扰指标选择，
        # 使模型在多个可对应的登记指标之间选错（实测多次复现）
        system_content += general_rules
        # 规范只注入一次：远程按模型生成的描述文件优先，
        # 平台提示词粘贴段兼容存量，提示词文件内置规范最后兜底。
        reference = await load_semantic_description(self.settings, semantic_model_id)
        remote_spec = (
            reference["content"]
            if reference["source"].startswith("semantic_model_")
            else ""
        )
        if remote_spec:
            system_content += _semantic_context_section(
                remote_spec, origin="语义模型描述文件"
            )
            spec_for_catalog = remote_spec
        elif semantic_context.strip():
            system_content += _semantic_context_section(semantic_context)
            spec_for_catalog = semantic_context
        elif semantic_model_id is None:
            # 完全没有模型标识时才用静态内置规范兜底；
            # 指定了模型ID但远程文件拉取失败时，不能用医药域旧快照污染其他业务域。
            embedded_spec = extraction_embedded_spec()
            spec_for_catalog = embedded_spec or ""
            if embedded_spec:
                system_content += (
                    "\n平台业务语义规范（结构化提取与参数口径的判定依据）：\n"
                    + embedded_spec + "\n"
                )
        else:
            spec_for_catalog = ""
        if prior_block:
            system_content += "\n" + prior_block + "\n"
        metric_catalog = _parse_metric_catalog(spec_for_catalog)
        from app.planning.task_plan_stages import (
            PLANNING_PROMPT, PARAMETER_PROMPT, TaskBoundaries,
            complete_boundaries, parameter_items, registered_metric_conditions,
            registered_metric_predicates, metric_definition, component_task_title,
        )
        dependencies = _parse_metric_dependencies(spec_for_catalog)
        planning_question = question
        if retry_feedback:
            planning_question += "\n上一轮需要修正：" + retry_feedback
        planned = await self._task_model_json(
            "single-question-task-plan",
            PLANNING_PROMPT + system_content + "\nJSON Schema："
            + json.dumps(TaskBoundaries.model_json_schema(), ensure_ascii=False),
            planning_question,
        )
        boundary_plan_complete = bool(planned.get('tasks')) and all(
            isinstance(item, dict) and 'extraction' not in item for item in planned.get('tasks', []))
        # The legacy top-level single form is accepted as a boundary only;
        # its combined extraction is deliberately ignored until phase two.
        if not planned.get('tasks'):
            if 'extraction' not in planned and ('intent' not in planned or 'split' in planned):
                raise ValueError('任务规划没有返回任务列表')
            planned = {**planned, 'tasks': [{'question': question}]}
        boundaries = complete_boundaries(TaskBoundaries.model_validate(planned).tasks,
            spec=spec_for_catalog, catalog=metric_catalog, dependencies=dependencies, source_question=question,
            default_times=_registered_default_times(question,spec_for_catalog))
        parameter_request = json.dumps({
            'original_question': question,
            'tasks': [dict(task_index=index, **task.model_dump(exclude_none=True))
                      for index, task in enumerate(boundaries)],
        }, ensure_ascii=False)
        # Intent is returned with extraction; no separate intent model call.
        extracted = await self._task_model_json(
            "single-question-task-parameters",
            _SINGLE_SYSTEM_PROMPT + system_content + '\n已规划任务JSON：\n' + parameter_request,
            question,
            timeout=max(self.settings.multi_question_model_timeout_seconds,
                        min(60, self.settings.multi_question_model_timeout_seconds * len(boundaries))),
        )
        try:
            parameters = parameter_items(extracted, boundaries)
        except ValueError as exc:
            raise TaskInputBindingError(str(exc)) from exc
        question_intent = self._sanitize_intent(extracted.get('intent') or planned.get('intent'))
        raw_tasks = []
        for boundary, parameters_for_task in zip(boundaries, parameters):
            value = copy.deepcopy(parameters_for_task)
            if isinstance(value, dict):
                if boundary._cohort_key:
                    value['维度'] = [boundary._cohort_key]
                    value['展示字段'] = [{'field':boundary._cohort_key}]
                    value['是否去重'] = '是'
                    value['排序'] = []
                    value['限制'] = None
                if boundary._whole_object_scope:
                    key, _, member = boundary._whole_object_scope
                    # Eligibility has its own complete-key query. Inheriting
                    # its row filters here would turn a whole-object statistic
                    # back into a matching-member statistic. Runtime injects
                    # the verified predecessor keys through the existing ASL
                    # dependency constraint, never via guessed placeholders.
                    value['过滤条件'] = []
                    value['维度'] = [key]
                    value['展示字段'] = [{'field':key}]
                    value['实体'] = [item for item in value.get('实体') or [] if item != member]
                    value['时间粒度'] = {'unit':None,'time_range':None,'anchor':None}
                if boundary.component_input:
                    value['意图'] = '指标查询'
                    if not boundary.preserve_ranking:
                        value['排序'] = []
                        value['限制'] = None
                    # A formula input is an aggregate measure, not an extra
                    # grouping column. Preserve keys and unrelated display
                    # attributes (e.g. salesperson's company).
                    definitions=[metric_definition(spec_for_catalog,name) for name in boundary.metric_targets]
                    display=[]
                    for item in value.get('展示字段') or []:
                        field=str(item.get('field') or item.get('name') or '') if isinstance(item,dict) else str(item)
                        canonical=_normalize_metric_name(field,*metric_catalog)
                        is_input=(canonical in boundary.metric_targets or any(field and field in text for text in definitions))
                        if field in boundary.grain or not is_input:
                            display.append(item)
                    value['展示字段']=display
                    for condition in value.get('过滤条件') or []:
                        if not isinstance(condition, dict):
                            continue
                        field = str(condition.get('field') or '')
                        canonical = _normalize_metric_name(field, *metric_catalog)
                        if canonical in metric_catalog[0] and any(
                                field and field in rule for rule in boundary.sample_requirements):
                            # Parent sample rules refer to aggregated measures,
                            # not similarly named source-row attributes.
                            condition['field'] = canonical
                    sample_metrics=[name for parent in boundaries for name in parent.metric_targets
                        if name in dependencies and metric_definition(spec_for_catalog,name) in boundary.sample_requirements]
                    predicates=registered_metric_predicates(spec_for_catalog,sample_metrics,metric_catalog)
                    conditions=value.get('过滤条件') or []
                    for predicate in predicates:
                        matched=False
                        for condition in conditions:
                            if not isinstance(condition,dict):
                                continue
                            field=_normalize_metric_name(str(condition.get('field') or ''),*metric_catalog)
                            actual=condition.get('value');actual=actual if isinstance(actual,list) else [actual]
                            if (field==predicate['field'] and condition.get('op')==predicate['op']
                                    and len(actual)==1 and str(actual[0])==str(predicate['value'][0])):
                                condition.update(copy.deepcopy(predicate));matched=True
                        if not matched:
                            conditions.append(copy.deepcopy(predicate))
                    value['过滤条件']=conditions
                if boundary_plan_complete and '不再查询数据库' not in boundary.question:
                    value.pop('计算绑定', None)
                    value.pop('结果操作', None)
                    if boundary.depends_on:
                        value['是否查询数据库'] = '是'
                elif boundary_plan_complete and not boundary.postprocess and any(
                        name in dependencies for name in boundary.metric_targets):
                    # The registered division is already planned against
                    # aggregate inputs. Do not run a second, unplanned raw-row
                    # pipeline invented during parameter extraction.
                    value.pop('结果操作', None)
                required = [name for name in boundary.metric_targets if name in metric_catalog[0]]
                if required or (boundary_plan_complete and not boundary.metric_targets):
                    # A declared output measure remains a measure even when
                    # its rows are presented as a detail result.
                    value['指标'] = [{'name': name} for name in required]
                    # Detail fields already come from parameter extraction.
                    # A planning grain such as "医院" is an entity, not an
                    # additional display field called "医院".
                    if required or not value.get('展示字段'):
                        existing = value.get('维度') or []
                        value['维度'] = existing + [name for name in boundary.grain if name not in existing]
                if boundary.expected_output:
                    requirement = str(value.get('输出要求') or '').strip()
                    if boundary.expected_output not in requirement:
                        value['输出要求'] = (requirement + '；' if requirement else '') + boundary.expected_output
            planned_task = boundary.model_dump(exclude={
                'metric_targets', 'grain', 'sample_requirements', 'component_input', 'preserve_ranking'})
            if isinstance(value, dict):
                planned_task['name'] = component_task_title(boundary, value)
            raw_tasks.append(_PlannedTask(**planned_task, extraction=value))
        tasks: list[DecomposedTask] = []
        for task_index, item in enumerate(raw_tasks):
            task_question = item.question.strip() or question
            task_intent = self._sanitize_intent(
                str((item.extraction or {}).get(_EXTRACTION_INTENT_KEY) or '')
            ) or question_intent
            structured = self._sanitize_extraction(
                item.extraction, intent=task_intent,
                metric_catalog=metric_catalog,
                required_metrics=tuple(boundaries[task_index].metric_targets),
            )
            if item.clarification:
                structured = structured or {}
                structured['计算待确认'] = copy.deepcopy(item.clarification)
                structured.pop('结果操作', None)
            elif item.postprocess:
                structured = structured or {}
                structured.pop('计算绑定', None)
                structured['结果操作'] = copy.deepcopy(item.postprocess)
            _ground_location_owners(structured, task_question)
            _ground_inferred_hospital_location(structured, question, semantic_model_id)
            task_question = _confirmed_hospital_department_scope(
                task_question, question, structured, semantic_model_id
            )
            if structured is not None and task_intent in {
                None, *_CHINESE_INTENT_TO_PRIMARY.values(),
            }:
                # 提取JSON里的中文意图键是最终展示契约，可回填任务意图
                task_intent = _CHINESE_INTENT_TO_PRIMARY.get(
                    str(structured.get(_EXTRACTION_INTENT_KEY) or "").strip(),
                    task_intent,
                )
            if (
                structured is not None
                and "不再查询数据库" in task_question
                and not str(structured.get(_EXTRACTION_INTENT_KEY) or "").strip()
            ):
                # 计算/筛选任务的意图键固定为指标计算，模型漏填时补上
                structured[_EXTRACTION_INTENT_KEY] = "指标计算"
            if "不再查询数据库" in task_question:
                # 计算/筛选任务基于前序任务结果，意图固定为对比分析
                task_intent = PrimaryIntent.COMPARISON_ANALYSIS
                if structured is not None:
                    structured[_EXTRACTION_INTENT_KEY] = '指标计算'
                    structured['是否查询数据库'] = '否'
            elif (
                task_intent is PrimaryIntent.COMPARISON_ANALYSIS
                and _period_change_series_question(task_question)
            ):
                # 环比/同比落在多期时间序列上时属于趋势链路的逐期计算，
                # 模型按“对比”字面归入对比分析会走两期引擎并拒算。
                # 计算任务不适用本改判（上一分支已固定意图）。
                task_intent = PrimaryIntent.TREND_ANALYSIS
                if structured is not None and str(
                    structured.get(_EXTRACTION_INTENT_KEY) or ""
                ).strip() == "对比分析":
                    structured[_EXTRACTION_INTENT_KEY] = "趋势分析"
                logger.info(
                    "period-change intent rerouted to TREND_ANALYSIS: question=%s",
                    task_question[:120],
                )
            if task_intent is PrimaryIntent.TREND_ANALYSIS:
                # 趋势引擎按相邻行计算逐期变化，时间乱序直接拒算，
                # 排序统一在提取出口归一为时间升序
                _normalize_trend_time_order(structured)
            # 口径/血缘解释问题保留完整参数；若模型仍输出null提取，则至少把
            # 原文指标名带进参数通道，否则下游解析不到指标，追问门禁会引导
            # 用户换成规范里的指标，违背“不得用语义相近指标替代”的约束。
            metadata_parameters: tuple[str, ...] = ()
            if task_intent in _METADATA_INTENTS:
                raw_metrics = (
                    item.extraction.get("指标")
                    if isinstance(item.extraction, dict)
                    else None
                ) or []
                metadata_parameters = tuple(
                    extraction_to_parameters({"指标": raw_metrics})
                )
            parameters = (
                tuple(extraction_to_parameters(structured)) or metadata_parameters
            )
            tasks.append(DecomposedTask(
                question=task_question,
                depends_on=tuple(
                    dep for dep in item.depends_on
                    if isinstance(dep, int) and 0 <= dep < len(raw_tasks)
                ),
                expected_output=(
                    item.expected_output.strip()
                    if item.expected_output and item.expected_output.strip()
                    else None
                ),
                intent=task_intent,
                parameters=parameters,
                structured=structured,
                name=(
                    item.name.strip()
                    if item.name and item.name.strip()
                    else None
                ),
            ))
        dependencies = _parse_metric_dependencies(spec_for_catalog)
        required = list(dict.fromkeys(
            item.get('name') if isinstance(item, dict) else item
            for task in tasks if task.intent not in _METADATA_INTENTS
            for item in (task.structured or {}).get('指标') or []
            if (item.get('name') if isinstance(item, dict) else item) in dependencies
            and not (task.structured or {}).get('计算绑定')
            and not (task.structured or {}).get('结果操作')))
        if required:
            from app.planning.calculation_bindings import resolve_compound_formulas
            from app.planning.task_plan_stages import canonical_formula_spec
            formulas = await resolve_compound_formulas(self.settings, self._transport,
                canonical_formula_spec(spec_for_catalog, metric_catalog), dependencies, required)
            for task in tasks:
                structured = task.structured or {}
                for item in structured.get('指标') or []:
                    name = item.get('name') if isinstance(item, dict) else item
                    if name not in formulas or structured.get('计算绑定'):
                        continue
                    binding = copy.deepcopy(formulas[name])
                    if database_query_choice(structured) is False or '不再查询数据库' in task.question:
                        for role in ('numerator', 'denominator'):
                            column = binding[role]['column']
                            sources = [index for index in task.depends_on
                                if any((metric.get('name') if isinstance(metric, dict) else metric) == column
                                       for metric in (tasks[index].structured or {}).get('指标') or [])]
                            if len(sources) != 1:
                                raise ValueError('计算公式无法唯一绑定成分任务')
                            binding[role]['source_task_index'] = sources[0]
                    structured['计算绑定'] = binding
        return QuestionDecomposition(
            intent=question_intent,
            tasks=tuple(tasks),
            metric_catalog=metric_catalog,
            metric_dependencies=dependencies,
            grounding_default_times=_registered_default_times(question, spec_for_catalog),
            grounding_registered_conditions=registered_metric_conditions(spec_for_catalog,
                [name for boundary in boundaries for name in boundary.metric_targets], metric_catalog),
            shared_region_denominator=_shared_region_denominator_from_spec(spec_for_catalog),
            boundary_plan_complete=boundary_plan_complete,
        )

    @staticmethod
    def _sanitize_intent(value: str | None) -> PrimaryIntent | None:
        """模型输出的意图字符串归一成枚举，非法取值直接丢弃回退规则分类。"""
        if not value:
            return None
        chinese = {**_CHINESE_INTENT_TO_PRIMARY,
                   '解释口径': PrimaryIntent.METRIC_DEFINITION,
                   '数据血缘': PrimaryIntent.DATA_LINEAGE,
                   '闲聊': PrimaryIntent.CHAT,
                   '非数据任务': PrimaryIntent.OUT_OF_SCOPE}.get(value.strip())
        if chinese is not None:
            return chinese
        normalized = value.strip().upper()
        try:
            return PrimaryIntent(normalized)
        except ValueError:
            return None

    @classmethod
    def _sanitize_extraction(
        cls,
        value: dict[str, Any] | None,
        *,
        intent: PrimaryIntent | None,
        metric_catalog: tuple[frozenset[str], dict[str, str]] | None = None,
        required_metrics: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """清洗模型输出的结构化提取：纯闲聊类意图强制为空，键与取值做形态归一。

        口径/血缘解释任务保留完整提取：参数照常进mentions通道供下游解析，
        展示层单独决定是否输出，指标不存在时回复“未定义”仍依赖这些参数。
        """
        if not isinstance(value, dict):
            return None
        if intent is not None and intent in _NO_DATA_INTENTS:
            return None
        structured: dict[str, Any] = {}
        intent_label = _clean_extraction_text(value.get(_EXTRACTION_INTENT_KEY) or "")
        if intent == PrimaryIntent.METRIC_DEFINITION:
            # 口径解释不在六类查询枚举里，意图键固定与任务意图显示一致
            structured[_EXTRACTION_INTENT_KEY] = "解释口径"
        elif intent == PrimaryIntent.DATA_LINEAGE:
            structured[_EXTRACTION_INTENT_KEY] = "数据血缘"
        else:
            structured[_EXTRACTION_INTENT_KEY] = (
                intent_label if intent_label in _CHINESE_INTENT_LABELS else None
            )
        for key in _EXTRACTION_LIST_KEYS:
            raw = value.get(key)
            items: list[Any] = []
            if isinstance(raw, list):
                for item in raw[:_EXTRACTION_LIST_MAX]:
                    cleaned = _clean_extraction_item(item)
                    if cleaned is not None and cleaned != "":
                        items.append(cleaned)
            elif isinstance(raw, (str, dict)):
                cleaned = _clean_extraction_item(raw)
                if cleaned is not None and cleaned != "":
                    items.append(cleaned)
            structured[key] = items
        if intent_label == "明细查询" or (
            intent == PrimaryIntent.DETAIL_QUERY
            and intent_label not in _CHINESE_INTENT_LABELS
        ):
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
                        _normalize_metric_name(ref_name, registered, aliases)
                    )
            for key in ("指标", "维度"):
                kept: list[Any] = []
                for item in structured[key]:
                    name = (
                        str(item.get("name") or item.get("field") or "").strip()
                        if isinstance(item, dict) else str(item).strip()
                    )
                    normalized = (
                        _normalize_metric_name(name, registered, aliases)
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
        structured["输出要求"] = (
            _clean_extraction_text(output_requirement)
            if isinstance(output_requirement, str) and output_requirement.strip()
            else None
        )
        dedup = _clean_extraction_text(value.get("是否去重") or "")
        structured["是否去重"] = dedup if dedup in ("是", "否") else None
        query_choice = database_query_choice(value)
        structured["是否查询数据库"] = (
            "是" if query_choice is True else "否" if query_choice is False else None
        )
        for key in ('计算绑定', '结果操作', '计算待确认'):
            if isinstance(value.get(key), dict) and value[key]:
                structured[key] = copy.deepcopy(value[key])
        return structured
