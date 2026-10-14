"""单一问题链路：任务意图识别与结构化参数提取。

职责边界：
- 本模块只处理“一个可独立验收的业务目标”：意图识别、参数提取、
  指标名归一与带反馈重试；
- “是否拆分、拆成什么子问题”的前置判断与多任务规划在
  compound_question.py
- 本模块同时承载两条链路共用的基础件：提取规范加载、extraction 清洗、
  指标目录解析、语义规范注入。
"""

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import PlannerExtraction, PrimaryIntent

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

_INTENT_PROMPT_OPTIONS = (
    "METRIC_QUERY（指标查询）, DETAIL_QUERY（明细/名单查询）, "
    "TREND_ANALYSIS（趋势分析）, COMPARISON_ANALYSIS（对比分析）, "
    "COMPOSITION_ANALYSIS（占比构成分析）, ANOMALY_ANALYSIS（异常分析）, "
    "ROOT_CAUSE_ANALYSIS（归因分析）, FORECAST_ANALYSIS（预测分析）, "
    "REPORT_GENERATION（报告生成）, METRIC_DEFINITION（指标口径解释）, "
    "CHAT（闲聊）, OUT_OF_SCOPE（超出业务范围）"
)

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
    "趋势分析": PrimaryIntent.TREND_ANALYSIS,
    "对比分析": PrimaryIntent.COMPARISON_ANALYSIS,
    "占比分析": PrimaryIntent.COMPOSITION_ANALYSIS,
    "排名分析": PrimaryIntent.METRIC_QUERY,
    "明细查询": PrimaryIntent.DETAIL_QUERY,
}
_CHINESE_INTENT_LABELS = frozenset(_CHINESE_INTENT_TO_PRIMARY)

_EXTRACTION_PROMPT_PATH = Path(__file__).with_name("structured_extraction_prompt.txt")
_EXTRACTION_PROMPT_MARKER = "【用户提示词（业务场景上下文）】"
_EXTRACTION_ROLE_GUIDANCE = """
结构化提取的角色边界：
实体表示本次查询涉及或需要关联的逻辑业务对象/表，不是具体筛选值。
例如实体中的“商品”“经销商”表示商品表、经销商表；不能生成商品名称=商品、经销商名称=经销商。
指标是计算口径，维度是分组方向，展示字段是返回列，均不能仅因被提取就转成值过滤。
只有用户给出的具体对象值（例如万益特、贝朗、具体商品名）才进入过滤条件。
同一字段的多个可选值保留 IN，排除多个值保留 NOT IN，不能拆成同时满足的多个等号。
用户消息中的业务语义说明仅用于理解角色与业务含义；不是授权清单，也不是物理 JOIN 指令。
说明有重复、矛盾或关联描述不一致时，不据此编造字段或关系，最终由当前授权语义目录绑定。
条件归属必须逐项依据当前问题，而非目录属性排列或默认主表：上海市各经销商→entity=经销商；
上海地区哪个医院→entity=医院；上海医院按经销商汇总→entity=医院。不同实体的所在地条件分开保留。
名称条件保留业务名称字段和用户原值；不能因为目录只在医院/经销商下列出“关联省份ID”就把上海填进ID。
地区名称用field=省份名称或城市名称并单独标明所属业务entity，名称到外键编码的转换交给ASL按已发布关系完成。
同理，公司名称、商品名称等不能填到关联编码字段；只有用户明确给出编码时才提取编码条件，不猜测编码。
医院的消化内科、呼吸内科等临床科室属于科室实体（field=科室名称），不是销售订单的业务科室/业务组织；只有原文明确要求企业业务组织时才提取业务科室条件。
"""


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
        "但规范指标层级中各指标的同义词视为该指标的等价说法：用户说法是登记指标的同义词或派生口径"
        "（对指标求增长率、均值、占比、排名、最高最低等）时，按正常任务意图处理，"
        "extraction的指标名输出该指标的登记原名，派生口径写入“输出要求”键；"
        "说法与规范内任何指标都无关、也无法由规范内指标派生时，才按指标口径解释处理"
        "（意图输出METRIC_DEFINITION，指标名保留用户原文）。"
        "判断是否“无法派生”按业务语义判断，不要求字面上包含指标名：围绕规范内"
        "实体或业务对象的度量（数量、金额、快慢、高低、升降、排名）提出的说法，"
        "都可由该业务对象在规范内的指标派生；只有规范中连对应业务对象的度量都"
        "不存在的纯概念词，才按指标口径解释处理：\n"
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


