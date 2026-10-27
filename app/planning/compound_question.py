"""复合问题链路：拆分前置判断与多任务规划。

与单一问题链路（task_dag.SingleQuestionPlanner）职责隔离：
- 本模块独占“是否拆分、拆成什么子问题”的判断职责，判断发生在意图识别阶段；
- 拆出的每个子问题交给 SingleQuestionPlanner 独立做意图识别与参数提取，
  复合问题的意图和业务域因此按子问题分别产出；
- 拆分模型不可用时有确定性规则兜底，兜底拆不出时按单一问题处理。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain.models import (
    AtomicTask,
    PlannerExtraction,
    PrimaryIntent,
    QuestionTaskGroup,
    TaskPlan,
)
from app.observability.langfuse_client import ensure_structural_span, trace_generation
from app.planning.calculation_targets import declared_calculation_targets
from app.planning.task_dag import (
    DecomposedTask,
    PlannerOutcome,
    QuestionDecomposition,
    SingleQuestionPlanner,
    TaskPlanningError,
    _normalize_metric_name,
    _strip_json_fences,
    database_query_choice,
    materialize_computation_binding,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 拆分判断用的文本模式
# ---------------------------------------------------------------------------

_ACTION_PATTERN = (
    r"查询|查一下|统计|分析|比较|对比|占比|预测|解释|口径|血缘|"
    r"生成|导出|下载|检查|找出|列出|筛选|匹配|拆分|计算|"
    r"算(?:出|一下)?|(?:再)?加(?:上)?|是多少"
)
_NEW_SENTENCE_ACTION_START = (
    r"(?:请|麻烦|帮我|请帮我|再)?\s*"
    r"(?:查询|查一下|统计|分析|比较|对比|预测|解释|生成|导出|下载|"
    r"检查|找出|列出|筛选|匹配|拆分|计算|算)"
)
_TASK_SPLIT_PATTERN = re.compile(
    rf"[;；?？\n]+|[。！!]\s*(?={_NEW_SENTENCE_ACTION_START})|"
    r"(?:，|,)?(?:另外|同时|此外|然后|再帮我|还要|以及还要)"
)
_DASH_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
    "\uff0d": "-",
})
_BUSINESS_IDENTIFIER_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9A-Za-z-]{3,64}(?![0-9A-Za-z-]))"
    r"(?=[0-9A-Za-z-]*[A-Za-z])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+"
)
_REPORT_FACET = re.compile(
    r"(?:整体)?(?:销售|订单|收入|利润|成本|库存|业务|业绩)(?:趋势|走势|变化)"
    r"|(?:医院|客户|门店|地区|区域|渠道|品类|商品|经销商|供应商)"
    r"(?:覆盖(?:情况|数据)?|分布)"
    r"|(?:合作)?(?:经销商|供应商|客户|医院|门店|渠道)"
    r"(?:数据|情况|清单|名单|画像|表现)"
    r"|(?:销售|订单|收入|利润|成本|库存|业务|业绩)"
    r"(?:规模|汇总|概览|构成|结构|排名)"
)


# ---------------------------------------------------------------------------
# 判断结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompoundJudgmentEntry:
    """一个拆出的子问题：完整可独立执行的业务问题文本 + 该问题的意图与业务域。"""

    question: str
    # 子问题的简短业务名称，任务规划节点的分组名；缺省回退问题文本
    label: str | None = None
    intent: str | None = None
    business_domain: str | None = None
    expected_output: str | None = None
    # 规则兜底拆分可能带任务依赖；模型判断的子问题完全独立，不使用该字段
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class CompoundJudgment:
    """前置判断产出：复合/单一结论，复合时给出 2-5 个子问题。

    单一问题时 intent/business_domain 描述整个问题；复合时每个子问题
    在自己的条目上携带意图与业务域。
    """

    is_compound: bool
    entries: tuple[CompoundJudgmentEntry, ...] = ()
    intent: str | None = None
    business_domain: str | None = None
    analyze_summary: str | None = None
    split_reason_code: str | None = None
    shared_conditions: tuple[str, ...] = ()
    # 报告类拆分的交付形态，与 TaskPlan.final_deliverable 对齐：仅 COMBINED_REPORT 或 None
    final_deliverable: str | None = None
    # 判断方式：STRUCTURED_MODEL 或 DETERMINISTIC_RULE
    judge_source: str = "STRUCTURED_MODEL"
    # 字段覆盖度结论：口径已定义 / 口径未定义；未定义时给出具体字段名
    schema_coverage: str = "口径已定义"
    unsupported_fields: tuple[str, ...] = ()
    analysis_routes: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# 拆分判断模型输出契约
# ---------------------------------------------------------------------------


class _JudgeTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=1000)
    label: str | None = Field(
        default=None,
        min_length=2,
        max_length=60,
        description="该子问题的简短业务名称，用规范登记的指标、维度或实体名"
            "概括要得到的业务结果，不得照抄用户问题原文，不带筛选值和标点",
    )
    intent: str = Field(
        max_length=20,
        description="必填；该子问题的问题意图，取值为意图枚举之一",
    )
    business_domain: str | None = Field(
        default=None,
        max_length=60,
        description="该子问题涉及的业务域名称，从业务语义规范中识别；识别不到输出null",
    )
    expected_output: str | None = Field(
        default=None,
        max_length=300,
        description="Short independently verifiable business deliverable",
    )


class _JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    split: bool = Field(description="必填；true=复合问题拆为多个子问题，false=单一问题")
    intent: str | None = Field(
        default=None,
        max_length=20,
        description="split=false时必填；整个问题的问题意图，取值为意图枚举之一",
    )
    business_domain: str | None = Field(
        default=None,
        max_length=60,
        description="split=false时填写；整个问题涉及的业务域名称，从业务语义规范中识别；识别不到输出null",
    )
    sub_questions: list[_JudgeTask] = Field(
        max_length=5,
        description="split=true时填2到5个完整业务子问题；split=false时必须为空数组",
    )
    analyze_summary: str | None = Field(
        default=None,
        max_length=200,
        description="仅拆分时必填；一句话向用户说明子问题结构，子问题相互独立可并行，用“问题N”指代",
    )
    split_reason_code: str | None = Field(
        default=None,
        max_length=80,
        description="仅拆分时填写；DISTINCT_DELIVERABLES, EXPLICIT_SEPARATE_RESULTS, "
        "MUTUALLY_EXCLUSIVE_BUSINESS_BRANCHES, or COMPOSITE_REPORT_FACETS 之一",
    )
    shared_conditions: list[str] = Field(default_factory=list, max_length=20)
    schema_coverage: str = Field(
        default="口径已定义",
        max_length=20,
        description="必填；查询所需字段是否全部被业务语义规范定义：口径已定义 或 口径未定义",
    )
    unsupported_fields: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="schema_coverage为口径未定义时必填；语义上无法匹配到业务语义规范中任何项的属性、指标或维度名称",
    )


_JUDGE_SYSTEM_PROMPT = """你是数据智能体的问题拆分判断器。只输出符合JSON Schema的JSON，不回答用户问题，不调用工具。

你的职责：判断用户问题是一个可单独验收的业务目标（单一问题），还是多个完全独立、需要分别交付的业务目标（复合问题）；同时对每个问题（或单一问题本身）识别问题意图和所属业务域，并检查查询所需字段是否被业务语义规范覆盖（口径未定义判断）。不做参数提取，不做执行层的任务拆解，这些由后续环节完成。

复合问题的判定标准：多个业务目标完全独立，各自都能单独验收，互相不消费对方的结果。只要其中一个目标需要使用另一个目标返回的名单、范围、排名或计算结果，就说明它们共同构成一个业务目标，属于单一问题，由后续任务拆解环节处理，不在这里拆分。

你的输出描述业务任务边界，不描述底层技术步骤。不要把ASL、SQL、取数、校验或洞察这些内部步骤当成业务目标。

先在内部完成以下判断，但不要输出推理过程：
1. 提取每个业务目标的动作、指标、返回对象或展示字段、分组维度、筛选值、时间范围与粒度、排序数量、分析或交付要求。
2. 判断每项目标能否形成可单独验收的答案或数据集；再判断目标之间是否完全独立。
3. 按判断结果输出：
   - split=false：只有一个可验收业务目标，sub_questions必须为空，intent/business_domain描述整个问题。
   - split=true：2到5个完全独立的子问题，每个子问题各自输出intent和business_domain。

数据分析任务边界：
1. 同一业务对象、筛选范围和结果粒度下的多个指标、多个展示字段或多个分组维度，通常可由一份ASL和一个数据集承载，属于一个子问题。例如“按月统计今年上海市销售额和订单笔数”“查询产品名称、规格型号和生产厂家”均为split=false。
2. 围绕同一份数据完成查询及同一分析链路的全部能力，属于一个子问题。若明确要求分别交付趋势分析、异常检测、归因或预测等多个分析目标，应拆为独立子问题，每个子问题完整保留指标、筛选、时间与粒度。例如“查询今年上海市销售额并分析月度趋势”为split=false。
3. 不同返回对象、不同关系类型、不同业务主题或用户明确要求分别交付时，应拆分。例如同一产品的“合作经销商名单”和“合作医院名单”是两个子问题；两个句子分别查询不同产品和不同对象也是两个子问题。
4. 同一语义属性下互斥且业务口径不同的分类分支，需要分别返回时应拆分。例如“主要适用科室”和“次要适用科室”是两个并行子问题；“所有适用科室”仍是一个子问题。
5. 多个筛选值本身不等于多个子问题。例如“分析费森尤斯产品在上海市和江苏省的销售趋势”可在同一数据集中按地区展示，属于一个子问题；只有用户明确要求分别形成独立结果时才拆分。
6. 两个完整查询即使只用句号、逗号或口语连接，也要按真实目标拆分，不能依赖“另外、同时、以及”等固定连接词。
7. 依赖式目标组合不属于复合问题：排名后再查询入选对象的关系或明细（如“找出销售额下降最大的五个产品，并列出这些产品涉及的经销商和医院”）、分别查询多个结果再比较合并（如“分别查询今年和去年销售额，并比较同比变化”）、明细查询带指标阈值条件（如“订单笔数大于5笔的经销商名单”）都输出split=false，由后续任务拆解环节处理。
8. 规范指标层级中登记的指标（包括业务语义由其他指标运算合成的复合口径指标，如覆盖率、平均每单金额）可直接作为一个查询目标执行，输出split=false；不得按其依赖指标拆成多个查询加计算。
9. 指标说法与规范指标层级比对后分两种情况：说法由规范内指标派生（对规范内指标求增长率、均值、占比、排名、最高最低等派生口径），按正常问题处理不拆分；说法与规范内任何指标都无关、也无法由规范内指标派生时，同样不拆分（后续环节会按指标口径解释处理）。判断是否“无法派生”按业务语义判断，不要求字面上包含指标名：围绕规范内实体或业务对象的度量（数量、金额、快慢、高低、升降、排名）提出的说法，都可由该业务对象在规范内的指标派生；只有规范中连对应业务对象的度量都不存在的纯概念词，才属于无法派生。
10. “前N名销售人员以及所属公司”等同一对象的唯一关联属性，不是额外的分组目标；把关联属性保留在查询的展示字段中。任何用户要求返回的属性不得遗漏。

