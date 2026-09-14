"""Context-only TaskState for questions executed by the original V1 chain.

The live bridge sometimes cannot lower a self-contained business question to a
V2 execution payload even though V1 can execute it.  After V1 returns a real
query result, this module retains only the conversational meaning required to
complete later edits.  It never stores or reconstructs V1 authorization,
retrieval, database, dataset or execution contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.domain.models import (
    AgentResponse,
    CanonicalAnalysisRequest,
    ChatRequest,
    SemanticFilterBinding,
    TrustedIdentity,
)

from . import models as m
from .authorized_contract import ScopedArtifact, contract_digest, scoped_artifact_material
from .catalog_bridge import ScopedPlanSession
from .enums import CatalogType
from .explicit_time import normalize_range
from .pending_recognition import governed_aliases
from .state_machine import ConversationState, TaskState, TaskVersion, TopicState


_QUERY_VERBS = re.compile(
    r"查询|统计|分析|列出|查找|查看|计算|比较|对比|生成|预测|多少|哪些|为什么|怎么"
)
_EXPLICIT_QUERY_VERBS = re.compile(
    r"查询|统计|分析|列出|查找|查看|计算|比较|对比|生成|预测"
)
_SELF_CONTAINED_RELATION_QUERY = re.compile(
    r"^\s*.{2,}?(?:销售|采购|购买|合作|供货|供应|使用|拥有|分布)"
    r".{0,20}?(?:哪些|什么|多少|几家|名单|排名|情况)"
)
_CONTEXT_DEPENDENT_SURFACE = re.compile(
    r"^\s*(?:那|那么|这些|那些|上述|它|它们|该对象|这个对象|那个对象|"
    r"继续|再|不要|去掉|删除|取消|换|改|返回)"
)
_ELLIPTICAL_VALUE = re.compile(
    r"^\s*(?:那|那么)?\s*(?P<value>.+?)\s*(?:呢|怎么样|如何)\s*[？?。.]?\s*$"
)
_EXPLICIT_FILTER_REPLACEMENT = re.compile(
    r"^\s*(?:那|那么)?\s*(?:把\s*)?"
    r"(?:(?:产品|商品|地区|区域|省份|城市|医院|经销商|供应商|客户|厂家|厂商|品牌)\s*)?"
    r"(?:换成|改成|改为|换为|替换成|替换为)\s*"
    r"(?P<value>.+?)\s*(?:呢)?\s*[？?。.]?\s*$"
)
_FILTER_RESTRICTION = re.compile(
    r"^\s*(?:只|仅)(?:看|查|查询|要|统计)?\s*(?P<value>.+?)"
    r"\s*(?:的)?\s*[？?。.]?\s*$"
)
_TIME_ONLY = re.compile(
    r"^\s*(?:(?:把)?时间)?\s*(?:换成|改成|改为|换到|改到|换|改)?\s*"
    r"(?P<value>(?:19|20)\d{2}年?|今年|去年|明年)\s*(?:呢)?\s*[？?。.]?\s*$"
)
_TIME_RANGE_SURFACE = re.compile(
    r"(?:19|20)\d{2}年|今年|去年|明年|本年|"
    r"(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?"
)
_TIME_GRAIN_FOLLOWUP = re.compile(
    r"^\s*(?P<replace>换成|改成|改为|换为)?\s*按\s*"
    r"(?P<grain>年|年度|季度|季|月|月份|周|星期|日|天)\s*"
    r"(?P<verb>看|查看|查询|统计|汇总|分析|展示|显示)?\s*"
    r"(?P<body>.*?)\s*[？?。.]?\s*$"
)
_GENERIC_OBJECT_REFERENCES = (
    "这些对象", "那些对象", "这个对象", "那个对象", "该对象", "它们", "它",
)
_OBJECT_RANKING_FOLLOWUP = re.compile(
    r"^\s*(?:它|该对象|这个对象|那个对象)\s*在?\s*哪个\s*"
    r"(?P<dimension>省份|省|城市|市|地区|区域)\s*"
    r"(?:卖得|销售得)?\s*(?P<direction>最好|最高|最多|最低|最少)\s*[？?。.]?\s*$"
)
_DIMENSION_RANKING_FOLLOWUP = re.compile(
    r"^\s*(?:那|那么)?\s*(?:(?:哪个|哪一个)\s*(?P<dimension_a>[^的？?。]{1,20})\s*"
    r"(?P<direction_a>最高|最多|最大|最低|最少|最小)|"
    r"(?P<direction_b>最高|最多|最大|最低|最少|最小)\s*的\s*"
    r"(?P<dimension_b>[^呢？?。]{1,20}))\s*(?:呢)?\s*[？?。.]?\s*$"
)
_RESULT_COUNT_FOLLOWUP = re.compile(
    r"^\s*(?:那|那么|这些|它们)?\s*(?:一共|总共)?\s*(?:有)?\s*"
    r"(?:多少|几)\s*(?:家|个|条)?\s*(?P<target>[^呢？?。]{1,30})\s*"
    r"(?:呢)?\s*[？?。.]?\s*$"
)
_SORT_FOLLOWUP = re.compile(
    r"^\s*按\s*(?P<metric>.+?)\s*"
    r"(?P<direction>从高到低|降序|从低到高|升序)\s*排序\s*[？?。.]?\s*$"
)
_RANKED_ITEM_METRIC_FOLLOWUP = re.compile(
    r"^\s*(?:那)?\s*(?P<rank>第一名|第1名|排名第一)\s*的?\s*"
    r"(?P<metric>.+?)\s*(?:是多少|有多少|多少)?\s*[？?。.]?\s*$"
)
_METRIC_ONLY_FOLLOWUP = re.compile(
    r"^\s*(?:那|那么|再)?\s*(?P<metric>.+?)\s*"
    r"(?:是多少|有多少|多少|呢)\s*[？?。.]?\s*$"
)
_RELATION_TARGET_ELLIPSIS = re.compile(
    r"^\s*(?:那|那么|再)?\s*(?P<body>"
    r"(?:主要)?(?:向|给)\s*(?:哪些|什么|哪几家)?\s*"
    r"(?P<target_a>医院|经销商|厂家|供应商|客户)\s*(?:供货|销售|合作)?|"
    r"(?:合作的?|关联的?|往来的?)\s*"
    r"(?P<target_b>厂家|供应商|医院|经销商|客户)\s*"
    r"(?:有|是)?\s*(?:哪些|什么|哪几家)|"
    r"(?:采购|购买|销售|卖出)\s*(?:了)?\s*(?:哪些|什么)\s*"
    r"(?P<target_c>产品|商品)"
    r")\s*[？?。.]?\s*$"
)
_RECENT_PAIR_TOTAL = re.compile(
    r"^\s*(?:这|那)\s*(?:两|2)\s*个?\s*"
    r"(?P<family>省份|省|地区|区域|产品|商品|医院|经销商|供应商)\s*"
    r"(?:加起来|合计|总共)\s*(?:是)?\s*(?:多少|多少呢)?\s*[？?。.]?\s*$"
)
_FILTER_CLEAR = re.compile(
    r"^\s*(?:那|那么)?\s*(?:"
    r"(?P<nationwide>全国)(?:整体|总体)?|"
    r"(?:不限|不限制|不看|不分)(?P<unlimited>地区|区域|省份|城市|产品|商品|医院|经销商|供应商)|"
    r"(?:全部|所有)(?P<all_values>地区|区域|省份|城市|产品|商品|医院|经销商|供应商)"
    r")\s*(?:呢)?\s*[？?。.]?\s*$"
)
_RECENT_TASK_PAIR_COMPARISON = re.compile(
    r"^\s*(?:这|那)\s*(?:两|2)\s*个?\s*"
    r"(?P<family>产品|商品|医院|经销商|供应商)\s*"
    r"(?:谁|哪个|哪一个|哪家)\s*的?\s*(?P<metric>.+?)\s*"
    r"(?P<direction>更高|更多|更大|更低|更少|更小)\s*[？?。.]?\s*$"
)
_TOP_GROUP_DRILLDOWN = re.compile(
    r"^\s*(?:排|排名)\s*(?:第)?(?:一|1)(?:名)?\s*的?\s*"
    r"(?P<dimension>省份|省|城市|市|地区|区域)\s*(?:里|中)\s*[，,]?\s*"
    r"(?P<body>(?:有)?哪些(?P<target>医院|经销商|产品|商品|厂家|供应商|客户).+?)"
    r"\s*[？?。.]?\s*$"
)
_RESULT_SET_EXTREME = re.compile(
    r"^\s*(?:这些|那些|上述)\s*(?P<target>医院|经销商|产品|商品|厂家|供应商|客户)"
    r"\s*(?:里|中)?\s*(?P<metric>.+?)\s*"
    r"(?P<direction>最高|最多|最大|最低|最少|最小)\s*的?\s*"
    r"(?:是)?(?:哪家|哪个|哪一个|什么)\s*[？?。.]?\s*$"
)
_RETURN_TO_NAMED_ENTITY_METRIC = re.compile(
    r"^\s*(?:再)?\s*(?:回到|返回到|切回|回看)\s*"
    r"(?P<value>[^，,。.!！?？]{2,200})\s*[，,]\s*"
    r"(?:它|该对象|这个对象|那个对象)?\s*(?:的)?\s*"
    r"(?P<metric>.+?)\s*(?:是多少|有多少|多少)\s*[？?。.]?\s*$"
)
_RELATION_ACTOR_QUERY = re.compile(
    r"^\s*(?:查询|查看|查找|列出)?\s*(?P<actor>.+?)\s*"
    r"(?:供货|供应|合作|采购|购买|销售)(?:的|了)?\s*"
    r"(?:哪些|什么|哪几家)?\s*"
    r"(?:医院|经销商|产品|商品|客户|厂家|厂商|供应商)"
    r"(?:名单|清单)?\s*[？?。.]?\s*$"
)
_SINGULAR_ACTOR_METRIC = re.compile(
    r"^\s*(?:那|那么)?\s*(?:它|该对象|这个对象|那个对象)\s*(?:的)?\s*"
    r"(?P<metric>.+?)\s*(?:是多少|有多少|多少)\s*[？?。.]?\s*$"
)
_RELATION_RESULT_TARGET = re.compile(
    r"(?:哪些|什么|哪几家)\s*(?P<after>医院|经销商|产品|商品|厂家|供应商|客户)|"
    r"(?P<before>医院|经销商|产品|商品|厂家|供应商|客户)\s*(?:有)?哪些"
)

_FILTER_FAMILIES = {
    "COMMERCIAL_PRODUCT": (
        "product_name", "goods_name", "商品名称", "产品名称", "商品品牌", "产品品牌",
        "parent_brand", "母品牌", "母厂牌", "brand", "厂家", "manufacturer",
        "商品分类", "产品分类", "category", "品类",
    ),
    "REGION": (
        "province", "city", "region", "area", "省份", "城市", "地区", "区域",
    ),
    "HOSPITAL": (
        "hospital", "医院", "护理院", "卫生服务中心", "卫生院",
    ),
    "PARTNER": (
        "dealer", "distributor", "supplier", "vendor", "经销商", "供应商",
    ),
}

_CONTEXT_REFERENCE_SURFACES = {
    "REGION": (
        "该省份", "这个省份", "那个省份", "本省",
        "该地区", "这个地区", "那个地区", "当地",
        "该城市", "这个城市", "那个城市",
    ),
    "COMMERCIAL_PRODUCT": (
        "该产品", "这个产品", "那个产品",
        "该商品", "这个商品", "那个商品",
    ),
    "HOSPITAL": (
        "该医院", "这个医院", "那个医院", "这家医院", "那家医院",
    ),
    "PARTNER": (
        "该经销商", "这个经销商", "那个经销商",
        "该供应商", "这个供应商", "那个供应商",
    ),
}

_CONTEXT_FAMILY_LABELS = {
    "REGION": "地区",
    "COMMERCIAL_PRODUCT": "产品",
    "HOSPITAL": "医院",
    "PARTNER": "合作方",
}


ContextValueResolver = Callable[
    [str, str, str | None], Awaitable[SemanticFilterBinding | None]
]


@dataclass(frozen=True)
class ContextQuestionResolution:
    completed_question: str
    next_state: ScopedArtifact
    relation: str
    operation: str
    slot: str
    understanding: str


@dataclass(frozen=True)
class ContextReferenceResolution:
    """A full current question after resolving explicit contextual references."""

    completed_question: str | None
    next_state: ScopedArtifact | None
    understanding: str | None = None
    clarification_question: str | None = None
    referenced_families: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DirectCompletion:
    completed_question: str
    label: str
    metrics: tuple[str, ...] | None = None
    dimensions: tuple[str, ...] | None = None
    filters: tuple[m.ContextQuestionFilter, ...] | None = None
    operation: str | None = None
    evidence_surface: str | None = None
    reset_detail_shape: bool = False
    result_entity: str | None = None
    replace_result_identity: bool = False


def _text(value: object) -> str:
    return str(value or "").strip().casefold()


def _has_current_antecedent_before_reference(
    question: str, markers: tuple[str, ...]
) -> bool:
    """Do not let a history shortcut replace an explicit same-turn object."""

    discourse_only = re.compile(
        r"^(?:请|帮我|给我|查询|查看|统计|看看|再|再看|再查|"
        r"再回到|回到|关于|对于|至于|那|那么)*$"
    )
    for marker in markers:
        before, separator, _ = question.partition(marker)
        if not separator:
            continue
        candidate = re.sub(r"[，,。.!！?？;；：:\s]+", "", before)
        if candidate and discourse_only.fullmatch(candidate) is None:
            return True
    return False


def semantic_filter_family(*values: object) -> str:
    material = " ".join(_text(value) for value in values if value is not None)
    matches = [
        family
        for family, markers in _FILTER_FAMILIES.items()
        if any(_text(marker) in material for marker in markers)
    ]
    return matches[0] if len(matches) == 1 else "OTHER"


def _time_surface(parse, question: str) -> str | None:
    candidates = []
    for mention in getattr(parse, "mentions", ()):
        roles = {str(getattr(role, "value", role)) for role in mention.candidate_roles}
        if roles & {"TIME_RANGE", "TIME_GRAIN", "TIME_FIELD", "COMPARISON_BASELINE"}:
            if question.count(mention.surface) == 1:
                candidates.append(mention.surface)
    values = list(dict.fromkeys(candidates))
    return values[0] if len(values) == 1 else None


def _filters_from_v1(
    question: str, request: CanonicalAnalysisRequest | None
) -> list[m.ContextQuestionFilter]:
    if request is None:
        return []
    result: list[m.ContextQuestionFilter] = []
    for binding in request.semantic_filter_bindings:
        if binding.filter_index >= len(request.filters):
            continue
        surface = binding.input_value.strip()
        if not surface or question.count(surface) != 1:
            continue
        family = semantic_filter_family(binding.attribute_code, binding.canonical_name)
        if family == "OTHER":
            continue
        result.append(m.ContextQuestionFilter(
            surface=surface,
            canonical_value=binding.canonical_value,
            canonical_name=binding.canonical_name,
            attribute_code=binding.attribute_code,
            semantic_family=family,
            evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
        ))
    if result:
        return list({(item.surface, item.semantic_family): item for item in result}.values())
    for item in request.filters:
        if not isinstance(item, dict):
            continue
        raw = item.get("value")
        values = raw if isinstance(raw, list) else [raw]
        field = str(item.get("field") or "").strip()
        family = semantic_filter_family(field)
        if family == "OTHER":
            continue
        for value in values:
            surface = str(value or "").strip()
            if surface and question.count(surface) == 1:
                result.append(m.ContextQuestionFilter(
                    surface=surface,
                    canonical_value=surface,
                    canonical_name=field or None,
                    attribute_code=None,
                    semantic_family=family,
                    evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
                ))
    return list({(item.surface, item.semantic_family): item for item in result}.values())


def canonical_matches_execution(
    request: CanonicalAnalysisRequest | None,
    *,
    chat: ChatRequest,
    identity: TrustedIdentity,
    response: AgentResponse | None = None,
) -> bool:
    """Accept V1 evidence only for the exact trusted request just executed."""
    dataset_matches = bool(
        request is not None and request.source_dataset_id == chat.dataset_id
    )
    if (
        not dataset_matches
        and request is not None
        and chat.dataset_id is None
        and response is not None
    ):
        # A successful database execution attaches its newly-created output
        # Dataset to the persisted Canonical request.  Prove that mutation with
        # both identifiers from the exact response instead of rejecting useful
        # semantic evidence merely because the caller had no input Dataset.
        dataset_matches = bool(
            response.dataset_id is not None
            and request.source_dataset_id == response.dataset_id
            and request.request_id == response.request_id
        )
    return bool(
        request is not None
        and request.tenant_id == identity.tenant_id
        and request.user_id == identity.user_id
        and request.application_id == chat.application_id
        and request.conversation_id == chat.conversation_id
        and request.semantic_model_id == chat.semantic_model_id
        and request.business_domain_ids == chat.business_domain_ids
        and request.database_id == chat.database_id
        and request.knowledge_base_names == chat.knowledge_base_names
        and dataset_matches
        and request.original_question == chat.question
    )


def build_context_question(
    *,
    chat: ChatRequest,
    parse,
    v1_request: CanonicalAnalysisRequest | None,
    catalog_version: str | None,
    original_question: str | None = None,
) -> m.ContextQuestionState:
    request = v1_request
    time = None
    if request is not None and request.time_range is not None:
        surface = _time_surface(parse, chat.question)
        if surface is not None:
            time = m.ContextQuestionTime(
                surface=surface,
                start=request.time_range.start,
                end_exclusive=request.time_range.end_exclusive,
                evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
            )
    return m.ContextQuestionState(
        original_question=original_question or chat.question,
        execution_question=chat.question,
        primary_intent=(
            str(getattr(request.primary_intent, "value", request.primary_intent))
            if request is not None else None
        ),
        entity=request.entity if request is not None else None,
        metrics=(
            [item.canonical_name or item.input for item in request.metrics]
            if request is not None else []
        ),
        dimensions=list(request.dimensions) if request is not None else [],
        fields=list(request.fields) if request is not None else [],
        filters=_filters_from_v1(chat.question, request),
        time=time,
        source_message_id=chat.message_id,
        v1_request_id=str(request.request_id) if request is not None else None,
        semantic_catalog_version=catalog_version,
    )


async def refine_context_question_relation_actor(
    frame: m.ContextQuestionState,
    *,
    resolve_value: ContextValueResolver | None,
) -> m.ContextQuestionState:
    """Prefer a proven current-turn relation actor over a malformed V1 filter.

    V1 Canonical output remains execution evidence, but it is not allowed to
    turn a relation verb into part of an entity value.  The replacement is made
    only when V1's existing value resolver uniquely proves the actor surface.
    """

    match = _RELATION_ACTOR_QUERY.fullmatch(frame.execution_question)
    if match is None:
        return frame
    surface = match.group("actor").strip().removesuffix("的").strip()
    resolved = await _resolve_filter_surface(surface, resolve_value)
    if resolved is None:
        return frame
    family, binding = resolved
    replacement = _current_filter(
        surface=surface, family=family, binding=binding
    )
    relation_markers = ("供货", "供应", "合作", "采购", "购买", "销售")
    retained = [
        item for item in frame.filters
        if item.semantic_family != family
        and not any(marker in item.surface for marker in relation_markers)
    ]
    return frame.model_copy(deep=True, update={"filters": [*retained, replacement]})


def publish_context_task(
    barrier: ScopedArtifact,
    *,
    chat: ChatRequest,
    frame: m.ContextQuestionState,
    created_at: datetime,
) -> ScopedArtifact:
    """Replace a reserved barrier with one V2-owned context task at same CAS version."""
    state = ConversationState.model_validate(barrier.payload)
    task_id = "task:" + chat.message_id
    topic_id = "topic:" + chat.message_id
    if task_id in state.tasks or topic_id in state.topics:
        raise ValueError("V2_CONTEXT_QUESTION_IDENTITY_REUSED")
    data = state.model_dump(mode="python")
    task = TaskState(
        task_id=task_id,
        topic_id=topic_id,
        active_version=1,
        status="PROVISIONAL",
        versions=[TaskVersion(
            version=1,
            status="PROVISIONAL",
            semantics=m.TaskSemanticState(),
            context_question=frame,
            current_turn_ref=chat.message_id,
            current_turn_digest=contract_digest(chat.question),
            created_at=created_at,
        )],
    )
    topic = TopicState(
        topic_id=topic_id,
        title=frame.execution_question[:500],
        active_task_id=task_id,
        task_ids=[task_id],
        last_accessed_at=created_at,
    )
    data["tasks"][task_id] = task.model_dump(mode="python")
    data["topics"][topic_id] = topic.model_dump(mode="python")
    data["active_topic_id"] = topic_id
    data["topic_stack"] = [*data["topic_stack"], topic_id]
    next_state = ConversationState.model_validate(data)
    payload = next_state.model_dump(mode="json")
    return ScopedArtifact(
        kind="CONVERSATION",
        context=barrier.context,
        payload=payload,
        source_value_bindings=barrier.source_value_bindings,
        payload_digest=contract_digest(
            scoped_artifact_material(payload, barrier.source_value_bindings)
        ),
    )


def _active_task_version(state: ConversationState):
    topic = state.topics.get(state.active_topic_id)
    if topic is None or topic.active_task_id is None:
        return None
    task = state.tasks.get(topic.active_task_id)
    if task is None:
        return None
    version = next(
        (item for item in task.versions if item.version == task.active_version), None
    )
    if version is None:
        return None
    return task, version


def _active_context_version(state: ConversationState):
    active = _active_task_version(state)
    if active is None or active[1].context_question is None:
        return None
    return active


def _structured_filter_surfaces(version, family: str) -> list[str]:
    expression = version.semantics.filter_expression
    if expression is None:
        return []
    raw = expression.model_dump(mode="json")
    result: list[str] = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("node_type") in {"PREDICATE", "ALIASED_PREDICATE"}:
                field = value.get("field_ref") or value.get("field") or {}
                if semantic_filter_family(
                    field.get("canonical_code"), field.get("display_name")
                ) == family:
                    operand = value.get("value") or {}
                    if operand.get("value_type") == "ENTITY_REF":
                        surface = str(
                            (operand.get("ref") or {}).get("display_name") or ""
                        ).strip()
                        if surface:
                            result.append(surface)
                    elif operand.get("value_type") == "STRING":
                        surface = str(operand.get("value") or "").strip()
                        if surface:
                            result.append(surface)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw)
    return list(dict.fromkeys(result))


def _context_filter_surfaces(version, family: str) -> list[str]:
    frame = version.context_question
    if frame is not None:
        return list(dict.fromkeys(
            item.surface for item in frame.filters
            if item.semantic_family == family and item.surface.strip()
        ))
    return _structured_filter_surfaces(version, family)


def _context_metric_surfaces(version) -> list[str]:
    frame = version.context_question
    if frame is not None:
        return list(dict.fromkeys(value.strip() for value in frame.metrics if value.strip()))
    return list(dict.fromkeys(
        item.display_name.strip() for item in version.semantics.metrics
        if item.display_name.strip()
    ))


def _context_dimension_surfaces(version) -> list[str]:
    frame = version.context_question
    if frame is not None:
        return list(dict.fromkeys(
            value.strip() for value in frame.dimensions if value.strip()
        ))
    return list(dict.fromkeys(
        item.display_name.strip() for item in version.semantics.dimensions
        if item.display_name.strip()
    ))


def _context_time_surface(version) -> str | None:
    frame = version.context_question
    if frame is not None:
        return frame.time.surface if frame.time is not None else None
    spec = version.semantics.time_spec
    if spec is None or spec.range is None:
        return None
    timezone = ZoneInfo(spec.timezone)
    start = spec.range.start.astimezone(timezone)
    end = spec.range.end_exclusive.astimezone(timezone)
    if (
        start.month == start.day == 1
        and start.hour == start.minute == start.second == start.microsecond == 0
        and end.month == end.day == 1
        and end.hour == end.minute == end.second == end.microsecond == 0
        and end.year == start.year + 1
    ):
        return f"{start.year}年"
    inclusive = end - timedelta(microseconds=1)
    return f"{start.date().isoformat()}至{inclusive.date().isoformat()}"


def _unique_catalog_surface(session, catalog_type: CatalogType, surface: str) -> str | None:
    normalized = _text(surface).strip("的")
    matched: dict[str, str] = {}
    for candidate in session.candidates(catalog_type):
        metadata = session._rows[candidate["candidate_id"]].metadata
        terms = {
            candidate.get("display_name"),
            candidate.get("canonical_code"),
            *governed_aliases(metadata),
        }
        if normalized not in {_text(value) for value in terms if value}:
            continue
        matched[candidate["candidate_id"]] = candidate["display_name"]
    values = list(dict.fromkeys(matched.values()))
    return values[0] if len(values) == 1 else None


def _unique_catalog_mention(
    session, catalog_type: CatalogType, text: str
) -> str | None:
    """Return one unambiguous longest catalog mention from free text.

    Longer governed names win only when they contain a shorter alias at the
    same span. Independent mentions remain ambiguous and continue through the
    complete recognizer.
    """

    normalized = _text(text)
    occurrences: list[tuple[int, int, str, str]] = []
    for candidate in session.candidates(catalog_type):
        metadata = session._rows[candidate["candidate_id"]].metadata
        terms = {
            candidate.get("display_name"),
            candidate.get("canonical_code"),
            *governed_aliases(metadata),
        }
        for value in terms:
            term = _text(value)
            if not term:
                continue
            start = normalized.find(term)
            while start >= 0:
                occurrences.append((
                    start,
                    start + len(term),
                    candidate["candidate_id"],
                    candidate["display_name"],
                ))
                start = normalized.find(term, start + 1)
    occurrences.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
    selected: list[tuple[int, int, str, str]] = []
    for occurrence in occurrences:
        start, end, _, _ = occurrence
        if any(
            start >= old_start and end <= old_end
            for old_start, old_end, *_ in selected
        ):
            continue
        selected.append(occurrence)
    candidates = {(item[2], item[3]) for item in selected}
    return next(iter(candidates))[1] if len(candidates) == 1 else None


def _context_qualifiers_from_filters(
    filters: list[m.ContextQuestionFilter],
) -> list[str] | None:
    """Return one proven surface per retained filter family.

    A natural-language completion may carry multiple independent families,
    such as region plus product.  Two values in the same family are not safe
    to collapse into an opaque sentence because their Boolean relationship is
    unknown here.
    """

    result: list[str] = []
    for family in ("REGION", "COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"):
        values = list(dict.fromkeys(
            item.surface for item in filters
            if item.semantic_family == family and item.surface.strip()
        ))
        if len(values) > 1:
            return None
        if values:
            result.append(values[0])
    return result


def _context_qualifiers(version) -> list[str] | None:
    return _context_qualifiers_from_filters(_context_filters_from_version(version))


def _dimension_ranking_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    match = _DIMENSION_RANKING_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    surface = (match.group("dimension_a") or match.group("dimension_b")).strip()
    dimension = _unique_catalog_surface(session, CatalogType.DIMENSION, surface)
    metrics = _context_metric_surfaces(version)
    qualifiers = _context_qualifiers(version)
    if dimension is None or len(metrics) != 1 or qualifiers is None:
        return None
    direction_surface = match.group("direction_a") or match.group("direction_b")
    direction = "最低" if direction_surface in {"最低", "最少", "最小"} else "最高"
    time_surface = _context_time_surface(version)
    prefix = "".join([time_surface or "", *qualifiers])
    return _DirectCompletion(
        completed_question=f"查询{prefix}{metrics[0]}{direction}的{dimension}。",
        label=dimension,
        metrics=(metrics[0],),
        dimensions=(dimension,),
        result_entity=dimension,
        replace_result_identity=True,
    )


def _object_ranking_completed_question(
    session, version, question: str, object_surface: str
) -> _DirectCompletion | None:
    match = _OBJECT_RANKING_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    dimension = _unique_catalog_surface(
        session, CatalogType.DIMENSION, match.group("dimension").strip()
    )
    metrics = _context_metric_surfaces(version)
    qualifiers = _context_qualifiers(version)
    if (
        dimension is None
        or len(metrics) != 1
        or qualifiers is None
        or object_surface not in qualifiers
    ):
        return None
    direction = (
        "最低"
        if match.group("direction") in {"最低", "最少"}
        else "最高"
    )
    time_surface = _context_time_surface(version)
    prefix = "".join([time_surface or "", *qualifiers])
    return _DirectCompletion(
        completed_question=f"查询{prefix}的{metrics[0]}{direction}的{dimension}。",
        label=dimension,
        metrics=(metrics[0],),
        dimensions=(dimension,),
        result_entity=dimension,
        replace_result_identity=True,
    )


def _result_count_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    match = _RESULT_COUNT_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    target_surface = match.group("target").strip()
    dimensions = _context_dimension_surfaces(version)
    matched = [item for item in dimensions if _text(item) == _text(target_surface)]
    if len(dimensions) != 1 or len(matched) != 1:
        return None
    target = matched[0]

    # Preserve opaque relationship wording when the previous successful
    # question already exposed the requested object as a list.  Only the
    # grammatical result form changes; V1 still resolves every business term.
    frame = version.context_question
    if frame is not None:
        base = frame.execution_question
        for suffix in ("名单", "列表", "明细"):
            phrase = target_surface + suffix
            if base.count(phrase) == 1:
                return _DirectCompletion(
                    completed_question=base.replace(
                        phrase, target_surface + "数量", 1
                    ),
                    label=target,
                    metrics=(target + "数量",),
                    dimensions=(target,),
                )

    qualifiers = _context_qualifiers(version)
    if qualifiers is None:
        return None
    time_surface = _context_time_surface(version)
    prefix = "".join([time_surface or "", *qualifiers])
    return _DirectCompletion(
        completed_question=f"查询{prefix}{target}数量。",
        label=target,
        metrics=(target + "数量",),
        dimensions=(target,),
    )


def _relation_target_completed_question(
    version, question: str
) -> _DirectCompletion | None:
    """Complete a subject-less relationship clause from one proven actor.

    The relationship wording and target come only from the current user turn.
    We reuse a prior value solely when exactly one non-geographic filter can be
    the omitted grammatical subject. V1 remains responsible for interpreting
    and executing the resulting complete sentence.
    """

    match = _RELATION_TARGET_ELLIPSIS.fullmatch(question)
    if match is None:
        return None
    filters = _context_filters_from_version(version)
    actors = list(dict.fromkeys(
        item.surface for item in filters
        if item.semantic_family in {"COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"}
        and item.surface.strip()
    ))
    if len(actors) != 1:
        return None
    regions = list(dict.fromkeys(
        item.surface for item in filters
        if item.semantic_family == "REGION" and item.surface.strip()
    ))
    if len(regions) > 1:
        return None
    target = next(
        value for value in (
            match.group("target_a"),
            match.group("target_b"),
            match.group("target_c"),
        )
        if value is not None
    )
    body = match.group("body").strip()
    prefix = "".join([
        _context_time_surface(version) or "",
        *regions,
        actors[0],
    ])
    return _DirectCompletion(
        completed_question=f"查询{prefix}{body}。",
        label=target,
        metrics=(),
        dimensions=(target,),
        result_entity=target,
        replace_result_identity=True,
    )


def _relation_result_target(question: str) -> str | None:
    """Return one explicit result-object class from a relationship list turn."""

    matches = [
        (match.group("after") or match.group("before"))
        for match in _RELATION_RESULT_TARGET.finditer(question)
    ]
    unique = list(dict.fromkeys(item for item in matches if item))
    return unique[0] if len(unique) == 1 else None


def _recent_pair_total_completed_question(
    task: TaskState, version: TaskVersion, question: str
) -> _DirectCompletion | None:
    """Resolve “these two … total” from two values in the current task only."""

    match = _RECENT_PAIR_TOTAL.fullmatch(question)
    if match is None:
        return None
    family = {
        "省份": "REGION",
        "省": "REGION",
        "地区": "REGION",
        "区域": "REGION",
        "产品": "COMMERCIAL_PRODUCT",
        "商品": "COMMERCIAL_PRODUCT",
        "医院": "HOSPITAL",
        "经销商": "PARTNER",
        "供应商": "PARTNER",
    }[match.group("family")]
    recent: list[m.ContextQuestionFilter] = []
    seen: set[str] = set()
    for candidate_version in sorted(
        task.versions, key=lambda item: item.version, reverse=True
    ):
        values = [
            item for item in _context_filters_from_version(candidate_version)
            if item.semantic_family == family and item.surface.strip()
        ]
        unique = {
            (item.surface, item.canonical_value): item for item in values
        }
        if len(unique) != 1:
            continue
        item = next(iter(unique.values()))
        identity = item.canonical_value or item.surface
        if identity in seen:
            continue
        seen.add(identity)
        recent.append(item)
        if len(recent) == 2:
            break
    if len(recent) != 2:
        return None
    recent.reverse()

    metrics = _context_metric_surfaces(version)
    if len(metrics) != 1:
        return None
    retained = [
        item for item in _context_filters_from_version(version)
        if item.semantic_family != family
    ]
    filters = [*retained, *recent]
    qualifiers: list[str] = []
    for current_family in (
        "REGION", "COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"
    ):
        values = [
            item.surface for item in filters
            if item.semantic_family == current_family and item.surface.strip()
        ]
        values = list(dict.fromkeys(values))
        if current_family == family:
            if len(values) != 2:
                return None
            qualifiers.append("和".join(values))
        elif len(values) > 1:
            return None
        elif values:
            qualifiers.append(values[0])
    prefix = "".join([_context_time_surface(version) or "", *qualifiers])
    if not prefix:
        return None
    return _DirectCompletion(
        completed_question=f"查询{prefix}的{metrics[0]}合计。",
        label="和".join(item.surface for item in recent),
        metrics=(metrics[0],),
        dimensions=(),
        filters=tuple(filters),
    )


def _recent_task_pair_comparison_completed_question(
    state: ConversationState,
    session: ScopedPlanSession,
    question: str,
) -> _DirectCompletion | None:
    """Compare two explicit values from the two most recent task topics.

    “这两个产品” differs from “这两个省加起来”: its antecedents can be
    the subjects of two independent NEW_TASK turns. Resolve it only from two
    different tasks in this conversation, and only when both tasks expose one
    value of the requested family plus identical remaining filters and time.
    The current turn supplies the metric and comparison direction explicitly.
    """

    match = _RECENT_TASK_PAIR_COMPARISON.fullmatch(question)
    if match is None:
        return None
    family = {
        "产品": "COMMERCIAL_PRODUCT",
        "商品": "COMMERCIAL_PRODUCT",
        "医院": "HOSPITAL",
        "经销商": "PARTNER",
        "供应商": "PARTNER",
    }[match.group("family")]
    metric = _unique_catalog_surface(
        session, CatalogType.METRIC, match.group("metric").strip()
    )
    if metric is None:
        return None

    # topic_stack is the persisted navigation order. Walking it backwards is
    # stable even when fast requests or tests share the same timestamp.
    candidates: list[
        tuple[
            m.ContextQuestionFilter,
            list[m.ContextQuestionFilter],
            m.ContextQuestionTime | None,
        ]
    ] = []
    seen_tasks: set[str] = set()
    seen_values: set[str] = set()
    for topic_id in reversed(state.topic_stack):
        topic = state.topics.get(topic_id)
        if topic is None or topic.active_task_id is None:
            continue
        candidate_task = state.tasks.get(topic.active_task_id)
        if candidate_task is None or candidate_task.task_id in seen_tasks:
            continue
        seen_tasks.add(candidate_task.task_id)
        candidate_version = next(
            (
                item for item in candidate_task.versions
                if item.version == candidate_task.active_version
            ),
            None,
        )
        if candidate_version is None:
            continue
        filters = _context_filters_from_version(candidate_version)
        compared = [
            item for item in filters
            if item.semantic_family == family and item.surface.strip()
        ]
        unique = {
            (item.surface, item.canonical_value): item for item in compared
        }
        if len(unique) != 1:
            continue
        value = next(iter(unique.values()))
        identity = value.canonical_value or value.surface
        if identity in seen_values:
            continue
        seen_values.add(identity)
        candidates.append((
            value,
            [item for item in filters if item.semantic_family != family],
            _context_time_from_version(candidate_version),
        ))
        if len(candidates) == 2:
            break
    if len(candidates) != 2:
        return None

    newest, previous = candidates

    def filter_signature(values: list[m.ContextQuestionFilter]):
        return sorted(
            (
                item.semantic_family,
                item.attribute_code or "",
                item.canonical_value or item.surface,
            )
            for item in values
        )

    # Do not silently compare objects under different regions, hospitals, or
    # partner restrictions. The user must state how those scopes relate.
    if filter_signature(newest[1]) != filter_signature(previous[1]):
        return None
    newest_time = newest[2]
    previous_time = previous[2]

    def time_signature(value):
        return (
            None
            if value is None
            else (value.start, value.end_exclusive)
        )

    if time_signature(newest_time) != time_signature(previous_time):
        return None

    compared = [previous[0], newest[0]]
    retained = previous[1]
    qualifiers = _context_qualifiers_from_filters(retained)
    if qualifiers is None:
        return None
    prefix = "".join([
        newest_time.surface if newest_time is not None else "",
        *qualifiers,
    ])
    values = "和".join(item.surface for item in compared)
    direction = match.group("direction")
    return _DirectCompletion(
        completed_question=(
            f"比较{prefix}{values}的{metric}，判断哪个{direction}。"
        ),
        label=values,
        metrics=(metric,),
        dimensions=(match.group("family"),),
        filters=tuple([*retained, *compared]),
    )


def _sort_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    match = _SORT_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    metric = _unique_catalog_surface(
        session, CatalogType.METRIC, match.group("metric").strip()
    )
    if metric is None:
        return None
    direction = (
        "从低到高"
        if match.group("direction") in {"从低到高", "升序"}
        else "从高到低"
    )
    frame = version.context_question
    if frame is not None:
        base = frame.execution_question.rstrip("。.!！?？")
        base = re.sub(
            r"[，,]\s*按.+?(?:从高到低|降序|从低到高|升序)排序$",
            "",
            base,
        )
        return _DirectCompletion(
            completed_question=f"{base}，按{metric}{direction}排序。",
            label=metric,
            metrics=(metric,),
        )

    dimensions = _context_dimension_surfaces(version)
    qualifiers = _context_qualifiers(version)
    if not dimensions or qualifiers is None:
        return None
    time_surface = _context_time_surface(version)
    prefix = "".join([time_surface or "", *qualifiers])
    return _DirectCompletion(
        completed_question=(
            f"查询{prefix}{'、'.join(dimensions)}，按{metric}{direction}排序。"
        ),
        label=metric,
        metrics=(metric,),
    )


def _ranked_item_metric_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    match = _RANKED_ITEM_METRIC_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    metric = _unique_catalog_surface(
        session, CatalogType.METRIC, match.group("metric").strip()
    )
    dimensions = _context_dimension_surfaces(version)
    if metric is None or len(dimensions) != 1:
        return None
    target = dimensions[0]
    replacement = f"{metric}排名第一的{target}及其{metric}"
    frame = version.context_question
    if frame is not None:
        base = frame.execution_question.rstrip("。.!！?？")
        base = re.sub(
            r"[，,]\s*按.+?(?:从高到低|降序|从低到高|升序)排序$",
            "",
            base,
        )
        for suffix in ("名单", "列表", "明细", "数量"):
            phrase = target + suffix
            if base.count(phrase) == 1:
                return _DirectCompletion(
                    completed_question=base.replace(phrase, replacement, 1) + "。",
                    label=metric,
                    metrics=(metric,),
                    dimensions=(target,),
                )

    qualifiers = _context_qualifiers(version)
    if qualifiers is None:
        return None
    time_surface = _context_time_surface(version)
    prefix = "".join([time_surface or "", *qualifiers])
    return _DirectCompletion(
        completed_question=f"查询{prefix}{replacement}。",
        label=metric,
        metrics=(metric,),
        dimensions=(target,),
    )


def _metric_only_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    """Replace the metric when the current turn contains only one known metric.

    A phrase such as ``订单笔数是多少`` is context dependent when the same
    conversation already has an active task.  The model has occasionally
    classified this identical surface as NEW_TASK and dropped proven filters.
    We therefore resolve only the whole metric surface against the current V1
    catalog and retain every other confirmed slot.  A surface containing an
    entity, region, time or other wording cannot match a metric exactly and
    continues through the full recognizer.
    """

    match = _METRIC_ONLY_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    metric = _unique_catalog_surface(
        session, CatalogType.METRIC, match.group("metric").strip()
    )
    if metric is None:
        return None

    previous_metrics = _context_metric_surfaces(version)
    frame = version.context_question
    if frame is not None and len(previous_metrics) == 1:
        base = frame.execution_question.rstrip("。.!！?？")
        previous_metric = previous_metrics[0]
        if base.count(previous_metric) == 1:
            return _DirectCompletion(
                completed_question=base.replace(previous_metric, metric, 1) + "。",
                label=metric,
                metrics=(metric,),
                reset_detail_shape=True,
            )

    filters = _context_filters_from_version(version)
    qualifiers = _context_qualifiers_from_filters(filters)
    if qualifiers is None:
        return None
    time_surface = _context_time_surface(version)
    prefix = "".join([time_surface or "", *qualifiers])
    connector = (
        "的"
        if any(item.semantic_family != "REGION" for item in filters)
        else ""
    )
    dimensions = _context_dimension_surfaces(version)
    completed = f"查询{prefix}{connector}{metric}"
    if dimensions:
        completed += f"，按{'、'.join(dimensions)}分组"
    return _DirectCompletion(
        completed_question=completed + "。",
        label=metric,
        metrics=(metric,),
        dimensions=tuple(dimensions),
        reset_detail_shape=True,
    )


def _top_group_drilldown_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    """Carry a ranked group as an opaque scope into a detail question."""

    match = _TOP_GROUP_DRILLDOWN.fullmatch(question)
    if match is None:
        return None
    requested_dimension = _unique_catalog_surface(
        session, CatalogType.DIMENSION, match.group("dimension")
    )
    dimensions = _context_dimension_surfaces(version)
    metrics = _context_metric_surfaces(version)
    filters = _context_filters_from_version(version)
    qualifiers = _context_qualifiers_from_filters(filters)
    if (
        requested_dimension is None
        or requested_dimension not in dimensions
        or len(metrics) != 1
        or qualifiers is None
    ):
        return None
    prefix = "".join([_context_time_surface(version) or "", *qualifiers])
    connector = (
        "的"
        if any(item.semantic_family != "REGION" for item in filters)
        else ""
    )
    target = match.group("target")
    body = match.group("body").strip()
    return _DirectCompletion(
        completed_question=(
            f"查询{prefix}{connector}{metrics[0]}排名第一的"
            f"{requested_dimension}中{body}。"
        ),
        label=target,
        metrics=(metrics[0],),
        dimensions=(target,),
        result_entity=target,
        replace_result_identity=True,
    )


def _result_set_extreme_completed_question(
    session, version, question: str
) -> _DirectCompletion | None:
    """Apply a current extreme metric to the immediately prior result set."""

    match = _RESULT_SET_EXTREME.fullmatch(question)
    frame = version.context_question
    if match is None or frame is None:
        return None
    target = match.group("target")
    structured_result_matches = (
        frame.entity == target and target in frame.dimensions
    )
    # Compatibility for context frames published before result-object identity
    # was stored explicitly.  The prior completed question itself is V2-owned
    # semantic evidence and may prove the same single result class.
    textual_result_matches = (
        _relation_result_target(frame.execution_question) == target
    )
    if not structured_result_matches and not textual_result_matches:
        return None
    explicit_metric = match.group("metric").strip().strip("的").strip()
    if not explicit_metric:
        return None
    # The result-set identity comes from the immediately prior V2 context, but
    # the metric is explicit current-turn language.  Prefer the governed name
    # when the current Catalog has one unique match; otherwise preserve the
    # user's wording for the original V1 semantic/execution chain.  Rejecting
    # here would incorrectly turn an unknown metric alias into antecedent
    # ambiguity (for example, ``采购金额`` over a known hospital result set).
    metric = (
        _unique_catalog_mention(session, CatalogType.METRIC, explicit_metric)
        or explicit_metric
    )
    base = frame.execution_question.rstrip("。.!！?？")
    result_phrase = re.compile(
        rf"(?:有)?哪些{re.escape(target)}(?:在)?(?:采购|购买|销售|合作|供货)?"
    )
    if result_phrase.search(base) is None:
        return None
    direction = (
        "最低"
        if match.group("direction") in {"最低", "最少", "最小"}
        else "最高"
    )
    completed = result_phrase.sub(f"{metric}{direction}的{target}", base, count=1)
    return _DirectCompletion(
        completed_question=completed + "。",
        label=target,
        metrics=(metric,),
        dimensions=(target,),
        result_entity=target,
        replace_result_identity=True,
    )


def _opaque_unique_result_followup(
    version, question: str, markers: tuple[str, ...]
) -> _DirectCompletion | None:
    """Refer to one prior superlative result without inventing its value."""

    frame = version.context_question
    if (
        frame is None
        or frame.entity is None
        or frame.entity not in frame.dimensions
    ):
        return None
    # A superlative elsewhere in the sentence does not make the current result
    # entity unique.  For example, ``排名第一的省份中有哪些医院`` is a hospital
    # result set; ``排名第一`` identifies the province, not one hospital.  Only
    # accept a singular pronoun when the immediately prior completion directly
    # selected an extreme instance of the stored result entity.
    if re.search(
        rf"(?:最高|最低|排名第一)的\s*{re.escape(frame.entity)}",
        frame.execution_question,
    ) is None:
        return None
    marker = next(
        (
            value for value in sorted(markers, key=len, reverse=True)
            if question.lstrip().startswith(value)
        ),
        None,
    )
    if marker is None:
        return None
    tail = question.lstrip()[len(marker):].strip().lstrip("，,")
    if not tail:
        return None
    base = frame.execution_question.rstrip("。.!！?？")
    return _DirectCompletion(
        completed_question=base + tail.rstrip("。.!！?？") + "。",
        label=frame.entity,
        result_entity=frame.entity,
        replace_result_identity=True,
    )


def _context_filters_from_version(version) -> list[m.ContextQuestionFilter]:
    if version.context_question is not None:
        return list(version.context_question.filters)
    result: list[m.ContextQuestionFilter] = []
    for family in ("REGION", "COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"):
        for surface in _structured_filter_surfaces(version, family):
            result.append(m.ContextQuestionFilter(
                surface=surface,
                canonical_value=surface,
                semantic_family=family,
                evidence_source="V2_TASK_STATE",
            ))
    return result


def _context_time_from_version(version) -> m.ContextQuestionTime | None:
    if version.context_question is not None:
        return version.context_question.time
    spec = version.semantics.time_spec
    surface = _context_time_surface(version)
    if spec is None or spec.range is None or surface is None:
        return None
    timezone = ZoneInfo(spec.timezone)
    return m.ContextQuestionTime(
        surface=surface,
        start=spec.range.start.astimezone(timezone).date(),
        end_exclusive=spec.range.end_exclusive.astimezone(timezone).date(),
        evidence_source="V2_TASK_STATE",
    )


def build_result_availability_question(
    state_artifact: ScopedArtifact | None,
    *,
    failed_capability: str | None,
    prior_state_artifact: ScopedArtifact | None = None,
    completed_question: str | None = None,
) -> str | None:
    """Build one simpler read-only question from current V2 meaning.

    This is used only by the explicitly enabled demo runtime after V1/Oagent
    rejects a grouping or time capability. It never reuses a Dataset or a
    previous response. The retry still enters the original V1 authorization,
    planning, Oagent, SQL and DB path with the same request contract.
    """

    if failed_capability not in {
        "ASL_GROUPING_DIMENSION_MISSING",
        "ASL_DIMENSION_INVALID",
        "ASL_ENTITY_MENTION_UNRESOLVED",
        "ASL_TIME_ANCHOR_MISSING",
        "DEPENDENCY_CONTRACT_REJECTED",
        "DEPENDENCY_UNAVAILABLE",
    }:
        return None
    if completed_question and failed_capability in {
        "ASL_ENTITY_MENTION_UNRESOLVED",
        "DEPENDENCY_CONTRACT_REJECTED",
    }:
        # V1's entity resolver can read a universal scope such as ``全部产品``
        # as though it were a concrete business name and demand an exact entity
        # match.  For the single demo-only read retry, remove only an explicit
        # universal entity-type scope at the start of a self-contained aggregate
        # question.  The displayed completed question remains unchanged and a
        # concrete entity value can never enter this branch.
        universal = re.fullmatch(
            r"\s*(查询|统计|计算|汇总)\s*(?:全部|所有)\s*"
            r"(?:产品|商品|医院|经销商|客户|厂家|厂商|品牌|省份|地区|城市)\s*的\s*"
            r"(.+?)\s*[。.!！?？]?\s*",
            completed_question,
        )
        if universal is not None and re.search(
            r"(?:销售额|金额|数量|笔数|总额|总量|均值|平均|占比|比例|次数|家数|个数)",
            universal.group(2),
        ):
            return f"{universal.group(1)}{universal.group(2).rstrip('。.!！?？')}。"
        generic_type_retry = None
        for generic_type in re.finditer(
            r"(?:产品|商品)(?=(?:的|在|最近|近|合作|销售|采购|含税|订单|数量|金额|趋势|排名))",
            completed_question,
        ):
            prefix = completed_question[:generic_type.start()].rstrip()
            if prefix.endswith(("全部", "所有", "哪些", "什么")):
                continue
            # A trailing entity-type label is sometimes absorbed into a brand
            # or product name by V1's relation/trend parser. Removing only that
            # label preserves the named value and all query operations.
            generic_type_retry = (
                completed_question[:generic_type.start()]
                + completed_question[generic_type.end():]
            )
            break
    else:
        generic_type_retry = None
    if state_artifact is None:
        return generic_type_retry
    state = ConversationState.model_validate(state_artifact.payload)
    active = _active_task_version(state)
    if active is None:
        return generic_type_retry
    _, version = active
    metrics = _context_metric_surfaces(version)
    if prior_state_artifact is not None:
        prior_state = ConversationState.model_validate(prior_state_artifact.payload)
        prior_active = _active_task_version(prior_state)
        prior_metrics = (
            _context_metric_surfaces(prior_active[1])
            if prior_active is not None
            else []
        )
        if len(metrics) == 1 and len(prior_metrics) == 1 and metrics != prior_metrics:
            # The current user wording remains authoritative in the displayed
            # completed question.  For a demo-only availability retry, prefer
            # the previous governed metric when the newly explicit phrase was
            # exactly what the downstream semantic service could not bind.
            metrics = prior_metrics
    filters = _context_filters_from_version(version)
    qualifiers: list[str] | None = []
    for family in ("REGION", "COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"):
        values = list(dict.fromkeys(
            item.surface for item in filters
            if item.semantic_family == family and item.surface.strip()
        ))
        if len(values) > 2:
            qualifiers = None
            break
        if values:
            qualifiers.append("和".join(values))
    if len(metrics) == 1 and qualifiers is not None:
        time_surface = (
            None
            if failed_capability == "ASL_TIME_ANCHOR_MISSING"
            else _context_time_surface(version)
        )
        prefix = "".join([time_surface or "", *qualifiers])
        connector = (
            "的"
            if any(item.semantic_family != "REGION" for item in filters)
            else ""
        )
        return f"查询{prefix}{connector}{metrics[0]}。"

    frame = version.context_question
    if (
        failed_capability == "ASL_TIME_ANCHOR_MISSING"
        and frame is not None
        and frame.time is not None
        and frame.execution_question.count(frame.time.surface) == 1
    ):
        without_time = frame.execution_question.replace(frame.time.surface, "", 1)
        return re.sub(r"\s+", "", without_time)
    return generic_type_retry


def _publish_context_continuation(
    *,
    state: ConversationState,
    task: TaskState,
    version: TaskVersion,
    chat: ChatRequest,
    session: ScopedPlanSession,
    completion: _DirectCompletion,
    now: datetime,
) -> ScopedArtifact:
    """Publish a reliable language completion as the current task version.

    Query execution success is deliberately not a prerequisite.  This is V2
    conversation meaning only: no Dataset, ASL or V1 execution field is copied.
    Once a completion cannot be represented by the structured V2 payload, the
    context frame becomes authoritative for subsequent language completion.
    """

    previous = version.context_question
    frame = m.ContextQuestionState(
        provenance="V2_CONTEXT_RESOLUTION",
        original_question=chat.question,
        execution_question=completion.completed_question,
        primary_intent=(
            "METRIC_QUERY"
            if completion.reset_detail_shape
            else (
                "DETAIL_QUERY"
                if completion.replace_result_identity
                else (previous.primary_intent if previous is not None else None)
            )
        ),
        entity=(
            None
            if completion.reset_detail_shape
            else (
                completion.result_entity
                if completion.replace_result_identity
                else (
                    previous.entity
                    if previous is not None
                    else (
                        version.semantics.subject.display_name
                        if version.semantics.subject is not None
                        else None
                    )
                )
            )
        ),
        metrics=list(
            completion.metrics
            if completion.metrics is not None
            else _context_metric_surfaces(version)
        ),
        dimensions=list(
            completion.dimensions
            if completion.dimensions is not None
            else _context_dimension_surfaces(version)
        ),
        fields=(
            []
            if completion.reset_detail_shape or completion.replace_result_identity
            else (list(previous.fields) if previous is not None else [])
        ),
        filters=(
            list(completion.filters)
            if completion.filters is not None
            else _context_filters_from_version(version)
        ),
        time=_context_time_from_version(version),
        source_message_id=chat.message_id,
        semantic_catalog_version=session.context.catalog_pin.catalog_version,
        last_edit=(
            m.ContextQuestionEdit(
                operation=completion.operation,
                slot="filter_expression",
                source_message_id=chat.message_id,
                evidence_surface=completion.evidence_surface,
            )
            if completion.operation is not None
            and completion.evidence_surface is not None
            else None
        ),
    )
    data = state.model_dump(mode="python")
    changed = data["tasks"][task.task_id]
    for item in changed["versions"]:
        if item["version"] == task.active_version:
            item["status"] = "SUPERSEDED"
    next_version = task.active_version + 1
    changed["versions"].append(TaskVersion(
        version=next_version,
        status="PROVISIONAL",
        # The natural-language frame is the semantic authority for this
        # partial version. Keeping the old structured payload here would make a
        # later model turn silently inherit stale metric/dimension values.
        semantics=m.TaskSemanticState(),
        context_question=frame,
        current_turn_ref=chat.message_id,
        current_turn_digest=contract_digest(chat.question),
        created_at=now,
    ).model_dump(mode="python"))
    changed["active_version"] = next_version
    changed["status"] = "PROVISIONAL"
    topic = data["topics"].get(task.topic_id)
    if topic is not None:
        topic["last_accessed_at"] = now
    data["recent_turn_ids"] = [*data["recent_turn_ids"], chat.message_id][-100:]
    data["state_version"] += 1
    next_state = ConversationState.model_validate(data)
    session.accept_catalog()
    return session.seal(kind="CONVERSATION", payload=next_state)


def _insert_time_surface(completed: str, time_surface: str) -> str:
    if completed.startswith("查询"):
        return "查询" + time_surface + completed[2:]
    grain_prefix = re.match(
        r"^(按(?:年|年度|季度|季|月|月份|周|星期|日|天)"
        r"(?:看|查看|查询|统计|汇总|分析|展示|显示)?)",
        completed,
    )
    if grain_prefix is not None:
        offset = grain_prefix.end()
        return completed[:offset] + time_surface + completed[offset:]
    return time_surface + completed


def _grain_completed_question(session, version, question: str) -> _DirectCompletion | None:
    """Complete a grain-only edit from current catalog terms and one active task."""

    match = _TIME_GRAIN_FOLLOWUP.fullmatch(question)
    if match is None:
        return None
    body = match.group("body").strip().strip("的")
    if body:
        metric = _unique_catalog_surface(session, CatalogType.METRIC, body)
        if metric is None:
            return None
    else:
        metrics = _context_metric_surfaces(version)
        if len(metrics) != 1:
            return None
        metric = metrics[0]

    qualifiers = _context_qualifiers(version)
    if not qualifiers:
        return None
    time_surface = _context_time_surface(version)
    if time_surface and not _TIME_RANGE_SURFACE.search(question):
        qualifiers.insert(0, time_surface)
    grain = {
        "年度": "年",
        "季度": "季",
        "月份": "月",
        "星期": "周",
        "天": "日",
    }.get(match.group("grain"), match.group("grain"))
    qualifier = "".join(qualifiers)
    completed = f"按{grain}统计{qualifier}的{metric}。"
    dimensions = [
        value for value in _context_dimension_surfaces(version)
        if value not in {"时间", "日期", "年", "季度", "季", "月", "月份", "周", "星期", "日", "天"}
    ]
    dimensions.append(grain)
    return _DirectCompletion(
        completed_question=completed,
        label=grain,
        metrics=(metric,),
        dimensions=tuple(dict.fromkeys(dimensions)),
    )


def has_active_context_question(state_artifact: ScopedArtifact | None) -> bool:
    if state_artifact is None:
        return False
    return _active_context_version(
        ConversationState.model_validate(state_artifact.payload)
    ) is not None


def is_contextual_ellipsis(question: str) -> bool:
    match = _ELLIPTICAL_VALUE.fullmatch(question)
    if match is None:
        return False
    value = match.group("value").strip()
    return bool(
        value
        and len(value) <= 200
        and value not in {"这", "这个", "那", "那个", "它", "这些", "那些"}
        and _QUERY_VERBS.search(value) is None
    )


def _contextual_filter_surface(question: str) -> str | None:
    explicit = _EXPLICIT_FILTER_REPLACEMENT.fullmatch(question)
    if explicit is not None:
        surface = explicit.group("value").strip().removesuffix("的").strip()
        return surface or None
    restriction = _FILTER_RESTRICTION.fullmatch(question)
    if restriction is not None:
        surface = restriction.group("value").strip().removesuffix("的").strip()
        return surface or None
    elliptical = _ELLIPTICAL_VALUE.fullmatch(question)
    if elliptical is None or not is_contextual_ellipsis(question):
        return None
    surface = elliptical.group("value").strip().removesuffix("的").strip()
    return surface or None


def _contextual_clear_family(question: str) -> str | None:
    match = _FILTER_CLEAR.fullmatch(question)
    if match is None:
        return None
    if match.group("nationwide") is not None:
        return "REGION"
    surface = match.group("unlimited") or match.group("all_values")
    if surface in {"地区", "区域", "省份", "城市"}:
        return "REGION"
    if surface in {"产品", "商品"}:
        return "COMMERCIAL_PRODUCT"
    if surface == "医院":
        return "HOSPITAL"
    if surface in {"经销商", "供应商"}:
        return "PARTNER"
    return None


def is_contextual_short_edit(question: str) -> bool:
    return (
        _TIME_ONLY.fullmatch(question) is not None
        or _contextual_filter_surface(question) is not None
        or _contextual_clear_family(question) is not None
    )


def is_self_contained_execution_question(question: str) -> bool:
    """Conservatively recognize a complete current-turn V1 request.

    This is a bridge safety classification, not semantic parsing.  It is used
    only when the V2 recognizer itself cannot produce a result.  Explicit
    commands and relation questions with their own antecedent may continue to
    V1; pronouns, discourse edits, bare replacements and short ellipses may not.
    """

    value = question.strip()
    if not value or _CONTEXT_DEPENDENT_SURFACE.search(value):
        return False
    if any(marker in value for marker in _GENERIC_OBJECT_REFERENCES):
        return False
    if is_contextual_short_edit(value) or is_contextual_ellipsis(value):
        return False
    return (
        _EXPLICIT_QUERY_VERBS.search(value) is not None
        or _SELF_CONTAINED_RELATION_QUERY.search(value) is not None
    )


def _referenced_families(question: str) -> dict[str, tuple[str, ...]]:
    return {
        family: tuple(surface for surface in surfaces if surface in question)
        for family, surfaces in _CONTEXT_REFERENCE_SURFACES.items()
        if any(surface in question for surface in surfaces)
    }


def resolve_context_references(
    *,
    chat: ChatRequest,
    identity: TrustedIdentity,
    state_artifact: ScopedArtifact | None,
    catalog,
    resolved_business_domain_ids,
    now: datetime,
) -> ContextReferenceResolution | None:
    """Complete explicit references or a grain-only edit from one active task.

    This path changes only user-visible language.  It does not create an
    executable V2 predicate or reuse V1 scope.  The completed question is sent
    through the original V1 chain, which resolves all current semantics again.
    """
    references = _referenced_families(chat.question)
    generic_markers = tuple(
        marker for marker in _GENERIC_OBJECT_REFERENCES if marker in chat.question
    )
    if generic_markers and _has_current_antecedent_before_reference(
        chat.question, generic_markers
    ):
        # A noun phrase before the pronoun is current-turn evidence.  The
        # complete V2 recognizer must decide its type and edit operation;
        # replacing the pronoun from history here would violate "current
        # explicit value wins" and can revive the previous object.
        return None
    grain_candidate = _TIME_GRAIN_FOLLOWUP.fullmatch(chat.question)
    ranking_candidate = _DIMENSION_RANKING_FOLLOWUP.fullmatch(chat.question)
    count_candidate = _RESULT_COUNT_FOLLOWUP.fullmatch(chat.question)
    sort_candidate = _SORT_FOLLOWUP.fullmatch(chat.question)
    ranked_item_candidate = _RANKED_ITEM_METRIC_FOLLOWUP.fullmatch(chat.question)
    metric_only_candidate = _METRIC_ONLY_FOLLOWUP.fullmatch(chat.question)
    relation_target_candidate = _RELATION_TARGET_ELLIPSIS.fullmatch(chat.question)
    recent_pair_candidate = _RECENT_PAIR_TOTAL.fullmatch(chat.question)
    recent_task_pair_candidate = _RECENT_TASK_PAIR_COMPARISON.fullmatch(
        chat.question
    )
    top_group_candidate = _TOP_GROUP_DRILLDOWN.fullmatch(chat.question)
    result_set_extreme_candidate = _RESULT_SET_EXTREME.fullmatch(chat.question)
    if (
        not references
        and not generic_markers
        and grain_candidate is None
        and ranking_candidate is None
        and count_candidate is None
        and sort_candidate is None
        and ranked_item_candidate is None
        and metric_only_candidate is None
        and relation_target_candidate is None
        and recent_pair_candidate is None
        and recent_task_pair_candidate is None
        and top_group_candidate is None
        and result_set_extreme_candidate is None
    ) or state_artifact is None:
        return None
    session = ScopedPlanSession(
        chat,
        identity,
        catalog,
        resolved_business_domain_ids=resolved_business_domain_ids,
    )
    state = ConversationState.model_validate(
        session.restore(
            state_artifact, kind="CONVERSATION", defer_source_values=True
        )
    )
    active = _active_task_version(state)
    if active is None:
        return None
    if any(record.status == "ACTIVE" for record in state.pending_records.values()):
        return None
    task, version = active

    direct_resolution = None
    direct_understanding = None
    # This grammar contains an explicit result class (``这些医院``) and a
    # current-turn metric.  Resolve it before the generic-pronoun path, which
    # otherwise tries to infer one filter value and reports a false ambiguity.
    if result_set_extreme_candidate is not None:
        direct_resolution = _result_set_extreme_completed_question(
            session, version, chat.question
        )
        if direct_resolution is not None:
            direct_understanding = (
                "沿用上一轮形成的结果集合，"
                f"按本轮明确指定的指标查询其中排名第一的{direct_resolution.label}。"
            )
    if not references and not generic_markers:
        if grain_candidate is not None:
            direct_resolution = _grain_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的查询范围和指标，"
                    f"将查看粒度调整为按{direct_resolution.label}。"
                )
        elif ranking_candidate is not None:
            direct_resolution = _dimension_ranking_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的指标和筛选条件，"
                    f"查询{direct_resolution.label}中的极值对象。"
                )
        elif count_candidate is not None:
            direct_resolution = _result_count_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的查询对象和筛选条件，"
                    f"将{direct_resolution.label}名单改为数量统计。"
                )
        elif sort_candidate is not None:
            direct_resolution = _sort_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的查询对象和条件，"
                    f"按本轮明确指定的{direct_resolution.label}排序。"
                )
        elif ranked_item_candidate is not None:
            direct_resolution = _ranked_item_metric_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的查询对象和条件，"
                    f"查询排名第一对象的{direct_resolution.label}。"
                )
        elif relation_target_candidate is not None:
            direct_resolution = _relation_target_completed_question(
                version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮唯一可确认的查询对象，"
                    f"按本轮要求查询关联的{direct_resolution.label}。"
                )
        elif recent_pair_candidate is not None:
            direct_resolution = _recent_pair_total_completed_question(
                task, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用当前任务最近两次明确指定的同类条件和其他查询语义，"
                    f"计算{direct_resolution.label}的合计。"
                )
        elif recent_task_pair_candidate is not None:
            direct_resolution = _recent_task_pair_comparison_completed_question(
                state, session, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "引用同一会话最近两个独立任务中已确认的同类对象，"
                    f"按本轮明确指定的指标比较{direct_resolution.label}。"
                )
        elif top_group_candidate is not None:
            direct_resolution = _top_group_drilldown_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的指标、筛选条件和分组，"
                    f"在排名第一的分组中继续查询{direct_resolution.label}。"
                )
        elif result_set_extreme_candidate is not None:
            # Already attempted above because this grammar normally includes a
            # generic reference marker such as ``这些``.
            pass
        elif metric_only_candidate is not None:
            direct_resolution = _metric_only_completed_question(
                session, version, chat.question
            )
            if direct_resolution is not None:
                direct_understanding = (
                    "沿用上一轮已确认的查询范围，仅将指标替换为"
                    f"{direct_resolution.label}。"
                )
    if direct_resolution is not None:
        return ContextReferenceResolution(
            completed_question=direct_resolution.completed_question,
            next_state=_publish_context_continuation(
                state=state,
                task=task,
                version=version,
                chat=chat,
                session=session,
                completion=direct_resolution,
                now=now,
            ),
            understanding=direct_understanding,
            referenced_families=tuple(
                family for family in _CONTEXT_REFERENCE_SURFACES
                if _context_filter_surfaces(version, family)
            ),
        )
    if result_set_extreme_candidate is not None:
        # The user named the referenced result class explicitly, so failure to
        # prove that class from the immediately prior result must not fall
        # through to generic pronoun replacement (which could produce text such
        # as ``测试产品医院`` from an unrelated product filter).
        return ContextReferenceResolution(
            completed_question=None,
            next_state=None,
            clarification_question=(
                f"上一任务中没有唯一可确认的"
                f"{result_set_extreme_candidate.group('target')}结果集合，"
                "请补充完整查询条件。"
            ),
        )
    if metric_only_candidate is not None and all(candidate is None for candidate in (
        grain_candidate,
        ranking_candidate,
        count_candidate,
        sort_candidate,
        ranked_item_candidate,
        relation_target_candidate,
        recent_pair_candidate,
        recent_task_pair_candidate,
        top_group_candidate,
        result_set_extreme_candidate,
    )):
        # The ``X呢`` grammar deliberately overlaps terse entity replacement.
        # Only an exact current-catalog metric match belongs to this shortcut;
        # otherwise preserve the established filter/model path.
        return None
    if not references and not generic_markers:
        return ContextReferenceResolution(
            completed_question=None,
            next_state=None,
            clarification_question=(
                "当前追问无法从上一任务中唯一确定要沿用的指标、维度或筛选条件，"
                "请补充完整问题。"
            ),
        )

    replacements: dict[str, str] = {}
    for family, markers in references.items():
        candidates = _context_filter_surfaces(version, family)
        if len(candidates) != 1:
            label = _CONTEXT_FAMILY_LABELS[family]
            return ContextReferenceResolution(
                completed_question=None,
                next_state=None,
                clarification_question=(
                    f"上一任务中的{label}条件无法唯一确定，请补充完整的{label}。"
                ),
                referenced_families=tuple(references),
            )
        for marker in markers:
            replacements[marker] = candidates[0]

    generic_family = None
    generic_surface = None
    if generic_markers:
        opaque_result = _opaque_unique_result_followup(
            version, chat.question, generic_markers
        )
        if opaque_result is not None:
            return ContextReferenceResolution(
                completed_question=opaque_result.completed_question,
                next_state=_publish_context_continuation(
                    state=state,
                    task=task,
                    version=version,
                    chat=chat,
                    session=session,
                    completion=opaque_result,
                    now=now,
                ),
                understanding=(
                    "沿用上一轮唯一确定的排名结果对象，"
                    "补全本轮对该对象的继续查询。"
                ),
            )
        generic_candidates = []
        for family in ("COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"):
            values = _context_filter_surfaces(version, family)
            if len(values) > 1:
                generic_candidates.extend((family, value) for value in values)
            elif values:
                generic_candidates.append((family, values[0]))
        frame = version.context_question
        if (
            frame is not None
            and frame.entity is not None
            and frame.entity in frame.dimensions
        ):
            result_family = semantic_filter_family(frame.entity)
            if result_family != "OTHER":
                # The immediate result class is an antecedent in its own right.
                # Including it prevents an older filter value from winning a
                # singular pronoun merely because it is the only filter.
                generic_candidates.append((result_family, frame.entity))
        generic_candidates = list(dict.fromkeys(generic_candidates))
        if len(generic_candidates) != 1:
            return ContextReferenceResolution(
                completed_question=None,
                next_state=None,
                clarification_question=(
                    "上一任务中可由当前指代引用的对象无法唯一确定，请补充完整对象。"
                ),
                referenced_families=tuple(references),
            )
        generic_family, generic_surface = generic_candidates[0]
        for marker in generic_markers:
            replacements[marker] = generic_surface

        ranking = _object_ranking_completed_question(
            session, version, chat.question, generic_surface
        )
        if ranking is not None:
            return ContextReferenceResolution(
                completed_question=ranking.completed_question,
                next_state=_publish_context_continuation(
                    state=state,
                    task=task,
                    version=version,
                    chat=chat,
                    session=session,
                    completion=ranking,
                    now=now,
                ),
                understanding=(
                    "沿用上一轮唯一可确认的查询对象和指标，"
                    f"查询{ranking.label}中的极值对象。"
                ),
                referenced_families=(generic_family,),
            )

    completed = chat.question
    for marker in sorted(replacements, key=len, reverse=True):
        completed = completed.replace(marker, replacements[marker])
    if completed == chat.question:
        return None

    if (
        _OBJECT_RANKING_FOLLOWUP.fullmatch(chat.question) is not None
        and generic_surface is not None
        and not completed.startswith("查询")
    ):
        completed = "查询" + completed

    time_surface = _context_time_surface(version)
    if time_surface and not _TIME_RANGE_SURFACE.search(chat.question):
        completed = _insert_time_surface(completed, time_surface)

    resolved_families = [*references]
    if generic_family is not None:
        resolved_families.append(generic_family)
    labels = "、".join(
        _CONTEXT_FAMILY_LABELS[family] for family in dict.fromkeys(resolved_families)
    )
    explicit_metric = _unique_catalog_mention(
        session, CatalogType.METRIC, chat.question
    )
    explicit_grain = None
    if grain_candidate is not None:
        explicit_grain = {
            "年度": "年",
            "季度": "季",
            "月份": "月",
            "星期": "周",
            "天": "日",
        }.get(grain_candidate.group("grain"), grain_candidate.group("grain"))
    completion = _DirectCompletion(
        completed_question=completed,
        label=labels,
        metrics=(explicit_metric,) if explicit_metric is not None else None,
        dimensions=(explicit_grain,) if explicit_grain is not None else None,
        reset_detail_shape=explicit_metric is not None,
    )
    relation_result_target = _relation_result_target(chat.question)
    if relation_result_target is not None and explicit_metric is None:
        completion = _DirectCompletion(
            completed_question=completed,
            label=relation_result_target,
            metrics=(),
            dimensions=(relation_result_target,),
            result_entity=relation_result_target,
            replace_result_identity=True,
        )
    next_state = _publish_context_continuation(
        state=state,
        task=task,
        version=version,
        chat=chat,
        session=session,
        completion=completion,
        now=now,
    )
    return ContextReferenceResolution(
        completed_question=completed,
        next_state=next_state,
        understanding=(
            f"沿用上一轮已确认的{labels}条件，保留本轮明确提出的其他语义。"
        ),
        referenced_families=tuple(dict.fromkeys(resolved_families)),
    )


def _calendar_label(value, surface: str, now: datetime) -> str:
    start = value.start.astimezone(now.tzinfo)
    end = value.end_exclusive.astimezone(now.tzinfo)
    if (
        start.month == start.day == 1
        and end.month == end.day == 1
        and end.year == start.year + 1
    ):
        return f"{start.year}年"
    return surface


def _replace_time(frame: m.ContextQuestionState, question: str, now: datetime):
    match = _TIME_ONLY.fullmatch(question)
    if match is None:
        return None
    surface = match.group("value").strip()
    normalized = normalize_range(surface, now)
    label = _calendar_label(normalized, surface, now)
    local_start = normalized.start.astimezone(now.tzinfo)
    local_end = normalized.end_exclusive.astimezone(now.tzinfo)
    completed = frame.execution_question
    if frame.time is not None:
        if completed.count(frame.time.surface) != 1:
            return None
        completed = completed.replace(frame.time.surface, label, 1)
    elif completed.startswith("查询"):
        completed = "查询" + label + completed[2:]
    else:
        completed = label + completed
    updated = frame.model_copy(deep=True, update={
        "execution_question": completed,
        "time": m.ContextQuestionTime(
            surface=label,
            start=local_start.date(),
            end_exclusive=local_end.date(),
            evidence_source="CURRENT_EXPLICIT_SURFACE",
        ),
    })
    return updated, surface


async def _resolve_filter_surface(
    surface: str,
    resolve_value: ContextValueResolver | None,
    *,
    preferred_attribute_code: str | None = None,
) -> tuple[str, SemanticFilterBinding] | None:
    if resolve_value is None:
        return None
    matches: list[tuple[str, SemanticFilterBinding]] = []
    for family in ("REGION", "COMMERCIAL_PRODUCT", "HOSPITAL", "PARTNER"):
        binding = await resolve_value(
            surface,
            family,
            preferred_attribute_code if family == "REGION" else None,
        )
        if binding is None:
            continue
        if semantic_filter_family(binding.attribute_code, binding.canonical_name) != family:
            continue
        matches.append((family, binding))
    return matches[0] if len(matches) == 1 else None


def _preferred_context_attribute_code(
    *,
    filters: list[m.ContextQuestionFilter],
    dimensions: list[str],
    family: str,
) -> str | None:
    """Return an already-established attribute level for one terse edit.

    Direct-admin regions such as 上海市 are valid both as a province and a
    city.  A prior `按省份` or `按城市` task is explicit user context that can
    safely select that level.  Existing exact filter evidence is stronger
    still.  No business value or authorization decision is made here.
    """

    existing = list(dict.fromkeys(
        item.attribute_code
        for item in filters
        if item.semantic_family == family and item.attribute_code
    ))
    if len(existing) == 1:
        return existing[0]
    if family != "REGION":
        return None
    levels: set[str] = set()
    for value in dimensions:
        normalized = _text(value)
        if "province" in normalized or "省份" in normalized:
            levels.add("province_name")
        elif "city" in normalized or "城市" in normalized:
            levels.add("city_name")
    return next(iter(levels)) if len(levels) == 1 else None


def _current_filter(
    *, surface: str, family: str, binding: SemanticFilterBinding
) -> m.ContextQuestionFilter:
    return m.ContextQuestionFilter(
        surface=surface,
        canonical_value=binding.canonical_value,
        canonical_name=binding.canonical_name,
        attribute_code=binding.attribute_code,
        semantic_family=family,
        evidence_source="CURRENT_EXPLICIT_SURFACE",
    )


def _render_metric_context(
    *,
    metrics: list[str],
    dimensions: list[str],
    filters: list[m.ContextQuestionFilter],
    time_surface: str | None,
) -> str | None:
    qualifiers = _context_qualifiers_from_filters(filters)
    if len(metrics) != 1 or qualifiers is None:
        return None
    prefix = "".join([time_surface or "", *qualifiers])
    if dimensions:
        return f"按{'、'.join(dimensions)}统计{prefix}的{metrics[0]}。"
    return f"查询{prefix}的{metrics[0]}。" if prefix else f"查询{metrics[0]}。"


def _clear_context_filter(
    frame: m.ContextQuestionState, question: str
):
    family = _contextual_clear_family(question)
    if family is None:
        return None
    removed = [item for item in frame.filters if item.semantic_family == family]
    if not removed:
        return None
    filters = [item for item in frame.filters if item.semantic_family != family]
    dimensions = [
        value for value in frame.dimensions
        if semantic_filter_family(value) != family
    ]
    completed = _render_metric_context(
        metrics=list(frame.metrics),
        dimensions=dimensions,
        filters=filters,
        time_surface=frame.time.surface if frame.time is not None else None,
    )
    if completed is None:
        return None
    updated = frame.model_copy(deep=True, update={
        "execution_question": completed,
        "filters": filters,
        "dimensions": dimensions,
    })
    return updated, question.strip().strip("？?。."), "CLEAR"


async def _replace_filter(
    frame: m.ContextQuestionState,
    question: str,
    resolve_value: ContextValueResolver | None,
):
    surface = _contextual_filter_surface(question)
    if surface is None:
        return None
    resolved = await _resolve_filter_surface(
        surface,
        resolve_value,
        preferred_attribute_code=_preferred_context_attribute_code(
            filters=list(frame.filters),
            dimensions=list(frame.dimensions),
            family="REGION",
        ),
    )
    if resolved is None:
        return None
    family, binding = resolved
    editable = [
        item for item in frame.filters
        if frame.execution_question.count(item.surface) == 1
        and item.semantic_family == family
    ]
    if len(editable) > 1:
        return None
    replacement = _current_filter(
        surface=surface, family=family, binding=binding
    )
    if editable:
        previous = editable[0]
        completed = frame.execution_question.replace(previous.surface, surface, 1)
        filters = [replacement if item == previous else item for item in frame.filters]
        operation = "REPLACE"
    else:
        if _EXPLICIT_FILTER_REPLACEMENT.fullmatch(question) is not None:
            # An explicit REPLACE without a prior condition in the same
            # semantic family has no safe target.  Do not silently turn it into
            # ADD and change two independent slots.
            return None
        completed = (
            "查询" + surface + frame.execution_question[2:]
            if frame.execution_question.startswith("查询")
            else "查询" + surface + frame.execution_question
        )
        filters = [*frame.filters, replacement]
        operation = "ADD"
    updated = frame.model_copy(deep=True, update={
        "execution_question": completed,
        "filters": filters,
    })
    return updated, surface, operation


async def _structured_filter_edit_completion(
    version,
    question: str,
    resolve_value: ContextValueResolver | None,
) -> _DirectCompletion | None:
    """Render a filter-only edit from V2 TaskState without executing it in V2."""

    surface = _contextual_filter_surface(question)
    if surface is None:
        return None
    resolved = await _resolve_filter_surface(
        surface,
        resolve_value,
        preferred_attribute_code=_preferred_context_attribute_code(
            filters=_context_filters_from_version(version),
            dimensions=_context_dimension_surfaces(version),
            family="REGION",
        ),
    )
    if resolved is None:
        return None
    family, binding = resolved
    current_filters = _context_filters_from_version(version)
    prior = [item for item in current_filters if item.semantic_family == family]
    if len(prior) > 1:
        return None
    replacement = _current_filter(
        surface=surface, family=family, binding=binding
    )
    if prior:
        filters = [replacement if item == prior[0] else item for item in current_filters]
        operation = "REPLACE"
    else:
        if _EXPLICIT_FILTER_REPLACEMENT.fullmatch(question) is not None:
            return None
        filters = [*current_filters, replacement]
        operation = "ADD"

    metrics = _context_metric_surfaces(version)
    qualifiers = _context_qualifiers_from_filters(filters)
    if len(metrics) != 1 or not qualifiers:
        return None
    dimensions = [
        value for value in _context_dimension_surfaces(version)
        if semantic_filter_family(value) != family
    ]
    prefix = "".join([_context_time_surface(version) or "", *qualifiers])
    completed = (
        f"按{'、'.join(dimensions)}统计{prefix}的{metrics[0]}。"
        if dimensions
        else f"查询{prefix}的{metrics[0]}。"
    )
    return _DirectCompletion(
        completed_question=completed,
        label=surface,
        metrics=(metrics[0],),
        dimensions=tuple(dimensions),
        filters=tuple(filters),
        operation=operation,
        evidence_surface=surface,
    )


async def _return_to_named_entity_metric_completion(
    session,
    version: TaskVersion,
    question: str,
    resolve_value: ContextValueResolver | None,
) -> _DirectCompletion | None:
    """Complete an explicit named-object return plus a metric replacement.

    The named value is current-turn evidence and must be resolved by V1's
    existing value resolver.  This avoids treating the pronoun as the only
    signal while also refusing to guess an object family or invent a value from
    older conversation text.
    """

    match = _RETURN_TO_NAMED_ENTITY_METRIC.fullmatch(question)
    if match is None:
        return None
    surface = match.group("value").strip().removesuffix("的").strip()
    metric_surface = match.group("metric").strip().removesuffix("的").strip()
    if not surface or not metric_surface:
        return None
    resolved = await _resolve_filter_surface(surface, resolve_value)
    if resolved is None:
        return None
    family, binding = resolved
    current_filters = _context_filters_from_version(version)
    editable = [item for item in current_filters if item.semantic_family == family]
    if len(editable) != 1:
        return None
    metric = _unique_catalog_surface(session, CatalogType.METRIC, metric_surface)
    if metric is None:
        return None
    replacement = _current_filter(
        surface=surface, family=family, binding=binding
    )
    filters = [replacement if item == editable[0] else item for item in current_filters]
    completed = _render_metric_context(
        metrics=[metric],
        dimensions=[],
        filters=filters,
        time_surface=_context_time_surface(version),
    )
    if completed is None:
        return None
    return _DirectCompletion(
        completed_question=completed,
        label=surface,
        metrics=(metric,),
        dimensions=(),
        filters=tuple(filters),
        operation="REPLACE",
        evidence_surface=surface,
        reset_detail_shape=True,
    )


async def _singular_relation_actor_metric_completion(
    session,
    version: TaskVersion,
    question: str,
    resolve_value: ContextValueResolver | None,
) -> _DirectCompletion | None:
    """Resolve singular pronouns to a relation query's unique proven actor."""

    match = _SINGULAR_ACTOR_METRIC.fullmatch(question)
    frame = version.context_question
    if match is None or frame is None:
        return None
    actor_match = _RELATION_ACTOR_QUERY.fullmatch(frame.execution_question)
    if actor_match is None:
        return None
    surface = actor_match.group("actor").strip().removesuffix("的").strip()
    resolved = await _resolve_filter_surface(surface, resolve_value)
    if resolved is None:
        return None
    family, binding = resolved
    metric_surface = match.group("metric").strip().removesuffix("的").strip()
    metric = _unique_catalog_surface(session, CatalogType.METRIC, metric_surface)
    if metric is None:
        return None
    replacement = _current_filter(
        surface=surface, family=family, binding=binding
    )
    relation_markers = ("供货", "供应", "合作", "采购", "购买", "销售")
    filters = [
        item for item in _context_filters_from_version(version)
        if item.semantic_family != family
        and not any(marker in item.surface for marker in relation_markers)
    ]
    filters.append(replacement)
    completed = _render_metric_context(
        metrics=[metric],
        dimensions=[],
        filters=filters,
        time_surface=_context_time_surface(version),
    )
    if completed is None:
        return None
    return _DirectCompletion(
        completed_question=completed,
        label=surface,
        metrics=(metric,),
        dimensions=(),
        filters=tuple(filters),
        operation="REPLACE",
        evidence_surface=surface,
        reset_detail_shape=True,
    )