_EXTRACTION_SCHEMA_DESCRIPTION = (
    "结构化提取JSON，严格按系统提示词中的结构化提取规范输出，"
    "固定键：意图/业务域/实体/指标/维度/展示字段/过滤条件/时间粒度/排序/限制/输出要求/是否去重/是否查询数据库；"
    "意图取值限定为 统计查询、趋势分析、对比分析、占比分析、排名分析、明细查询 之一。"
    "数据查询任务的指标名只能输出注入规范指标层级表中登记的原名"
    "（用户说法是同义词或派生口径时归一为登记原名，派生口径写入“输出要求”），不得自造指标名；"
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


class _SinglePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str | None = Field(
        default=None,
        max_length=40,
        description="必填；问题意图，取值为系统给定的意图枚举之一",
    )
    tasks: list[_PlannedTask] = Field(
        default_factory=list,
        max_length=4,
        description="任务拆解结果，1到4个；绝大多数问题只输出1个任务",
    )
    extraction: dict[str, Any] | None = Field(
        default=None,
        description="单任务时可直接输出的结构化提取JSON；已输出tasks时忽略本字段",
    )


_SINGLE_SYSTEM_PROMPT = """你是数据智能体的任务拆解与参数提取器。只输出符合JSON Schema的JSON，不回答用户问题，不调用工具。

你的职责：把问题拆解成可执行任务（绝大多数问题只有1个任务），识别问题意图，并按末尾的结构化提取规范逐任务输出参数。问题是否为复合问题已由前置环节判断——你收到的每个问题都是一个可独立验收的业务目标，不要把多个业务目标合成一个任务，也不要丢弃问题中的任何条件。

任务拆解规则：
1. 默认输出1个任务，任务问题即用户问题本身。
2. 明细查询（名单/明细/清单类返回）带指标阈值条件（如“订单笔数大于5笔”“合作时长大于3个月”“销售额大于100万”）时，阈值不能作用在明细行上，拆为2个任务：任务1按该指标做分组统计（写明分组维度和指标，保留原问题的地区、产品、时间等其他筛选条件），任务2依赖任务1，任务问题写明“根据上述任务的查询结果筛选×××，不再查询数据库”。
3. 先排名或筛选出对象、再查询这些对象的其他关系或明细时（如“找出销售额下降最大的五个产品，并列出这些产品涉及的经销商和医院”），拆为2个任务：任务1排名或筛选，任务2依赖任务1并写明“根据上述任务的查询结果……”。任务2要执行新的数据库查询，问题里不要写“不再查询数据库”——该句只允许出现在基于前序结果直接计算或筛选、不再发任何新查询的任务里。
4. 用户明确要求分别查询多个结果再比较或合并计算时（如“分别查询今年和去年销售额，并比较同比变化”），按查询数拆任务，再加一个依赖全部前序任务的计算任务，任务问题写明“根据上述任务的查询结果……不再查询数据库”。
5. 业务语义规范指标表中某指标的依赖指标列登记了成分指标（取值不是"无"或空）时，该指标必须按公式成分拆分：依赖指标列里的每个成分指标各一个查询任务（指标名用该成分的规范登记原名，筛选条件按规范中该成分的口径与公式规则归属，分组维度只放在需要按维度分组的成分任务上），最后再加一个计算任务，依赖全部成分任务，问题写明"根据上述任务的查询结果计算×××，不再查询数据库"。对复合指标做趋势、占比、对比等分析时同样先按成分拆分，分析口径写进成分任务与计算任务的问题文本。依赖指标列为无、或成分指标不在规范指标表内时不拆；依赖指标列为无的普通指标的趋势、占比、对比分析，以及围绕同一份数据的分析都保持1个任务。
6. 不要把ASL、SQL、取数、校验、洞察这些内部步骤拆成任务。
7. 每个任务都要输出name字段：任务名称是"动词+业务指标+过滤条件"的陈述句（如"统计2025年上海市各经销商的含税销售总额""计算2025年上海市平均每单销售金额""查询订单笔数大于5笔的经销商名单"），用规范登记的指标、维度或实体名概括任务要得到的业务结果，不得照抄用户问题原文，不得出现"是多少""有哪些"等疑问词，结尾不加标点；计算任务的name直接写"计算×××"，不用带"根据上述任务的查询结果"前缀。

意图判断规则（问题级intent字段）：
1. 从下列意图枚举中选一个最贴合问题目标的取值，只输出枚举名本身。不需要查询业务数据的问题（闲聊、超出业务范围）按实际选CHAT或OUT_OF_SCOPE，extraction输出null。
2. 指标说法与规范指标层级比对后分两种情况：说法由规范内指标派生（对规范内指标求增长率、均值、占比、排名、最高最低等派生口径），按正常任务意图处理并完整提取参数，extraction的指标名输出派生来源的规范指标登记原名（按规范中各指标的登记名、同义词和业务语义选择最贴合的一个，不得自造指标名），派生口径（如"按环比增长率降序取排名"）写入"输出要求"键；说法与规范内任何指标都无关、也无法由规范内指标派生时，才按指标口径解释处理：任务意图输出METRIC_DEFINITION，参数仍照常完整提取，指标名保留用户原文，不得用语义相近的指标替代，也不得自行编造计算口径。判断是否"无法派生"按业务语义判断，不要求字面上包含指标名：围绕规范内实体或业务对象的度量（数量、金额、快慢、高低、升降、排名）提出的说法，都可由该业务对象在规范内的指标派生，按正常任务意图处理；只有规范中连对应业务对象的度量都不存在的纯概念词，才按指标口径解释处理。
3. 任务问题含"不再查询数据库"时，这是基于前序任务结果的计算/筛选任务，不是闲聊：该任务照常输出extraction，指标取被计算的指标名（保留原文叫法），"意图"键固定输出"指标计算"，过滤条件、时间粒度、维度按任务问题文本提取，识别不到的键置[]或null。

数据查询问题的extraction字段：按本提示词末尾的"结构化提取规范"产出结构化提取JSON（固定键：意图/业务域/实体/指标/维度/展示字段/过滤条件/时间粒度/排序/限制/输出要求），只提取用户原文和注入的业务语义规范中出现的内容，识别不到的键按规范置[]或null。数据查询问题的指标名只输出规范指标层级表中的登记原名（用户说法是同义词或派生口径时归一为登记原名，派生口径写入"输出要求"），不得自造指标名。排序的field同样只输出规范登记的指标名或维度名：增长率、占比、排名等派生口径不作为排序字段，按哪个指标排就把该指标的登记原名放进field，派生口径写入"输出要求"。指标口径解释问题（METRIC_DEFINITION）的extraction按规范完整提取参数并保留用户原文指标名，"意图"键固定输出"解释口径"。

任务意图枚举：
""" + _INTENT_PROMPT_OPTIONS + """

判定示例：
- “查看上海各经销商科室业务匹配度。” → 意图METRIC_DEFINITION：科室业务匹配度与规范内任何指标都无关且无法派生，按指标口径解释处理并保留原文指标名，参数照常完整提取（地区、经销商等照常输出）；不得改用规范内指标替代。
- “统计各经销商近12月的平均月度销量增长率。” → 意图METRIC_QUERY：平均月度销量增长率由规范内指标“销量”派生，完整提取参数（时间范围、维度等），extraction的指标名输出规范登记原名“销量”，“计算平均月度增长率”写入“输出要求”。
"""

# 结构化提取规范整段取自国药语义解析器提示词，extraction字段按此输出。
_SINGLE_SYSTEM_PROMPT += (
    "\n结构化提取规范（extraction字段严格按此规范输出，"
    "意图取值限定为规范中的中文枚举，业务语义规范以下方注入内容为准）：\n"
    + extraction_parser_prompt()
)


class SingleQuestionPlanner:
    """单一问题链路：任务意图识别 + 结构化参数提取 + 指标名归一与重试。"""

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
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                # 传输、Schema 或解析失败先整体重试一次，仍失败返回空结果
                logger.warning(
                    "single planner decompose failed attempt=%s error=%r",
                    attempt, exc,
                )
                if attempt == attempts - 1:
                    return last_result
                continue
            last_result = decomposition
            off_data, off_meta = self._normalize_decomposition_metrics(decomposition)
            unsplit_compound = self._unsplit_compound_metrics(decomposition)
            empty_extraction = (
                not any(task.structured is not None for task in decomposition.tasks)
                and decomposition.intent not in _NO_DATA_INTENTS
            )
            if attempt == 0 and (empty_extraction or off_data or off_meta or unsplit_compound):
                # 全部任务提取为空、指标名目录外或复合指标未按公式拆分，
                # 说明这次输出不可信，带纠错说明重试一次
                retry_feedback = self._retry_feedback(
                    off_data, off_meta, empty_extraction=empty_extraction,
                    unsplit_compound=unsplit_compound,
                )
                continue
            return last_result
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
        unsplit_compound: list[tuple[str, tuple[str, ...]]] | None = None,
    ) -> str | None:
        """重试时附带的纠错说明：指出问题，给两条合法出路，不点名该选哪个指标。"""
        parts: list[str] = []
        if empty_extraction:
            parts.append(
                "上一轮没有输出任何结构化提取参数。该问题属于业务问题，"
                "必须按结构化提取规范完整输出参数；仅当问题确与业务无关时才允许输出null。"
            )
        if unsplit_compound:
            for metric, components in unsplit_compound:
                parts.append(
                    "指标“" + metric + "”在规范指标表的依赖指标列登记了成分指标（"
                    + "、".join(components)
                    + "），上一轮把它当成了单个查询任务。必须按任务拆解规则5拆分："
                    "每个成分指标一个查询任务（指标名用该成分的登记原名，"
                    "筛选条件按规范中该成分的口径归属），最后再加一个依赖全部成分任务的"
                    "计算任务，问题写明“根据上述任务的查询结果计算" + metric
                    + "，不再查询数据库”。"
                )
        if off_data:
            parts.append(
                "指标名“"
                + "、".join(off_data)
                + "”不在注入规范指标层级表中。数据查询问题的指标名只能输出表中登记的原名；"
                "若用户说法是某个登记指标的同义词或派生口径，输出该指标的登记原名，"
                "派生口径写入“输出要求”。"
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

        schema = _SinglePlan.model_json_schema()
        system_content = _SINGLE_SYSTEM_PROMPT
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
        system_content += general_rules
        metric_catalog = _parse_metric_catalog(spec_for_catalog)
        body = {
            "model": self.settings.intent_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_content + _EXTRACTION_ROLE_GUIDANCE + "\nJSON Schema：" + json.dumps(
                        schema, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                {"role": "user", "content": (
                    question
                    if retry_feedback is None
                    else question + "\n（上一轮输出校验未通过，请按下述说明修正后重新输出：\n"
                    + retry_feedback + "）"
                )},
            ],
            "temperature": 0,
            # json_object 与 enable_thinking=false 同开时模型会丢过滤条件的
            # 取值，因此只关思考；输出格式由 Schema 约束。
            "enable_thinking": False,
        }
        headers = {
            "Authorization": (
                "Bearer " + self.settings.intent_model_api_key.get_secret_value()
            ),
            "Content-Type": "application/json",
        }
        with trace_generation(
            name="single-question-extract",
            model=body.get("model"),
            messages=body.get("messages"),
        ) as generation:
            async with httpx.AsyncClient(
                base_url=self.settings.intent_model_base_url.rstrip("/"),
                timeout=self.settings.multi_question_model_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/chat/completions", headers=headers, json=body
                )
                response.raise_for_status()
                payload = response.json()
                generation.set_response(payload)
        content = payload["choices"][0]["message"].get("content")
        if not content:
            raise ValueError("single question extractor returned empty content")
        result = _SinglePlan.model_validate(json.loads(_strip_json_fences(content)))
        question_intent = self._sanitize_intent(result.intent)
        raw_tasks: list[_PlannedTask] = list(result.tasks)
        if not raw_tasks:
            # 模型走旧版单任务直出（顶层extraction）时归一成单任务
            raw_tasks = [_PlannedTask(question=question, extraction=result.extraction)]
        tasks: list[DecomposedTask] = []
        for item in raw_tasks:
            task_question = item.question.strip() or question
            task_intent = question_intent
            structured = self._sanitize_extraction(
                item.extraction, intent=task_intent
            )
            _ground_location_owners(structured, task_question)
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
        return QuestionDecomposition(
            intent=question_intent,
            tasks=tuple(tasks),
            metric_catalog=metric_catalog,
            metric_dependencies=_parse_metric_dependencies(spec_for_catalog),
        )

    @staticmethod
    def _sanitize_intent(value: str | None) -> PrimaryIntent | None:
        """模型输出的意图字符串归一成枚举，非法取值直接丢弃回退规则分类。"""
        if not value:
            return None
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
        return structured