问题意图枚举（intent字段从中选择最贴合的一个，只输出枚举名本身）：
指标查询、明细查询、趋势分析、对比分析、占比分析、异常分析、归因分析、预测分析、解释口径、闲聊、非数据任务。

同比环比判定补充：问题含同比、环比、同期比等期间对比措辞时，两期对比（如"11月环比10月""今年同比去年"）intent输出对比分析，不输出趋势分析；落在多期时间序列上的逐期同比/环比（带时间范围或各月、逐期等序列措辞，如"各月环比变化情况""近6个月同比增长趋势"）intent输出趋势分析，多期序列的逐期计算由趋势链路承载，不属于两期对比。

极值判定补充：问题按最高、最低、最快、最慢等极值挑出某个对象或订单（如"哪个订单的均价最低"）时，intent输出明细查询，不输出指标查询；极值不存在大于/小于的门槛数值，不属于阈值筛选。极值依据的数值口径由后续环节处理：口径是业务语义规范中已注册指标的，提取环节把该指标写入指标通道并按登记口径计算后排序取最值；未注册的口径按明细列排序加行数限制实现。

业务域（business_domain）从注入的业务语义规范中识别，输出业务域名称（如“医药销售域”）；规范中识别不到时输出null。

字段覆盖度检查规则（schema_coverage与unsupported_fields的输出依据）：
1. 只检查问题中明确要求的属性、指标、维度三类要素；关系、筛选值、计算方式不在检查范围。
2. 匹配按语义不按字面：问题用语与规范项存在措辞差异（修饰词增减、语序变化、口语化表达）时，按语义相似判断是否指向同一业务概念，能判定为同一概念即为口径已定义。例如规范定义了"合作科室"，问题说"合作综合科室"，两者指向同一业务概念，按口径已定义处理。
3. 同义词、别名、上下位、衍生计算、规范关系路径同样按已定义处理。
4. 只有语义上确实对应不到规范内任何项（规范中没有该业务概念）时，schema_coverage才输出口径未定义，unsupported_fields逐个列出对应不上的名称；任一子问题存在未定义要素即判口径未定义，全部并入unsupported_fields。
5. 不得把未定义要素臆造为规范字段；商品名称、厂家名称等具体值与品牌、品类等独立维度不是同一概念，前者不构成对后者的语义匹配；不得因字段未定义而改变复合/单一判断或问题意图。
6. 要素未定义不代表问题无法回答：规范内可替代的字段不影响本判断，只判断用户明确要求的要素是否有对应概念。