def _structured_filter_clear_completion(
    version: TaskVersion, question: str
) -> _DirectCompletion | None:
    family = _contextual_clear_family(question)
    if family is None:
        return None
    current_filters = _context_filters_from_version(version)
    if not any(item.semantic_family == family for item in current_filters):
        return None
    filters = [
        item for item in current_filters if item.semantic_family != family
    ]
    dimensions = [
        value for value in _context_dimension_surfaces(version)
        if semantic_filter_family(value) != family
    ]
    metrics = _context_metric_surfaces(version)
    completed = _render_metric_context(
        metrics=metrics,
        dimensions=dimensions,
        filters=filters,
        time_surface=_context_time_surface(version),
    )
    if completed is None:
        return None
    return _DirectCompletion(
        completed_question=completed,
        label=_CONTEXT_FAMILY_LABELS[family],
        metrics=tuple(metrics),
        dimensions=tuple(dimensions),
        filters=tuple(filters),
        operation="CLEAR",
        evidence_surface=question.strip().strip("？?。."),
    )


async def resolve_context_question_followup(
    *,
    chat: ChatRequest,
    identity: TrustedIdentity,
    state_artifact: ScopedArtifact | None,
    catalog,
    resolved_business_domain_ids,
    now: datetime,
    resolve_value: ContextValueResolver | None = None,
) -> ContextQuestionResolution | None:
    if state_artifact is None:
        return None
    session = ScopedPlanSession(
        chat,
        identity,
        catalog,
        resolved_business_domain_ids=resolved_business_domain_ids,
    )
    state = ConversationState.model_validate(
        session.restore(state_artifact, kind="CONVERSATION", defer_source_values=True)
    )
    active = _active_task_version(state)
    if active is None:
        return None
    task, version = active
    actor_metric = await _singular_relation_actor_metric_completion(
        session, version, chat.question, resolve_value
    )
    if actor_metric is not None:
        return ContextQuestionResolution(
            completed_question=actor_metric.completed_question,
            next_state=_publish_context_continuation(
                state=state,
                task=task,
                version=version,
                chat=chat,
                session=session,
                completion=actor_metric,
                now=now,
            ),
            relation="FOLLOW_UP",
            operation="REPLACE",
            slot="metrics",
            understanding=(
                "沿用上一轮关系查询中唯一确认的发起对象，"
                "按本轮明确指定的指标继续查询。"
            ),
        )
    named_return = await _return_to_named_entity_metric_completion(
        session, version, chat.question, resolve_value
    )
    if named_return is not None:
        return ContextQuestionResolution(
            completed_question=named_return.completed_question,
            next_state=_publish_context_continuation(
                state=state,
                task=task,
                version=version,
                chat=chat,
                session=session,
                completion=named_return,
                now=now,
            ),
            relation="MODIFY",
            operation="REPLACE",
            slot="filter_expression",
            understanding=(
                "按本轮明确指定的对象返回对应查询范围，"
                "并将指标替换为本轮明确指定的指标。"
            ),
        )
    frame = version.context_question
    if frame is None:
        completion = _structured_filter_clear_completion(version, chat.question)
        if completion is None:
            completion = await _structured_filter_edit_completion(
                version, chat.question, resolve_value
            )
        if completion is None:
            return None
        operation_verb = {
            "REPLACE": "替换",
            "ADD": "增加",
            "CLEAR": "清除",
        }[completion.operation]
        return ContextQuestionResolution(
            completed_question=completion.completed_question,
            next_state=_publish_context_continuation(
                state=state,
                task=task,
                version=version,
                chat=chat,
                session=session,
                completion=completion,
                now=now,
            ),
            relation="MODIFY",
            operation=completion.operation,
            slot="filter_expression",
            understanding=(
                "沿用上一轮已确认的查询对象、指标和其他条件，"
                f"{operation_verb}"
                f"本轮明确指定的{completion.label}条件。"
            ),
        )
    resolved = _replace_time(frame, chat.question, now)
    slot = "time_spec"
    operation = "REPLACE"
    understanding = "沿用上一轮查询目标和条件，仅替换本轮明确指定的时间。"
    if resolved is None:
        resolved = _clear_context_filter(frame, chat.question)
        slot = "filter_expression"
        if resolved is not None:
            operation = resolved[2]
        understanding = (
            "沿用上一轮查询目标和其他条件，"
            "清除本轮明确取消的条件。"
        )
    if resolved is None:
        resolved = await _replace_filter(frame, chat.question, resolve_value)
        slot = "filter_expression"
        if resolved is not None:
            operation = resolved[2]
        understanding = (
            "沿用上一轮查询目标和其他条件，"
            f"{('替换' if operation == 'REPLACE' else '增加')}"
            "本轮明确指定的实体条件。"
        )
    if resolved is None:
        return None
    updated_frame, evidence_surface = resolved[:2]
    updated_frame = updated_frame.model_copy(update={
        "source_message_id": chat.message_id,
        "semantic_catalog_version": session.context.catalog_pin.catalog_version,
        "last_edit": m.ContextQuestionEdit(
            operation=operation,
            slot=slot,
            source_message_id=chat.message_id,
            evidence_surface=evidence_surface,
        ),
    })
    data = state.model_dump(mode="python")
    changed = data["tasks"][task.task_id]
    for item in changed["versions"]:
        if item["version"] == task.active_version:
            item["status"] = "SUPERSEDED"
    next_version = task.active_version + 1
    changed["versions"].append(TaskVersion(
        version=next_version,
        status="PROVISIONAL",
        semantics=version.semantics,
        context_question=updated_frame,
        current_turn_ref=chat.message_id,
        current_turn_digest=contract_digest(chat.question),
        created_at=now,
    ).model_dump(mode="python"))
    changed["active_version"] = next_version
    changed["status"] = "PROVISIONAL"
    data["recent_turn_ids"] = [*data["recent_turn_ids"], chat.message_id][-100:]
    data["state_version"] += 1
    next_state = ConversationState.model_validate(data)
    session.accept_catalog()
    artifact = session.seal(kind="CONVERSATION", payload=next_state)
    return ContextQuestionResolution(
        completed_question=updated_frame.execution_question,
        next_state=artifact,
        relation="MODIFY",
        operation=operation,
        slot=slot,
        understanding=understanding,
    )
