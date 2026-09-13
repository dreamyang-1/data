"""Context-only TaskState for questions executed by the original V1 chain.

The live bridge sometimes cannot lower a self-contained business question to a
V2 execution payload even though V1 can execute it.  After V1 returns a real
query result, this module retains only the conversational meaning required to
complete later edits.  It never stores or reconstructs V1 authorization,
retrieval, database, dataset or execution contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Awaitable, Callable

from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    SemanticFilterBinding,
    TrustedIdentity,
)

from . import models as m
from .authorized_contract import ScopedArtifact, contract_digest, scoped_artifact_material
from .catalog_bridge import ScopedPlanSession
from .explicit_time import normalize_range
from .state_machine import ConversationState, TaskState, TaskVersion, TopicState


_QUERY_VERBS = re.compile(
    r"查询|统计|分析|列出|查找|查看|计算|比较|对比|生成|预测|多少|哪些|为什么|怎么"
)
_ELLIPTICAL_VALUE = re.compile(
    r"^\s*(?:那|那么)?\s*(?P<value>.+?)\s*(?:呢|怎么样|如何)\s*[？?。.]?\s*$"
)
_TIME_ONLY = re.compile(
    r"^\s*(?:(?:把)?时间)?\s*(?:换成|改成|改为|换到|改到|换|改)?\s*"
    r"(?P<value>(?:19|20)\d{2}年?|今年|去年|明年)\s*(?:呢)?\s*[？?。.]?\s*$"
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


ContextValueResolver = Callable[
    [str, str], Awaitable[SemanticFilterBinding | None]
]


@dataclass(frozen=True)
class ContextQuestionResolution:
    completed_question: str
    next_state: ScopedArtifact
    relation: str
    operation: str
    slot: str
    understanding: str


def _text(value: object) -> str:
    return str(value or "").strip().casefold()


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
) -> bool:
    """Accept V1 evidence only for the exact trusted request just executed."""
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
        and request.source_dataset_id == chat.dataset_id
        and request.original_question == chat.question
    )


def build_context_question(
    *,
    chat: ChatRequest,
    parse,
    v1_request: CanonicalAnalysisRequest | None,
    catalog_version: str | None,
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
        original_question=chat.question,
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


def _active_context_version(state: ConversationState):
    topic = state.topics.get(state.active_topic_id)
    if topic is None or topic.active_task_id is None:
        return None
    task = state.tasks.get(topic.active_task_id)
    if task is None:
        return None
    version = next(
        (item for item in task.versions if item.version == task.active_version), None
    )
    if version is None or version.context_question is None:
        return None
    return task, version


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


def is_contextual_short_edit(question: str) -> bool:
    return _TIME_ONLY.fullmatch(question) is not None or is_contextual_ellipsis(question)


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


async def _replace_filter(
    frame: m.ContextQuestionState,
    question: str,
    resolve_value: ContextValueResolver | None,
):
    match = _ELLIPTICAL_VALUE.fullmatch(question)
    if match is None or not is_contextual_ellipsis(question):
        return None
    surface = match.group("value").strip()
    editable = [
        item for item in frame.filters
        if frame.execution_question.count(item.surface) == 1
        and item.semantic_family != "OTHER"
    ]
    if len(editable) != 1:
        return None
    previous = editable[0]
    if resolve_value is None:
        return None
    binding = await resolve_value(surface, previous.semantic_family)
    if binding is None:
        return None
    if semantic_filter_family(binding.attribute_code, binding.canonical_name) != (
        previous.semantic_family
    ):
        return None
    completed = frame.execution_question.replace(previous.surface, surface, 1)
    replacement = m.ContextQuestionFilter(
        surface=surface,
        canonical_value=binding.canonical_value,
        canonical_name=binding.canonical_name,
        attribute_code=binding.attribute_code,
        semantic_family=previous.semantic_family,
        evidence_source="CURRENT_EXPLICIT_SURFACE",
    )
    filters = [replacement if item == previous else item for item in frame.filters]
    updated = frame.model_copy(deep=True, update={
        "execution_question": completed,
        "filters": filters,
    })
    return updated, surface


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
    active = _active_context_version(state)
    if active is None:
        return None
    task, version = active
    frame = version.context_question
    resolved = _replace_time(frame, chat.question, now)
    slot = "time_spec"
    understanding = "沿用上一轮查询目标和条件，仅替换本轮明确指定的时间。"
    if resolved is None:
        resolved = await _replace_filter(frame, chat.question, resolve_value)
        slot = "filter_expression"
        understanding = "沿用上一轮查询目标和其他条件，仅替换本轮明确指定的实体条件。"
    if resolved is None:
        return None
    updated_frame, evidence_surface = resolved
    updated_frame = updated_frame.model_copy(update={
        "source_message_id": chat.message_id,
        "semantic_catalog_version": session.context.catalog_pin.catalog_version,
        "last_edit": m.ContextQuestionEdit(
            operation="REPLACE",
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
        operation="REPLACE",
        slot=slot,
        understanding=understanding,
    )