子问题生成规则：
1. 每个子问题必须是自然、完整、可独立理解和执行的业务问题。补全原句中对该子问题生效的产品、地区、指标、时间、筛选、排序及排除条件，不保留只有“另一个、上述条件”等内容的空泛指代。
2. 只允许复用用户原文明确提供的事实和共享条件；不得创造指标、日期、数量、业务对象、筛选值、口径或数据库字段，不负责替用户消除业务歧义。
3. 子问题之间完全独立，不互相消费结果。保持用户目标的原始顺序，不重复、不遗漏，子问题总数为2到5。若无需拆分，split输出false且sub_questions输出空数组。
4. 拆分时analyze_summary必填：一句面向用户的话说明子问题结构，子问题相互独立可并行，用“问题N”指代，不描述内部技术步骤。
"""


# ---------------------------------------------------------------------------
# 多任务计划校验与执行辅助
# ---------------------------------------------------------------------------


def validate_plan(plan: TaskPlan, *, source_question: str | None = None,
                  registered_default_times: tuple[str, ...] = (),
                  registered_conditions: tuple[str, ...] = ()) -> None:
    if not 2 <= len(plan.tasks) <= 12:
        raise TaskPlanningError("任务计划必须包含2到12个原子任务")
    task_ids = [task.task_id for task in plan.tasks]
    if len(task_ids) != len(set(task_ids)):
        raise TaskPlanningError("任务ID重复")
    known: set[str] = set()
    for task in plan.tasks:
        if not task.question.strip():
            raise TaskPlanningError("原子任务问题不能为空")
        if task.task_id in task.depends_on:
            raise TaskPlanningError("任务不能依赖自身")
        unknown = set(task.depends_on) - known
        if unknown:
            raise TaskPlanningError(
                "任务依赖必须引用排在其前面的任务：" + ",".join(sorted(unknown))
            )
        known.add(task.task_id)
    if source_question is not None:
        _validate_grounding(plan, source_question, registered_default_times, registered_conditions)


def _unique_mentions(text: str, candidates: tuple[str, ...]) -> list[str]:
    # 取最长词优先，避免“退款”嵌在“退款率”里被当成两个独立指标，
    # 导致共享条件不再向子问题补齐。
    selected: list[str] = []
    for value in sorted(candidates, key=len, reverse=True):
        if value in text and not any(value in existing for existing in selected):
            selected.append(value)
    return sorted(selected, key=lambda value: text.find(value))


_QUARTER_RANGE_PATTERN = re.compile(
    r"(?:\d{4})?[QqHh][1-4](?:-[0-9A-Za-z]+)*"
)


def _business_identifiers(text: str) -> list[str]:
    normalized = text.translate(_DASH_TRANSLATION)
    matches = _BUSINESS_IDENTIFIER_PATTERN.findall(normalized)
    # 季度/半年度区间写法（Q2-Q4、2025H1-H2）是时间条件不是业务型号编码，
    # 交给时间通道校验；按业务编码字面强比对曾把等价改写（“Q2至Q4”）的
    # 规划整体误杀成“任务计划遗漏原问题中的业务型号或编码”。
    return list(dict.fromkeys(
        match for match in matches if not _QUARTER_RANGE_PATTERN.fullmatch(match)
    ))


def _cn_rank_number(match: re.Match[str]) -> str:
    """中文序数写法转阿拉伯数字，“前五位”“前十名”与“前5位”“10名”等价。"""
    text = match.group(1)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in text:
        tens_part, _, ones_part = text.partition("十")
        if (tens_part and any(ch not in digits for ch in tens_part)) or (
            ones_part and any(ch not in digits for ch in ones_part)
        ):
            return match.group(0)
        tens = digits.get(tens_part, 1) if tens_part else 1
        ones = digits.get(ones_part, 0) if ones_part else 0
        return str(tens * 10 + ones)
    if any(ch not in digits for ch in text):
        return match.group(0)
    return str(digits[text])


def _normalize_grounding_numbers(text: str) -> str:
    """口语日期写法与规范写法等价"""
    text = re.sub(
        r"(?<!\d)(\d{2})(?=年)",
        lambda m: f"20{m.group(1)}" if int(m.group(1)) <= 49 else m.group(1),
        text,
    )
    text = re.sub(r"(?<!\d)0(\d)(?=[月日号])", r"\1", text)
    # “前五位/排名前三”常被改写成“前5位/前3”，前/第后面的中文数字转阿拉伯数字再比对
    text = re.sub(
        r"(?<=[前第])([一二两三四五六七八九十]{1,3})(?![一二两三四五六七八九十\d年])",
        _cn_rank_number,
        text,
    )
    # “最多/最高”常被改写为“第1名/前1名”，序数措辞里的1不参与数字比对
    return re.sub(
        r"第\s*[一1]\s*[名位席]|前\s*[一1]\s*[名位席个家]|top\s*1(?!\d)",
        "首位",
        text,
    )


def _qualified_facet_parts(question: str) -> tuple[str, list[str]] | None:
    """返回共享前缀和两个对比性、可在原文落地的业务分支措辞。"""
    normalized = question.strip(" ，,。！？?；;")
    qualifier_groups = (
        ("主要", "次要"),
        ("正常", "异常"),
        ("新增", "存量"),
        ("线上", "线下"),
        ("有效", "无效"),
        ("合作", "未合作"),
    )
    qualifier_to_group = {
        qualifier: frozenset(group)
        for group in qualifier_groups
        for qualifier in group
    }
    qualifier_pattern = re.compile("|".join(
        re.escape(value)
        for value in sorted(qualifier_to_group, key=len, reverse=True)
    ))
    split_match = re.search(r"(?:、|，|,|以及|和|与|及)", normalized)
    if split_match is None:
        return None
    qualifiers = list(qualifier_pattern.finditer(normalized))
    if len(qualifiers) != 2:
        return None
    left, right = qualifiers
    if left.group(0) == right.group(0):
        return None
    if qualifier_to_group[left.group(0)] != qualifier_to_group[right.group(0)]:
        return None
    if left.start() > split_match.start() or right.start() < split_match.end():
        return None
    suffix = normalized[right.end():].strip(" ，,。！？?；;")
    if not suffix or len(suffix) > 30:
        return None
    if not re.search(_ACTION_PATTERN, normalized):
        return None
    prefix = normalized[: left.start()]
    if not prefix:
        return None
    return prefix, [left.group(0) + suffix, right.group(0) + suffix]


def _validate_grounding(plan: TaskPlan, source_question: str,
                       registered_default_times: tuple[str, ...] = (),
                       registered_conditions: tuple[str, ...] = ()) -> None:
    """拒绝模型自造的指标和带引号的过滤值。

    只做防自造的正向校验：任务文本不得引入原问题没有的指标、时间条件、
    引号过滤值和业务型号。数字不拦截（2026-10-21 用户裁决）："第一名→1"、
    "去年同期→2024"等价改写只留诊断日志，拆错了就继续执行。"遗漏"方向
    只比对无参数载体的信号（排除/限定语气、并列分支结构、业务型号编码）
    ——任务问题是允许改写的展示字段，年份、排名、指标、时间等要求合法
    落在结构化参数（时间粒度、排序、限制、指标通道）里而不复读进文本，
    文本级强比对只会把整个规划误杀（实测多轮复现），下游合同以结构化
    参数为准。
    """
    source_for_grounding = re.sub(
        r"(?:^|[;；\n])\s*[一二三四五1-5][、.)．]\s*",
        " ",
        source_question,
    )
    source_compact = _normalize_grounding_numbers(
        re.sub(r"\s+", "", source_for_grounding).casefold()
    )
    planned_compact = _normalize_grounding_numbers(
        "".join(re.sub(r"\s+", "", task.question) for task in plan.tasks).casefold()
    )
    source_numbers = set(re.findall(r"\d+(?:\.\d+)?", source_compact))
    metric_terms = (
        "销售额", "订单量", "销售量", "客户数", "客单价", "退款额", "退款率",
        "毛利额", "毛利率", "利润额", "利润率", "库存量", "转化率",
    )
    temporal_terms = (
        "今天", "昨天", "本周", "上周", "本月", "上月", "本季度", "上季度",
        "今年", "去年", "明年", "最近一周", "最近一个月", "最近三个月",
        "最近半年", "最近一年",
    )
    source_metrics = {value for value in metric_terms if value in source_compact}
    source_temporal = {value for value in temporal_terms if value in source_compact}
    source_quoted = set(re.findall(r"[‘’'\"“”]([^‘’'\"“”]{1,100})[‘’'\"“”]", source_question))
    source_identifiers = set(_business_identifiers(source_question))
    for task in plan.tasks:
        grounding_question = task.question
        for default in registered_default_times:
            pattern = r"\s*".join(re.escape(char) for char in default)
            grounding_question = re.sub(pattern, "", grounding_question)
        compact = _normalize_grounding_numbers(
            re.sub(r"\s+", "", grounding_question).casefold()
        )
        numeric_question = compact
        # Task references are control metadata, not new business thresholds.
        # Accept only references to this task's declared predecessors.
        predecessors = getattr(task,'depends_on',())
        predecessor_numbers={str(index) for index,item in enumerate(plan.tasks)
                             if getattr(item,'task_id',None) in predecessors}
        predecessor_numbers.update(str(index+1) for index,item in enumerate(plan.tasks)
                                   if getattr(item,'task_id',None) in predecessors)
        numeric_question=re.sub(r'任务(\d+)|第(\d+)个任务',
            lambda match:'' if (match[1] or match[2]) in predecessor_numbers else match[0],numeric_question)
        for condition in sorted(registered_conditions,key=len,reverse=True):
            numeric_question = re.sub(re.escape(condition.casefold()) + r'(?![\d.])', '', numeric_question)
        task_numbers = set(re.findall(r"\d+(?:\.\d+)?", numeric_question))
        # 结构化时间粒度已声明的月份/日期边界数字是合法绑定数字：“春季”
        # 被结构化抽取展开成“3月-5月”时，任务文本复读这些边界不是自造
        # 数值，不计入 unexpected（两段时段对比问题误报复盘）。数字口径
        # 仍以下游按结构化参数与向量目录的校验为准。
        declared_time_numbers: set[str] = set()
        grain = (getattr(task, 'extraction', None) or {}).get('时间粒度')
        if isinstance(grain, dict):
            for key in ('time_range', 'anchor'):
                value = grain.get(key)
                if isinstance(value, str):
                    declared_time_numbers.update(re.findall(
                        r"\d+(?:\.\d+)?",
                        _normalize_grounding_numbers(
                            re.sub(r"\s+", "", value).casefold()),
                    ))
        unexpected_numbers = task_numbers - source_numbers - declared_time_numbers
        if unexpected_numbers:
            # 2026-10-21 用户裁决：数字不再拦截。"第一名→1"、"去年同期→2024"
            # 属等价改写或由原问题年份直接推导，字面强比对只会误杀整个规划
            # （实测复现：多任务计划被整体拒绝后无任务执行）。拆分不完美
            # 就继续执行，数字口径交给下游按结构化参数与向量目录校验，
            # 这里只留诊断日志。
            logger.warning(
                "grounding numeric mismatch tolerated task=%s unexpected=%r question=%r",
                getattr(task, "task_id", ""),
                sorted(unexpected_numbers),
                task.question,
            )
        if not {value for value in metric_terms if value in compact}.issubset(source_metrics):
            # 2026-10-21 用户裁决延续：与数字校验同类——原问题"销售结构中的
            # 占比"隐含"销售额"等指标名改写是合法展开不是自造指标，字面强
            # 比对曾把整个规划拒成"未能生成完整的可执行计划"（Q16实测复现，
            # 零任务执行）。指标口径交给下游按结构化参数与向量目录校验，
            # 这里只留诊断日志。
            logger.warning(
                "grounding metric mismatch tolerated task=%s metrics=%r question=%r",
                getattr(task, "task_id", ""),
                sorted({value for value in metric_terms if value in compact} - source_metrics),
                task.question,
            )
        if not {value for value in temporal_terms if value in compact}.issubset(source_temporal):
            raise TaskPlanningError("子任务包含原问题中不存在的时间条件")
        quoted = set(re.findall(r"[‘’'\"“”]([^‘’'\"“”]{1,100})[‘’'\"“”]", task.question))
        if not all(value in source_quoted or value in source_question for value in quoted):
            raise TaskPlanningError("子任务包含原问题中不存在的过滤值")
        task_identifiers = set(_business_identifiers(task.question))
        if not task_identifiers.issubset(source_identifiers):
            raise TaskPlanningError("子任务包含原问题中不存在的业务型号或编码")

    # "遗漏"方向的文本比对只保留无参数载体的信号：排除/限定语气、并列分支
    # 结构、业务型号编码没有独立的结构化参数通道，任务文本丢了就真丢了；
    # 数字、排名、指标、时间等信号合法落在时间粒度/排序/限制/指标通道里，
    # 文本级强比对只会把整个规划误杀（实测多轮复现），不做反向比对。
    planned_identifiers = set(
        _business_identifiers(" ".join(task.question for task in plan.tasks))
    )
    if not source_identifiers.issubset(planned_identifiers):
        raise TaskPlanningError("任务计划遗漏原问题中的业务型号或编码")

    facet_parts = _qualified_facet_parts(source_question)
    if facet_parts is not None:
        _, required_facets = facet_parts
        coverage = {
            facet: sum(facet in task.question for task in plan.tasks)
            for facet in required_facets
        }
        if any(count != 1 for count in coverage.values()):
            raise TaskPlanningError("任务计划遗漏或重复了并列业务分支")

    constraint_markers = {
        marker for marker in ("不要", "不看", "不查", "排除", "剔除", "只看", "仅看")
        if marker in source_compact
    }
    if not constraint_markers.issubset({marker for marker in constraint_markers if marker in planned_compact}):
        raise TaskPlanningError("任务计划遗漏原问题中的排除或限定条件")


def execution_layers(plan: TaskPlan) -> list[list[AtomicTask]]:
    remaining = {task.task_id: task for task in plan.tasks}
    completed: set[str] = set()
    layers: list[list[AtomicTask]] = []
    while remaining:
        ready = [
            task for task in remaining.values()
            if set(task.depends_on).issubset(completed)
        ]
        if not ready:
            raise TaskPlanningError("任务DAG存在环路或无效依赖")
        layers.append(ready)
        completed.update(task.task_id for task in ready)
        for task in ready:
            remaining.pop(task.task_id)
    return layers


def query_fingerprint(question: str) -> str:
    normalized = question.casefold().strip()
    normalized = re.sub(r"[\s，。！？；、,.!?;:：]+", "", normalized)
    normalized = re.sub(r"^(?:请|帮我|麻烦|请帮我)+", "", normalized)
    return normalized


def deduplicate_plan(plan: TaskPlan) -> tuple[TaskPlan, dict[str, str]]:
    """合并完全相同的原子查询，被合并任务保留别名供最终输出引用。"""
    canonical_by_key: dict[tuple, str] = {}
    aliases: dict[str, str] = {}
    unique: list[AtomicTask] = []
    resolved: dict[str, str] = {}
    for task in plan.tasks:
        dependencies = tuple(dict.fromkeys(resolved.get(dep, dep) for dep in task.depends_on))
        extraction = copy.deepcopy(task.extraction)
        if extraction:
            binding = extraction.get('计算绑定') or {}
            operands = [binding.get(role) for role in ('numerator', 'denominator', 'left', 'right', 'input')]
            operands.append(extraction.get('结果操作'))
            operands.extend(extraction.get('依赖绑定') or [])
            for operand in operands:
                if isinstance(operand, dict) and operand.get('source_task_id') in resolved:
                    operand['source_task_id'] = resolved[operand['source_task_id']]
        analysis_binding = (extraction or {}).get('分析绑定')
        if analysis_binding:
            for period in analysis_binding.get('periods') or []:
                period['task'] = resolved.get(period['task'], period['task'])
            analysis_binding['optional_dependencies'] = [resolved.get(dep, dep) for dep in analysis_binding.get('optional_dependencies') or []]
        calculation = {key: extraction[key] for key in ('计算绑定', '结果操作', '分析绑定', '依赖绑定') if extraction and key in extraction}
        key = (query_fingerprint(task.question), dependencies, database_query_choice(extraction),
               json.dumps(calculation, ensure_ascii=False, sort_keys=True) if calculation else '')
        existing = canonical_by_key.get(key)
        if existing is not None:
            aliases[task.task_id] = existing
            resolved[task.task_id] = existing
            continue
        canonical_by_key[key] = task.task_id
        resolved[task.task_id] = task.task_id
        unique.append(task.model_copy(update={"depends_on": list(dependencies), 'extraction': extraction}))
    # TaskPlan 公开契约要求至少两个节点；全部重复的计划由编排器按单任务执行，
    # 不为这种情况放松公开契约。
    if len(unique) == len(plan.tasks):
        return plan, aliases
    return plan.model_copy(update={"tasks": unique}, deep=True), aliases


# ---------------------------------------------------------------------------
# 确定性规则兜底拆分
# ---------------------------------------------------------------------------


def _has_action(text: str) -> bool:
    return bool(re.search(_ACTION_PATTERN, text))


def _depends_on_previous(text: str) -> bool:
    return bool(re.search(
        r"基于(?:上一步|前面|上述|这个结果)|从中|其中|这些结果|"
        r"再对(?:它|其)|(?:再|并)?(?:找出|计算|算).*(?:最高|最低|差|结果)|"
        r"最高比最低", text
    ))


def _ranked_relation_plan(question: str) -> TaskPlan | None:
    match = re.fullmatch(
        r"(?P<ranking>(?:查询|找出|列出).+?(?:最高|最低|最大|最小)"
        r"(?:的)?(?:前)?[一二两三四五六七八九十\d]*个?(?:产品|商品))"
        r"(?:，|,)?(?:并|再|然后)(?P<relation>列出|查询|找出)"
        r"(?P<tail>(?:涉及|关联|对应).+)",
        question.strip(" 。"),
    )
    if match is None:
        return None
    return TaskPlan(
        tasks=[
            AtomicTask(task_id="task-1", question=match.group("ranking")),
            AtomicTask(
                task_id="task-2",
                question=f"{match.group('relation')}上述产品{match.group('tail')}",
                depends_on=["task-1"],
            ),
        ],
        planner="DETERMINISTIC_RULE",
    )


def _parallel_ranking_plan(question: str) -> TaskPlan | None:
    """两个各自带排名口径的目标即使共用动词也要拆开。"""
    normalized = question.strip(" ，,。")
    # “分别查询A和B”“查询A，以及B”常省略第二个动词；
    # 只有两侧都带明确排名形态才拆，避免误拆普通指标/维度并列。
    match = re.match(
        r"^(?P<prefix>分别)?(?P<verb>查询|统计|找出|列出)"
        r"(?P<left>.+?(?:前[一二两三四五六七八九十\d]+|最高|最多|最低|最少).+?)"
        r"(?:，|,)?(?:以及|同时(?:查询|统计)?|和|与)"
        r"(?P<right>.+(?:前[一二两三四五六七八九十\d]+|最高|最多|最低|最少).*)$",
        normalized,
    )
    if match is None:
        return None
    verb = match.group("verb")
    left = match.group("left").strip(" ，,")
    right = re.sub(r"^(?:查询|统计|找出|列出)", "", match.group("right")).strip(" ，,")
    if not left or not right:
        return None
    return TaskPlan(
        tasks=[
            AtomicTask(task_id="task-1", question=f"{verb}{left}"),
            AtomicTask(task_id="task-2", question=f"{verb}{right}"),
        ],
        planner="DETERMINISTIC_RULE",
    )


def _report_plan(question: str, *, max_tasks: int) -> TaskPlan | None:
    """明确多分析面的报告拆成可独立验证的查询。

    报告是最终交付物而不是查询形态；把多个分析面压成一份探索性ASL
    可能只返回其中一个聚合。该确定性路径保留共享主体/筛选文本，
    每个点名的分析面各自查询后再组装报告。
    """
    if not re.search(r"(?:分析)?报告|报表", question, re.IGNORECASE):
        return None
    facet_report = re.fullmatch(
        r"(?:请|帮我|给我)?(?:生成|制作|输出)?"
        r"(?P<scope>.+?)(?:销售)?分析(?:报告|报表)[，,；;]?"
        r"(?:包含|包括)(?P<facets>.+)",
        question.strip(" 。"),
    )
    if facet_report is not None:
        scope = facet_report.group("scope").strip(" ，,")
        facets = facet_report.group("facets")
        tasks: list[AtomicTask] = []
        for label, task_text in (
            ("趋势", f"分析{scope}销售趋势"),
            ("排名", f"分析{scope}销售排名"),
            ("异常", f"分析{scope}销售异常"),
        ):
            if label in facets:
                tasks.append(AtomicTask(
                    task_id=f"task-{len(tasks) + 1}", question=task_text,
                ))
        if len(tasks) >= 2:
            return TaskPlan(
                tasks=tasks,
                planner="DETERMINISTIC_RULE",
                final_deliverable="COMBINED_REPORT",
            )
    body = re.sub(
        r"[，,；;]?\s*(?:(?:并|再)?(?:输出|生成|形成|制作|导出)|给我)"
        r".{0,24}?(?:分析)?(?:报告|报表).*$",
        "",
        question.strip(),
        flags=re.IGNORECASE,
    ).strip(" ，,；;。")
    matches = list(_REPORT_FACET.finditer(body))
    # 后续扩展分析面表达式出现重叠时，保留更长的那个匹配。
    selected: list[re.Match[str]] = []
    for match in matches:
        if selected and match.start() < selected[-1].end():
            if match.end() - match.start() > selected[-1].end() - selected[-1].start():
                selected[-1] = match
            continue
        selected.append(match)
    if len(selected) < 2:
        return None

    prefix = body[: selected[0].start()]
    prefix = re.sub(
        r"^\s*(?:请|麻烦|帮我|给我|请帮我)?\s*"
        r"(?:分析|查询|统计|查看|看看|梳理)?\s*(?:一下|下)?\s*(?:关于)?",
        "",
        prefix,
    ).strip(" ，,；;、")
    if not prefix:
        return None

    tasks = []
    uses_default_history = not re.search(
        r"(?:19|20)\d{2}年|最近|过去|近(?:一|二|三|四|五|六|七|八|九|十|\d+)"
        r"(?:天|日|周|个月|月|季度|年)|本(?:周|月|季度|年)|上(?:周|月|季度|年)",
        question,
    )
    selected = selected[:max_tasks]
    for index, match in enumerate(selected):
        end = selected[index + 1].start() if index + 1 < len(selected) else len(body)
        facet = body[match.start():end].strip(" ，,；;、和及与")
        if not facet:
            continue
        matched_facet = match.group(0)
        explicit_detail = bool(re.search(r"清单|名单|明细", matched_facet))
        report_object = re.search(
            r"医院|客户|门店|地区|区域|渠道|品类|商品|经销商|供应商",
            matched_facet,
        )
        bounded_count_facet = bool(
            report_object
            and not explicit_detail
            and re.search(r"覆盖|分布|数据|情况|表现", matched_facet)
        )
        if bounded_count_facet:
            object_name = report_object.group(0)
            if "覆盖" in matched_facet:
                task_question = f"统计{prefix}已合作{object_name}数"
            elif matched_facet.startswith("合作"):
                task_question = f"统计{prefix}已合作{object_name}数"
            else:
                task_question = f"统计{prefix}{facet}中的唯一{object_name}总数"
        elif explicit_detail:
            task_question = f"列出{prefix}{facet}明细"
        else:
            task_question = f"分析{prefix}{facet}"
        if uses_default_history:
            task_question += "，使用数据源全部可用历史"
        tasks.append(AtomicTask(
            task_id=f"task-{len(tasks) + 1}",
            question=task_question,
        ))
    if len(tasks) < 2:
        return None
    return TaskPlan(
        tasks=tasks,
        planner="DETERMINISTIC_RULE",
        final_deliverable="COMBINED_REPORT",
    )


def _parallel_qualified_facet_plan(question: str) -> TaskPlan | None:
    """一个共享动作下两个对比性业务分支拆成并行查询。

    只识别对比性业务限定词，不拆“名称、规格型号”这类普通展示字段并列。
    """
    parts = _qualified_facet_parts(question)
    if parts is None:
        return None
    prefix, facets = parts
    tasks = [
        AtomicTask(
            task_id=f"task-{index + 1}",
            question=(prefix + facet).strip(),
        )
        for index, facet in enumerate(facets)
    ]
    return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")


def _inline_dependent_plan(question: str, *, max_tasks: int) -> TaskPlan | None:
    """没有标点分隔的显式“查询→计算”链也要拆。"""
    referenced_result = re.fullmatch(
        r"(?P<first>.+?)(?:，|,)?(?:并)?再"
        r"(?P<relation>从|根据|基于)"
        r"(?P<second>(?:上述|上一步|前述|前面|这些|该|其).+"
        r"(?:筛选|过滤|查找|查询|列出|统计|分析).+)",
        question,
    )
    dependent_filter = re.fullmatch(
        r"(.+?)(?:，|,)?并根据(.{1,80}?)(筛选出|过滤出)(.+)", question
    )
    if referenced_result:
        pieces = [
            referenced_result.group("first"),
            referenced_result.group("relation") + referenced_result.group("second"),
        ]
    elif dependent_filter:
        pieces = [
            dependent_filter.group(1),
            "根据" + dependent_filter.group(2)
            + dependent_filter.group(3) + dependent_filter.group(4),
        ]
    else:
        pieces = re.split(
            r"(?:(?:，|,)?(?:并(?:再)?|然后|再)|(?:，|,))(?="
            r"(?:计算|算(?:出|一下)?|找出|筛选|过滤|排序).{0,80}"
            r"(?:差|客单价|占比|最高|最低|最大|最小|排序|筛选|过滤))",
            question,
        )
    pieces = [piece.strip(" ，,。.") for piece in pieces if piece.strip(" ，,。.")]
    if len(pieces) < 2 or not _has_action(pieces[0]):
        return None
    if not all(_has_action(piece) for piece in pieces[1:]):
        return None
    shared_identifiers = _business_identifiers(question)
    for index in range(1, len(pieces)):
        for identifier in shared_identifiers:
            if identifier not in pieces[index]:
                pieces[index] += f"，业务对象为{identifier}"
    if (
        any("差" in piece for piece in pieces[1:])
        and len(re.findall(r"(?:1[0-2]|0?[1-9])月", pieces[0])) >= 2
        and not re.search(r"按月|每月|分月|分别", pieces[0])
    ):
        pieces[0] += "，分别按月返回"
    tasks = [
        AtomicTask(
            task_id=f"task-{index + 1}",
            question=piece,
            depends_on=[] if index == 0 else [f"task-{index}"],
        )
        for index, piece in enumerate(pieces[:max_tasks])
    ]
    return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")


def _rule_plan(question: str, *, max_tasks: int) -> TaskPlan | None:
    qualified_facet_plan = _parallel_qualified_facet_plan(question)
    if qualified_facet_plan is not None:
        return qualified_facet_plan
    normalized = re.sub(r"\r\n?", "\n", question).strip()
    normalized = re.sub(
        r"(?:^|[\s;；])[一二三四五12345][、.)．]\s*", "\n", normalized
    )
    if _TASK_SPLIT_PATTERN.search(normalized) is None:
        dependent_plan = _inline_dependent_plan(normalized, max_tasks=max_tasks)
        if dependent_plan is not None:
            return dependent_plan
    pieces = _TASK_SPLIT_PATTERN.split(normalized)
    pieces = [piece.strip(" ，,。.") for piece in pieces if piece.strip(" ，,。.")]
    actionable = [piece for piece in pieces if _has_action(piece)]
    if len(actionable) < 2:
        return None
    tasks: list[AtomicTask] = []
    shared_metrics = _unique_mentions(
        normalized,
        (
            "销售额", "订单量", "订单", "销售量", "销量", "客户数", "客单价",
            "退款额", "退款率", "退款", "毛利额", "毛利率", "利润额", "利润率",
            "库存量", "库存", "转化率",
        ),
    )
    shared_periods = _unique_mentions(
        normalized,
        (
            "今天", "昨天", "本周", "上周", "本月", "上月", "本季度", "上季度",
            "今年", "去年", "最近一周", "最近一个月", "最近三个月", "最近半年",
            "最近一年",
        ),
    )
    explicit_periods = re.findall(
        r"(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月份?"
        r"|(?:19|20)\d{2}年"
        r"|(?:19|20)\d{2}[-/.](?:1[0-2]|0?[1-9])",
        normalized,
    )
    if not shared_periods and explicit_periods:
        shared_periods = list(dict.fromkeys(explicit_periods))
    for index, piece in enumerate(actionable[:max_tasks]):
        dependency = (
            [f"task-{index}"]
            if index > 0 and _depends_on_previous(piece)
            else []
        )
        enriched = piece
        if len(shared_metrics) == 1 and shared_metrics[0] not in enriched:
            enriched += f"，指标为{shared_metrics[0]}"
        if len(shared_periods) == 1 and shared_periods[0] not in enriched:
            enriched += f"，时间范围为{shared_periods[0]}"
        tasks.append(
            AtomicTask(
                task_id=f"task-{index + 1}",
                question=enriched,
                depends_on=dependency,
            )
        )
    return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")


def _rule_split_candidate(question: str) -> bool:
    action_count = len(re.findall(_ACTION_PATTERN, question))
    separators = bool(
        _TASK_SPLIT_PATTERN.search(question)
        or re.search(r"[、]|(?:以及|和|与|及)", question)
    )
    numbered = len(re.findall(r"(?:^|\s)[一二三四五12345][、.)．]", question)) >= 2
    inline_dependency = bool(re.search(
        r"(?:"
        r"(?:并(?:再)?|然后|再)(?:计算|算(?:出|一下)?|找出|筛选|过滤|排序)"
        r".{0,80}(?:差|客单价|占比|最高|最低|最大|最小|排序|筛选|过滤)"
        r"|匹配.{1,80}(?:所属|适用).{1,40}(?:并|然后|再).{0,20}(?:筛选|过滤)"
        r"|(?:再)?加(?:上)?.{0,40}[，,](?:计算|算).{0,40}客单价"
        r"|(?:并)?再(?:从|根据|基于)(?:上述|上一步|前述|前面|这些|该|其)"
        r".{1,80}(?:筛选|过滤|查找|查询|列出|统计|分析)"
        r")",
        question,
    ))
    qualified_facets = _qualified_facet_parts(question) is not None
    return (
        action_count >= 2 and (separators or numbered or inline_dependency)
    ) or qualified_facets


def rule_split_plan(question: str, *, max_tasks: int) -> TaskPlan | None:
    """判断模型不可用时的确定性拆分兜底。

    只覆盖结构明确、被验证过的形态：排名+关系依赖、双排名并行、
    多分析面报告、通用分句拆分。拆不出可靠结构时返回None按单一处理。
    """
    plan = _ranked_relation_plan(question)
    if plan is not None:
        return plan
    plan = _parallel_ranking_plan(question)
    if plan is not None:
        return plan
    plan = _report_plan(question, max_tasks=max_tasks)
    if plan is not None:
        return plan
    if not _rule_split_candidate(question):
        return None
    plan = _rule_plan(question, max_tasks=max_tasks)
    if plan is None or len(plan.tasks) < 2:
        return None
    return plan


def _plan_from_judgment(judgment: CompoundJudgment) -> TaskPlan:
    tasks = [
        AtomicTask(
            task_id=f"task-{index + 1}",
            question=entry.question.strip(),
            # 规则兜底拆分没有拆解模型的任务名，用判定模型的简短业务名
            name=entry.label,
            depends_on=[f"task-{dep + 1}" for dep in entry.depends_on],
            expected_output=entry.expected_output,
        )
        for index, entry in enumerate(judgment.entries)
    ]
    return TaskPlan(
        tasks=tasks,
        planner="STRUCTURED_MODEL" if judgment.judge_source == "STRUCTURED_MODEL" else "DETERMINISTIC_RULE",
        split_reason_code=judgment.split_reason_code or "UNSPECIFIED",
        shared_conditions=list(dict.fromkeys(judgment.shared_conditions)),
        analyze_summary=judgment.analyze_summary,
        final_deliverable=judgment.final_deliverable,
        question_groups=[
            QuestionTaskGroup(
                question=entry.question.strip(),
                label=entry.label,
                task_offset=index,
                task_count=1,
            )
            for index, entry in enumerate(judgment.entries)
        ],
    )


# ---------------------------------------------------------------------------
# 指标顶替兜底用的问句切分模式：只服务于从用户原话里抠指标短语，
# 抠不出来就返回空串走通用未定义话术，不参与意图判定。
# ---------------------------------------------------------------------------

_USER_METRIC_ACTION = re.compile(
    r"^(?:请|麻烦|帮我|请帮我|再|给我)?\s*"
    r"(?:查询|查看|查一下|看一下|看看|统计|分析|列出|找出|筛选|计算|算一下|算)"
)
_USER_METRIC_FILLER = re.compile(
    r"^(?:今年|去年|本月|上月|上个月|本季度|本年|最近一年|近一年|"
    r"全部|所有|每个|各|分别|目前|当前)"
)
_USER_METRIC_OBJECT = re.compile(
    r"^(?:[一-龥]{1,3}(?:省|市|自治区))?"
    r"(?:各)?(?:经销商|医院|产品|商品|客户|供应商|门店|渠道|厂家|品牌|地区|区域)"
)
_METRIC_SUFFIX = re.compile(
    r"(?:匹配度|满意度|占比|度|率|比|值|额|总数|笔数|次数|天数|人数|家数|数量|数|量|时长|周期)$"
)
# 计算任务问题里“计算……的×××”的指标名，供顶替检查把计算目标也算进
# 计划声明的口径；“作为×××”等用户指标别名尾巴要先截掉。
_CALC_METRIC_PATTERN = re.compile(
    r"(?:计算|算出|汇总|求得)[^，。;；]{0,20}的"
    r"([^，。;；]{2,16}?(?:匹配度|满意度|占比|率|度|比|值|额|总数|笔数|次数|天数|人数|家数|数量|数|量|时长|周期))"
)


def _calc_metric_names(question: str) -> list[str]:
    """从计算任务问题里抠出被计算的指标名，用户别名尾巴（作为×××）截掉。"""
    names: list[str] = declared_calculation_targets(question)
    for match in _CALC_METRIC_PATTERN.finditer(question or ""):
        name = match.group(1).strip()
        # “计算A作为B”句式里A才是计划实际计算的规范指标
        name = re.split(r"作为|记作|写作|即", name)[0].strip(" 的，,、")
        if 2 <= len(name) <= 16:
            names.append(name)
    return names


def user_metric_phrase(question: str) -> str:
    """从用户原话里截取指标短语，供顶替兜底时保留原文指标名。

    去掉开头动词、时间范围和业务对象后，取以指标后缀结尾的片段；
    拿不到就返回空串，由调用方退回不带指标名的通用话术。
    """
    text = question.strip(" 。，,？?：:、！!").strip()
    action = _USER_METRIC_ACTION.match(text)
    if action:
        text = text[action.end():].strip(" 的，,、")
    for _ in range(8):
        before = text
        for pattern in (_USER_METRIC_FILLER, _USER_METRIC_OBJECT):
            match = pattern.match(text)
            if match:
                text = text[match.end():].strip(" 的，,、")
        if text == before:
            break
    segments = re.split(r"[的按在从与和及各每]", text)
    for part in reversed(segments):
        part = part.strip(" ，,、")
        if not part:
            continue
        # 片段以业务对象开头且剥掉后仍是指标短语时才剥，避免把
        # “医院覆盖率”这类指标本身的前缀误删。
        object_match = _USER_METRIC_OBJECT.match(part)
        if object_match:
            remainder = part[object_match.end():].strip(" 的，,、")
            if len(remainder) >= 4 and _METRIC_SUFFIX.search(remainder):
                part = remainder
        if 2 <= len(part) <= 16 and _METRIC_SUFFIX.search(part):
            return part
    return ""


def _plan_metric_names(plan: TaskPlan) -> list[str]:
    """计划里各数据查询任务声明的指标名，作为顶替检查的比对基准。"""
    names: list[str] = []
    for task in plan.tasks:
        extraction = task.extraction
        if not isinstance(extraction, dict):
            if "不再查询数据库" in (task.question or ""):
                names.extend(_calc_metric_names(task.question))
            continue
        for item in extraction.get("指标") or []:
            name = item.get("name") if isinstance(item, dict) else item
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        if "不再查询数据库" in (task.question or ""):
            names.extend(_calc_metric_names(task.question))
    return names


def _question_covers_metric(question: str, name: str) -> bool:
    """用户原话逐字提到该指标，或命中指标名里任一不少于3字的连续片段，视为覆盖。

    片段匹配是放宽方向：白话叫法（如“覆盖率”指“区域医院覆盖率”）不误判，
    而完全不相干的指标名在原话里不会留下任何片段。
    """
    if name in question:
        return True
    for size in range(len(name) - 1, 2, -1):
        for start in range(len(name) - size + 1):
            if name[start:start + size] in question:
                return True
    return False


class MetricSubstitutionDetected(RuntimeError):
    """概念词被拆成统计+计算任务：该说法在规范里无指标可派生，应转单一问题走口径解释。"""


# ---------------------------------------------------------------------------
# 前置判断器
# ---------------------------------------------------------------------------


def _analysis_goal_label(label, question):
    # The model label is authoritative; question text cannot overwrite it.
    return (label or '').strip() or None


COVERAGE_DEFINED = "口径已定义"
COVERAGE_UNDEFINED = "口径未定义"


def _normalize_schema_coverage(value) -> str:
    """模型可能照平台模板输出英文枚举，归一到中文结论；无法识别按已定义处理。"""
    text = str(value or "").strip()
    if text in (COVERAGE_UNDEFINED, "未覆盖", "not_covered"):
        return COVERAGE_UNDEFINED
    return COVERAGE_DEFINED



def _validate_model_routes(judgment, routes):
    from app.analysis.tools.registry import chain_for_intent, CHAINS
    from app.analysis.intents import primary_intent_from_label
    allowed = set(routes)
    entries = judgment.entries if judgment.is_compound else (judgment,)
    observed = set()
    for item in entries:
        intent = primary_intent_from_label(item.intent)
        chain = chain_for_intent(intent)
        if chain is not None:
            observed.add(chain)
    if observed != allowed:
        if observed and observed.issubset(allowed):
            # 判断模型未引入未确认链路，只是按更少的链路组织任务（如把
            # "对比A、B各季度趋势"判成按业态分组的单一趋势）。按其结构执行，
            # 确认后未被覆盖的链路本轮不落地。
            logger.warning(
                "compound judge covered subset of confirmed routes: routes=%s observed=%s",
                sorted(allowed), sorted(observed))
            return replace(judgment, analysis_routes=tuple(routes))
        if len(allowed) != 1 or len(observed) != 1:
            raise TaskPlanningError('ANALYSIS_ROUTE_CHANGED_AFTER_COMPLETION:' + str(sorted(allowed)) + '->' + str(sorted(observed)))
        # 补全阶段确认的链路是权威路由。判断模型偶尔会按问句字面把意图
        # 标成另一条链路（如两期份额变化标成对比）；单链路冲突时改回已确认
        # 链路的意图标签继续执行，空集或跨多链路冲突无法确定性对齐，仍按合同失败。
        confirmed = next(iter(allowed))
        confirmed_label = CHAINS[confirmed][0]
        logger.warning(
            "compound judge relabeled to confirmed chain: routes=%s observed=%s label=%s",
            sorted(allowed), sorted(observed), confirmed_label)
        if judgment.is_compound:
            entries = tuple(
                replace(entry, intent=confirmed_label)
                if chain_for_intent(primary_intent_from_label(entry.intent)) not in allowed
                and chain_for_intent(primary_intent_from_label(entry.intent)) is not None
                else entry
                for entry in judgment.entries)
            judgment = replace(judgment, entries=entries)
        else:
            judgment = replace(judgment, intent=confirmed_label)
    return replace(judgment, analysis_routes=tuple(routes))


def _analysis_planning_kwargs(judgment, intent):
    from app.analysis.tools.registry import chain_for_intent
    chain = chain_for_intent(intent)
    if judgment.analysis_routes is not None and chain in judgment.analysis_routes:
        return {'analysis_chain': chain}
    return {}


class CompoundQuestionJudge:
    """复合/单一前置判断：意图识别阶段执行，只判断和拆分子问题，不提取参数。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    async def judge(
        self,
        question: str,
        *,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        analysis_routes: list[str] | None = None,
    ) -> CompoundJudgment:
        """判断问题是否复合。判断模型失败时落确定性规则，规则也拆不出按单一处理。"""
        if (
            self.settings.intent_model_enabled
            and self.settings.multi_question_model_enabled
            and self.settings.intent_model_api_key
        ):
            for attempt in range(2):
                try:
                    judgment = await self._model_judge(
                        question,
                        semantic_context=semantic_context,
                        semantic_model_id=semantic_model_id,
                        **({'analysis_routes': analysis_routes} if analysis_routes is not None else {}),
                    )
                    from app.analysis.tools.registry import chain_for_intent
                    from app.analysis.intents import primary_intent_from_label
                    if not judgment.is_compound and not analysis_routes and chain_for_intent(primary_intent_from_label(judgment.intent)) is None:
                        # Frozen ordinary-query dependency repair. This cannot
                        # introduce an analysis chain or select any analysis tool.
                        precise=_ranked_relation_plan(question) or _parallel_ranking_plan(question)
                        if precise is not None and all(chain_for_intent(task.primary_intent) is None for task in precise.tasks):
                            validate_plan(precise,source_question=question)
                            result=self._judgment_from_rule_plan(precise)
                            return replace(result,analysis_routes=tuple(analysis_routes)) if analysis_routes is not None else result
                    if analysis_routes is not None:
                        return _validate_model_routes(judgment, analysis_routes)
                    if not judgment.is_compound:
                        return judgment
                    plan = _plan_from_judgment(judgment)
                    validate_plan(plan, source_question=question)
                    return judgment
                except (httpx.HTTPError, KeyError, ValueError, RuntimeError,
                        json.JSONDecodeError, TaskPlanningError) as exc:
                    # 传输、Schema 或落地校验失败先整体重试一次，仍失败才落规则
                    logger.warning(
                        "compound judge attempt failed: routes=%s attempt=%s error=%r",
                        analysis_routes, attempt, exc)
                    if attempt == 1:
                        break
        if analysis_routes is not None or (self.settings.intent_model_enabled
            and self.settings.multi_question_model_enabled and self.settings.intent_model_api_key):
            raise TaskPlanningError("模型意图合同未通过校验；未退回关键词路由")
        rule_plan = rule_split_plan(question, max_tasks=self.settings.multi_question_max_tasks)
        if rule_plan is None:
            return CompoundJudgment(is_compound=False, judge_source="DETERMINISTIC_RULE")
        from app.analysis.tools.registry import chain_for_intent
        if any(chain_for_intent(task.primary_intent) is not None for task in rule_plan.tasks):
            raise TaskPlanningError('MODEL_ANALYSIS_ROUTING_REQUIRED')
        validate_plan(rule_plan, source_question=question)
        return self._judgment_from_rule_plan(rule_plan)

    @staticmethod
    def _judgment_from_rule_plan(plan: TaskPlan) -> CompoundJudgment:
        order = {task.task_id: index for index, task in enumerate(plan.tasks)}
        entries = tuple(
            CompoundJudgmentEntry(
                question=task.question,
                depends_on=tuple(order[dep] for dep in task.depends_on if dep in order),
                expected_output=task.expected_output,
            )
            for task in plan.tasks
        )
        return CompoundJudgment(
            is_compound=True,
            entries=entries,
            analyze_summary=plan.analyze_summary,
            split_reason_code=plan.split_reason_code,
            shared_conditions=tuple(plan.shared_conditions),
            final_deliverable=plan.final_deliverable,
            judge_source="DETERMINISTIC_RULE",
        )

    async def _model_judge(
        self,
        question: str,
        *,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        analysis_routes: list[str] | None = None,
    ) -> CompoundJudgment:
        from app.domain.semantic_description import load_semantic_description

        from app.services.general_rules import rule_section, without_rule_section
        general_rules = rule_section(semantic_context)
        semantic_context = without_rule_section(semantic_context)
        # 上一轮任务表块不是业务语义规范：远程规范存在时不能跟着
        # semantic_context 一起被跳过，追问轮判断需要看到上轮结果
        prior_block = ""
        prior_marker = "[上一轮任务表]"
        if prior_marker in semantic_context:
            prior_start = semantic_context.index(prior_marker)
            prior_block = semantic_context[prior_start:]
            semantic_context = semantic_context[:prior_start].rstrip()

        schema = _JudgeResult.model_json_schema()
        from app.analysis.coverage import GUIDANCE
        system_content = _JUDGE_SYSTEM_PROMPT + '\n' + GUIDANCE
        if analysis_routes is None:
            from app.analysis.tools.registry import recognition_directory, ROUTING_INSTRUCTION
            system_content += '\n分析链路与工具名称：' + json.dumps(recognition_directory(), ensure_ascii=False)
            system_content += '\n依据上述目录语义判断意图，不按时间词或关键词猜测链路。'
        if analysis_routes is not None:
            from app.analysis.tools.registry import CHAINS
            system_content += "\n补全模型已确认的分析链路：" + json.dumps(
                [{"chain_id": key, "intent": CHAINS[key][0]} for key in analysis_routes], ensure_ascii=False)
            system_content += ("。分析链路不得重判，不得遗漏或新增。普通查询不要求分析。"
                "多条链路拆成各自独立交付子问题，分别保留适用条件；本阶段不选工具参数。\n")
        # 语义规范用于复合指标识别、业务域判定与字段覆盖度检查；
        # 注入链路与参数提取一致：远程描述文件 > 平台粘贴段 > 内置兜底。
        reference = await load_semantic_description(self.settings, semantic_model_id)
        remote_spec = (
            reference["content"]
            if reference["source"].startswith("semantic_model_")
            else ""
        )
        if remote_spec:
            system_content += "\n业务语义规范：\n" + remote_spec + "\n"
        elif semantic_context.strip():
            system_content += "\n业务语义规范：\n" + semantic_context + "\n"
        if prior_block:
            system_content += "\n" + prior_block + "\n"
        system_content += general_rules
        body = {
            "model": self.settings.intent_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_content + "\nJSON Schema：" + json.dumps(
                        schema, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "enable_thinking": False,
        }
        headers = {
            "Authorization": (
                "Bearer " + self.settings.intent_model_api_key.get_secret_value()
            ),
            "Content-Type": "application/json",
        }
        with trace_generation(
            name="compound-question-judge",
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
            raise ValueError("compound judge returned empty content")
        result = _JudgeResult.model_validate(json.loads(_strip_json_fences(content)))
        from app.analysis.coverage import source_field_gaps
        schema_coverage = _normalize_schema_coverage(result.schema_coverage)
        from app.analysis.intents import primary_intent_from_label, analysis_goals
        model_intents = (analysis_goals(routes=analysis_routes) if analysis_routes is not None
            else [primary_intent_from_label(item.intent) for item in result.sub_questions]
            if result.split else [primary_intent_from_label(result.intent)])
        unsupported_fields = source_field_gaps(question, result.unsupported_fields, intents=model_intents)
        if schema_coverage == COVERAGE_UNDEFINED and not unsupported_fields:
            # 未定义结论必须带具体字段名，否则中断话术没有可报内容，按已定义处理
            schema_coverage = COVERAGE_DEFINED
        judgment: CompoundJudgment
        if not result.split:
            if result.sub_questions:
                raise ValueError("single decision must not contain sub questions")
            judgment = CompoundJudgment(
                is_compound=False,
                intent=(result.intent if analysis_routes is not None else _analysis_goal_label(result.intent, question)),
                business_domain=(result.business_domain or "").strip() or None,
                schema_coverage=schema_coverage,
                unsupported_fields=unsupported_fields,
            )
        else:
            if len(result.sub_questions) < 2:
                raise ValueError("compound decision must contain at least two sub questions")
            entries = tuple(
                CompoundJudgmentEntry(
                    question=item.question.strip(),
                    label=(item.label.strip() if item.label and item.label.strip() else None),
                    intent=(item.intent if analysis_routes is not None else _analysis_goal_label(item.intent, item.question)),
                    business_domain=(item.business_domain or "").strip() or None,
                    expected_output=(
                        item.expected_output.strip()
                        if item.expected_output and item.expected_output.strip()
                        else None
                    ),
                )
                for item in result.sub_questions
            )
            summary = (result.analyze_summary or "").strip()[:200] or None
            judgment = CompoundJudgment(
                is_compound=True,
                entries=entries,
                analyze_summary=summary,
                split_reason_code=(result.split_reason_code or "").strip() or None,
                shared_conditions=tuple(dict.fromkeys(result.shared_conditions)),
                schema_coverage=schema_coverage,
                unsupported_fields=unsupported_fields,
            )
        return judgment


# ---------------------------------------------------------------------------
# 复合任务规划器
# ---------------------------------------------------------------------------


class CompoundTaskPlanner:
    """把前置判断拆出的子问题落成可执行计划：每个子问题独立做任务拆解与参数提取。"""

    def __init__(
        self,
        settings: Settings,
        single_planner: SingleQuestionPlanner,
    ) -> None:
        self.settings = settings
        self._single = single_planner

    async def plan(
        self,
        judgment: CompoundJudgment,
        *,
        source_question: str,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        root_intent: PrimaryIntent | None = None,
        intents_locked: bool = False,
    ) -> TaskPlan:
        if not judgment.is_compound or len(judgment.entries) < 2:
            raise TaskPlanningError("复合规划需要至少两个子问题")
        # 子问题互不依赖（拆解与提取只看子问题文本），全部并行。
        # 结构节点必须先于并行分支登记：子任务各自拷贝上下文，节点在
        # 并行前登记，各子问题规划才能挂到同一个任务拆分与规划节点下
        ensure_structural_span("task_planning", name="任务拆分与规划")
        from app.analysis.intents import primary_intent_from_label
        entry_intents = [
            primary_intent_from_label(entry.intent) or root_intent
            for entry in judgment.entries
        ]
        if intents_locked and any(intent is None for intent in entry_intents):
            raise TaskPlanningError("复合问题的每个子问题都必须携带上游意图")
        decompositions = await asyncio.gather(*(
            self._single.plan_question(
                entry.question,
                semantic_context=semantic_context,
                semantic_model_id=semantic_model_id,
                root_intent=entry_intent,
                **_analysis_planning_kwargs(judgment, entry_intent),
            )
            for entry, entry_intent in zip(judgment.entries, entry_intents, strict=True)
        ))
        tasks: list[AtomicTask] = []
        question_groups: list[QuestionTaskGroup] = []
        group_terminals: list[list[str]] = []
        for entry, entry_intent, decomposition in zip(
            judgment.entries, entry_intents, decompositions, strict=True,
        ):
            offset = len(tasks)
            entry_tasks = decomposition.tasks
            if not entry_tasks:
                # 提取模型不可用时按单任务兜底，任务无提取参数
                entry_tasks = (
                    DecomposedTask(question=entry.question),
                )
            from app.planning.analysis_task_planner import (
                ANALYSIS_PLAYBOOKS, build_analysis_task_plan,
                normalize_analysis_task_plan, standardize_period_analysis_plan,
                validate_task_plan_execution_contract,
            )
            from app.analysis.tools.compiler import compile_decomposition
            expanded = compile_decomposition(entry.question, decomposition)
            if expanded is None:
                expanded = standardize_period_analysis_plan(entry.question, decomposition)
            if expanded is None and len(entry_tasks) == 1 and not entry_tasks[0].depends_on:
                original = entry_tasks[0]
                expanded = build_analysis_task_plan(entry.question, PlannerExtraction(
                    intent=original.intent or decomposition.intent,
                    parameters=list(original.parameters), structured=original.structured),
                    task_question=original.question, task_name=original.name)
            if expanded is None and entry_intent in ANALYSIS_PLAYBOOKS and len(entry_tasks) >= 2:
                candidate = TaskPlan(tasks=[
                    AtomicTask(
                        task_id=f"task-{index + 1}", question=task.question,
                        name=task.name,
                        depends_on=[f"task-{dependency + 1}" for dependency in task.depends_on],
                        expected_output=task.expected_output,
                        parameters=list(task.parameters), extraction=task.structured,
                        primary_intent=task.intent or decomposition.intent,
                    )
                    for index, task in enumerate(entry_tasks)
                ], planner="STRUCTURED_MODEL")
                expanded = normalize_analysis_task_plan(entry.question, candidate, entry_intent)
            if expanded is not None:
                validate_task_plan_execution_contract(expanded, entry_intent)
            if expanded is not None:
                indexes = {task.task_id: index for index, task in enumerate(expanded.tasks)}
                entry_tasks = tuple(DecomposedTask(question=task.question, name=task.name,
                    depends_on=tuple(indexes[dep] for dep in task.depends_on),
                    expected_output=task.expected_output, intent=task.primary_intent,
                    parameters=tuple(task.parameters), structured=task.extraction)
                    for task in expanded.tasks)
            for local_index, task in enumerate(entry_tasks):
                task_question = task.question or entry.question
                expected_output = task.expected_output or entry.expected_output
                if expected_output is None and "不再查询数据库" in task_question:
                    expected_output = (
                        "、".join(declared_calculation_targets(task_question)) or None
                    )
                depends_on = [f"task-{offset + dep + 1}" for dep in task.depends_on]
                if not task.depends_on:
                    # A preceding question may have expanded into several
                    # queries and a calculation. Depend on its terminal output.
                    for dep in entry.depends_on:
                        if not 0 <= dep < len(group_terminals):
                            raise TaskPlanningError('问题依赖必须引用前序问题')
                        depends_on.extend(group_terminals[dep])
                    depends_on = list(dict.fromkeys(depends_on))
                structured = copy.deepcopy(task.structured)
                analysis_binding = (structured or {}).get('分析绑定')
                if analysis_binding:
                    def rebase(task_id):
                        return f"task-{offset + int(task_id.split('-')[-1])}"
                    for period in analysis_binding.get('periods') or []:
                        period['task'] = rebase(period['task'])
                    analysis_binding['optional_dependencies'] = [rebase(dep) for dep in analysis_binding.get('optional_dependencies') or []]
                tasks.append(AtomicTask(
                    task_id=f"task-{offset + local_index + 1}",
                    question=task_question.strip(),
                    name=task.name,
                    depends_on=depends_on,
                    expected_output=expected_output,
                    parameters=list(task.parameters),
                    extraction=materialize_computation_binding(structured,
                        task_index=local_index, task_offset=offset, dependencies=depends_on),
                    primary_intent=task.intent or decomposition.intent,
                ))
            referenced = {dep for task in entry_tasks for dep in task.depends_on}
            group_terminals.append([f'task-{offset + index + 1}'
                                    for index in range(len(entry_tasks)) if index not in referenced])
            question_groups.append(QuestionTaskGroup(
                question=entry.question.strip(),
                label=entry.label,
                task_offset=offset,
                task_count=len(entry_tasks),
            ))
        plan = TaskPlan(
            tasks=tasks,
            planner=(
                "STRUCTURED_MODEL"
                if judgment.judge_source == "STRUCTURED_MODEL"
                else "DETERMINISTIC_RULE"
            ),
            split_reason_code=judgment.split_reason_code or "UNSPECIFIED",
            shared_conditions=list(dict.fromkeys(judgment.shared_conditions)),
            analyze_summary=judgment.analyze_summary,
            final_deliverable=judgment.final_deliverable,
            question_groups=question_groups,
            compound_metrics=_compound_metrics_in_decompositions(decompositions),
        )
        if len(tasks) > 12:
            # 子问题数×每题任务数超出计划上限时按子问题截断，
            # 剩余子问题让用户分轮提问，不能让整轮规划直接失败
            keep_entries = 0
            kept = 0
            for group in question_groups:
                if kept + group.task_count > 12:
                    break
                keep_entries += 1
                kept += group.task_count
            if keep_entries < 2:
                raise TaskPlanningError(
                    "复合问题拆分后的任务数超出执行上限，请减少问题中的业务目标后重试"
                )
            tasks = tasks[:kept]
            question_groups = question_groups[:keep_entries]
            plan = TaskPlan(
                tasks=tasks,
                planner=(
                    "STRUCTURED_MODEL"
                    if judgment.judge_source == "STRUCTURED_MODEL"
                    else "DETERMINISTIC_RULE"
                ),
                split_reason_code=judgment.split_reason_code or "UNSPECIFIED",
                shared_conditions=list(dict.fromkeys(judgment.shared_conditions)),
                analyze_summary=(
                    (judgment.analyze_summary or "")
                    + f"；任务数超出上限，本轮仅执行前{keep_entries}个子问题，其余请分开提问"
                ),
                final_deliverable=judgment.final_deliverable,
                question_groups=question_groups,
            )
        validate_plan(plan, source_question=source_question, registered_default_times=tuple(
            dict.fromkeys(default for decomposition in decompositions
                          for default in decomposition.grounding_default_times)),
            registered_conditions=tuple(dict.fromkeys(condition for decomposition in decompositions
                for condition in decomposition.grounding_registered_conditions)))
        self._reject_metric_substitution(plan, source_question)
        return plan

    @staticmethod
    def _reject_metric_substitution(plan: TaskPlan, source_question: str) -> None:
        """计算任务的指标在用户原话里毫无踪影时判定为相近指标顶替。

        拆分判断器提示词约束不住模型时在这里确定性拦截：含计算任务的计划，
        其声明指标必须在原话里出现（逐字或不少于3字的连续片段），否则整个
        拆分作废，转单一问题按指标口径解释处理。只拦带计算任务的计划，
        普通查询计划不动，避免误伤白话问法。
        """
        if not any("不再查询数据库" in (task.question or "") for task in plan.tasks):
            return
        names = _plan_metric_names(plan)
        if not names:
            return
        if any(_question_covers_metric(source_question, name) for name in names):
            return
        raise MetricSubstitutionDetected(source_question)


# ---------------------------------------------------------------------------
# 路由门面：编排器的单一入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompoundRouteResult:
    """路由结果：判断结论 + 复合计划或单一提取，二者只居其一。"""

    judgment: CompoundJudgment
    plan: TaskPlan | None = None
    single: PlannerOutcome | None = None


def _question_annotation(intent: str | None, business_domain: str | None) -> str:
    """单个问题的意图与业务域标注，判断器没给出的项不展示。"""
    parts: list[str] = []
    if intent:
        parts.append(f"意图：{intent}")
    if business_domain:
        parts.append(f"业务域：{business_domain}")
    return "，".join(parts)


# 子问题文本尾部统一成句号，避免"统计各经销商的订单笔数？"这类问号混在陈述句里
_SUBQUESTION_TAIL_PUNCT = "。．.!！?？;；,， "


def _subquestion_statement(entry: CompoundJudgmentEntry) -> str:
    """子问题一行：问题文本在前，意图与业务域字段标注跟在后面，缺项跳过。"""
    question = str(entry.question or "").strip().rstrip(_SUBQUESTION_TAIL_PUNCT)
    parts: list[str] = []
    if question:
        parts.append(question)
    if entry.intent:
        parts.append(f"意图：{entry.intent}")
    if entry.business_domain:
        parts.append(f"业务域：{entry.business_domain}")
    if not parts:
        return ""
    return "，".join(parts) + "。"


def judgment_display_section(judgment: CompoundJudgment | None) -> str:
    """意图识别节点展示的复合/单一判断段：意图、业务域与拆分出的子问题。"""
    if judgment is None:
        return ""
    if not judgment.is_compound:
        annotation = _question_annotation(
            judgment.intent, judgment.business_domain
        )
        if annotation:
            return f"{annotation}。"
        return ""
    lines = [f"问题拆分为{len(judgment.entries)}个独立子问题："]
    lines.extend(
        f"问题{index}：{_subquestion_statement(entry)}"
        for index, entry in enumerate(judgment.entries, 1)
    )
    return "\n".join(lines)


class CompoundQuestionRouter:
    """前置判断 + 分发的单一入口：复合走 CompoundTaskPlanner，单一走 SingleQuestionPlanner。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._judge = CompoundQuestionJudge(settings, transport=transport)
        self._single = SingleQuestionPlanner(settings, transport=transport)
        self._planner = CompoundTaskPlanner(settings, self._single)

    @property
    def single_planner(self) -> SingleQuestionPlanner:
        return self._single

    async def judge_only(
        self,
        question: str,
        *,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
    ) -> CompoundJudgment:
        """只做复合/单一前置判断，不分发执行。供意图识别节点提前展示判断结论。"""
        return await self._judge.judge(
            question,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
        )

    async def plan_with_judgment(
        self,
        judgment: CompoundJudgment,
        question: str,
        *,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        root_intent: PrimaryIntent | None = None,
        intents_locked: bool = False,
    ) -> CompoundRouteResult:
        """按既有判断结论分发：复合走 CompoundTaskPlanner，单一走 SingleQuestionPlanner。

        root_intent 是意图识别节点的根意图，透传给全部任务拆解，
        规划阶段不再做意图判断。
        """
        if judgment.is_compound:
            try:
                plan = await self._planner.plan(
                    judgment,
                    source_question=question,
                    semantic_context=semantic_context,
                    semantic_model_id=semantic_model_id,
                    root_intent=root_intent,
                    intents_locked=intents_locked,
                )
            except MetricSubstitutionDetected:
                # 概念词被误拆成统计+计算：整个拆分作废，
                # 按单一问题走单一路径（会判成指标口径解释）
                judgment = CompoundJudgment(is_compound=False)
                return await self._route_single(
                    question,
                    judgment=judgment,
                    semantic_context=semantic_context,
                    semantic_model_id=semantic_model_id,
                    root_intent=root_intent,
                )
            return CompoundRouteResult(judgment=judgment, plan=plan)
        return await self._route_single(
            question,
            judgment=judgment,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
            root_intent=root_intent,
        )

    async def route_and_plan(
        self,
        question: str,
        *,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        root_intent: PrimaryIntent | None = None,
        intents_locked: bool = False,
    ) -> CompoundRouteResult:
        judgment = await self.judge_only(
            question,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
        )
        return await self.plan_with_judgment(
            judgment,
            question,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
            root_intent=root_intent,
            intents_locked=intents_locked,
        )

    async def _route_single(
        self,
        question: str,
        *,
        judgment: CompoundJudgment,
        semantic_context: str = "",
        semantic_model_id: int | None = None,
        root_intent: PrimaryIntent | None = None,
    ) -> CompoundRouteResult:
        if root_intent is None:
            from app.analysis.intents import primary_intent_from_label
            root_intent = primary_intent_from_label(judgment.intent)
        decomposition = await self._single.plan_question(
            question,
            semantic_context=semantic_context,
            semantic_model_id=semantic_model_id,
            root_intent=root_intent,
            **_analysis_planning_kwargs(judgment, root_intent),
        )
        from app.analysis.tools.compiler import compile_decomposition
        catalog_plan = compile_decomposition(question, decomposition)
        if catalog_plan is not None:
            validate_plan(catalog_plan, source_question=question)
            return CompoundRouteResult(judgment=judgment, plan=catalog_plan)
        if len(decomposition.tasks) <= 1:
            first = next(iter(decomposition.tasks), None)
            outcome = PlannerOutcome(
                single_intent=(
                    (first.intent if first is not None else None)
                    or decomposition.intent
                ),
                single_parameters=(
                    first.parameters if first is not None else ()
                ),
                single_structured=(
                    first.structured if first is not None else None
                ),
                single_task_name=(
                    first.name if first is not None else None
                ),
                single_task_question=(
                    first.question if first is not None else None
                ),
                metric_catalog=decomposition.metric_catalog,
            )
            return CompoundRouteResult(judgment=judgment, single=outcome)
        from app.planning.analysis_task_planner import standardize_period_analysis_plan
        canonical_analysis = standardize_period_analysis_plan(question, decomposition)
        if canonical_analysis is not None:
            validate_plan(canonical_analysis, source_question=question)
            return CompoundRouteResult(judgment=judgment, plan=canonical_analysis)
        # 单一问题拆解出多个任务：与复合计划同样按任务DAG执行
        tasks = [
            AtomicTask(
                task_id=f"task-{index + 1}",
                question=task.question.strip(),
                name=task.name,
                depends_on=[f"task-{dep + 1}" for dep in task.depends_on],
                expected_output=task.expected_output,
                parameters=list(task.parameters),
                extraction=materialize_computation_binding(task.structured, task_index=index,
                    dependencies=[f"task-{dep + 1}" for dep in task.depends_on]),
                primary_intent=task.intent or decomposition.intent,
            )
            for index, task in enumerate(decomposition.tasks)
        ]
        plan = TaskPlan(
            tasks=tasks,
            planner="STRUCTURED_MODEL",
            split_reason_code="TASK_DECOMPOSITION",
            compound_metrics=_compound_metrics_in_decompositions([decomposition]),
        )
        validate_plan(plan, source_question=question,
                      registered_default_times=decomposition.grounding_default_times,
                      registered_conditions=decomposition.grounding_registered_conditions)
        return CompoundRouteResult(judgment=judgment, plan=plan)


def _compound_metrics_in_decompositions(
    decompositions: list[QuestionDecomposition],
) -> list[str]:
    """拆分结果里出现的复合指标名：指标命中规范依赖指标映射时登记。

    计算任务的指标名保留用户原文叫法，归一到登记名后再比对；
    跨拆解结果去重并保持首次出现顺序。
    """
    compound: list[str] = []
    for decomposition in decompositions:
        dependencies = decomposition.metric_dependencies
        catalog = decomposition.metric_catalog
        if not dependencies or catalog is None:
            continue
        registered, aliases = catalog
        for task in decomposition.tasks:
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
                if normalized in dependencies and normalized not in compound:
                    compound.append(normalized)
    return compound
