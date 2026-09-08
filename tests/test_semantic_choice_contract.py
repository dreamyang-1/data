"""A catalog clarification resolves one slot member, preserving the task."""
import json
from datetime import date

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterBundle, AdapterError
from app.adapters.mock import MockDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest, ChatRequest, MetricRef, PrimaryIntent,
    SemanticAmbiguity, TimeRange, TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="choice-tenant", user_id="choice-user")


def ambiguity(kind="metric", phrase="销售额"):
    names = ["含税销售额", "不含税销售额"] if kind == "metric" else ["省份", "城市"]
    return SemanticAmbiguity(
        type=kind, phrase=phrase, ambiguity_id="catalog-choice",
        question="请选择本次使用的业务口径。", candidates=names,
        candidate_details=[{"canonical_name": name, "canonical_code": f"option_{i}", "version": "v1"}
                           for i, name in enumerate(names)],
        affected_slots=["metrics" if kind == "metric" else "dimensions"],
        semantic_model_id=81,
    )


def pending(kind="metric", phrase="销售额"):
    return CanonicalAnalysisRequest(
        application_id="choice-app", conversation_id="choice-conversation",
        tenant_id=IDENTITY.tenant_id, user_id=IDENTITY.user_id,
        original_question="查询2026年7月按区域和渠道拆分销售额和订单量",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=81,
        metrics=[MetricRef(input="销售额"), MetricRef(input="订单量", metric_id="81:orders", version="v2")],
        dimensions=["区域", "渠道"],
        filters=[{"field": "地区", "operator": "EQ", "value": "上海市"}],
        time_range=TimeRange(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)),
        missing_slots=["semantic_ambiguity"],
        semantic_ambiguities=[ambiguity(kind, phrase)],
    )


def choose(request, answer="1"):
    choice = DataAnalysisOrchestrator._semantic_clarification_choice(request, answer)
    assert choice is not None
    return DataAnalysisOrchestrator._apply_semantic_clarification_choice(
        request, request.model_copy(deep=True), choice,
    )


def test_metric_choice_keeps_other_metric_and_metadata():
    request = pending()
    result = choose(request)
    assert [m.input for m in result.metrics] == ["含税销售额", "订单量"]
    assert result.metrics[1] == request.metrics[1]
    assert result.dimensions == request.dimensions
    assert result.filters == request.filters
    assert result.time_range == request.time_range
    assert request.metrics[0].input == "销售额"


def test_dimension_choice_keeps_other_grouping():
    request = pending("dimension", "区域")
    result = choose(request)
    assert result.dimensions == ["省份", "渠道"]
    assert result.metrics == request.metrics
    assert result.filters == request.filters


class AmbiguousRetrieval:
    def __init__(self, kind="metric", phrase="销售额", terminal_error=False):
        self.requests = []
        self.delegate = MockDataRetrievalAdapter()
        self.ambiguity = ambiguity(kind, phrase)
        self.terminal_error = terminal_error

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        members = [m.input for m in request.metrics] if self.ambiguity.type == "metric" else request.dimensions
        if not any(name in self.ambiguity.candidates for name in members):
            details = [self.ambiguity.model_dump(mode="json")]
            raise AdapterError("ASL_AMBIGUOUS", json.dumps(details, ensure_ascii=False), details=details)
        if self.terminal_error:
            raise AdapterError("SQL_EXECUTION_FAILED", "synthetic offline dependency failure")
        return await self.delegate.query(request, identity, **kwargs)


def service(retrieval):
    defaults = build_mock_adapters()
    return DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(), sessions=InMemorySessionStore(),
        adapters=AdapterBundle(semantic=defaults.semantic, retrieval=retrieval, knowledge=defaults.knowledge),
    )


def chat(question, message="message-1"):
    return ChatRequest(semantic_model_id=81, application_id="choice-app",
                       conversation_id="choice-conversation", message_id=message, question=question)


@pytest.mark.asyncio
async def test_real_clarification_round_preserves_second_metric():
    retrieval = AmbiguousRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.missing_slots == ["semantic_ambiguity"]
    assert [m.input for m in retrieval.requests[0].metrics] == ["销售额", "订单量"]
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["含税销售额", "订单量"]
    assert retrieval.requests[-1].time_range == retrieval.requests[0].time_range


@pytest.mark.parametrize("kind", ["metric", "dimension"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_choice_replaces_only_named_member_in_place(kind, position):
    names = ["成员甲", "成员乙", "成员丙"]
    request = pending(kind, names[position])
    if kind == "metric":
        request.metrics = [MetricRef(input=name, metric_id=f"81:old_{i}", version="v7") for i, name in enumerate(names)]
    else:
        request.dimensions = names.copy()
    before = request.model_dump(mode="json")
    result = choose(request, "2")
    expected = names.copy()
    expected[position] = request.semantic_ambiguities[0].candidates[1]
    actual = [m.input for m in result.metrics] if kind == "metric" else result.dimensions
    assert actual == expected
    assert request.model_dump(mode="json") == before
    assert not result.semantic_ambiguities


@pytest.mark.parametrize("alias", ["规范销售额", "81:old_sales"])
def test_catalog_phrase_can_target_existing_canonical_name_or_id(alias):
    request = pending(phrase=alias)
    request.metrics[0].canonical_name = "规范销售额"
    request.metrics[0].metric_id = "81:old_sales"
    result = choose(request)
    assert [m.input for m in result.metrics] == ["含税销售额", "订单量"]
    assert result.metrics[0].metric_id == "81:option_0"


@pytest.mark.parametrize("kind", ["metric", "dimension"])
@pytest.mark.parametrize("phrase", [None, "不存在的成员"])
def test_unknown_target_preserves_entire_pending_without_confirmation(kind, phrase):
    request = pending(kind)
    request.semantic_ambiguities[0].phrase = phrase
    result = choose(request)
    assert result.metrics == request.metrics
    assert result.dimensions == request.dimensions
    assert result.semantic_ambiguities == request.semantic_ambiguities
    assert result.missing_slots == ["semantic_ambiguity"]
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" not in result.assumptions
    assert "SEMANTIC_CHOICE_TARGET_UNRESOLVED" in result.assumptions


@pytest.mark.parametrize("kind", ["metric", "dimension"])
@pytest.mark.parametrize("empty", [False, True])
def test_legacy_no_phrase_can_resolve_empty_or_singleton_slot(kind, empty):
    request = pending(kind)
    request.semantic_ambiguities[0].phrase = None
    if kind == "metric":
        request.metrics = [] if empty else [request.metrics[0]]
    else:
        request.dimensions = [] if empty else [request.dimensions[0]]
    result = choose(request)
    members = [m.input for m in result.metrics] if kind == "metric" else result.dimensions
    assert members == [request.semantic_ambiguities[0].candidates[0]]
    assert not result.semantic_ambiguities


@pytest.mark.parametrize("kind", ["metric", "dimension"])
def test_duplicate_target_does_not_pick_first_member(kind):
    request = pending(kind, "重复名称")
    if kind == "metric":
        request.metrics = [MetricRef(input="重复名称", metric_id=f"81:{i}") for i in range(2)]
    else:
        request.dimensions = ["重复名称", "重复名称"]
    result = choose(request)
    assert result.metrics == request.metrics
    assert result.dimensions == request.dimensions
    assert result.semantic_ambiguities


def test_label_only_dimension_option_changes_only_its_target():
    request = pending("dimension", "区域")
    request.semantic_ambiguities[0].candidate_details = []
    result = choose(request, "2")
    assert result.dimensions == ["城市", "渠道"]


def test_two_independent_choices_preserve_previously_confirmed_member():
    request = pending()
    second = ambiguity("dimension", "区域")
    second.ambiguity_id = "dimension-choice"
    request.semantic_ambiguities.append(second)
    first_result = choose(request)
    assert [a.ambiguity_id for a in first_result.semantic_ambiguities] == ["dimension-choice"]
    final = choose(first_result, "2")
    assert [m.input for m in final.metrics] == ["含税销售额", "订单量"]
    assert final.dimensions == ["城市", "渠道"]
    assert not final.semantic_ambiguities
    assert final.filters == request.filters


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["1", "第一个", "含税销售额"])
async def test_confirmed_metrics_survive_later_time_followup(answer):
    retrieval = AmbiguousRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.clarification_decision_traces[0].reason_type == "USER_SEMANTIC_AMBIGUITY"
    second = await agent.handle(chat(answer, "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    third = await agent.handle(chat("那2026年8月呢？", "message-3"), IDENTITY)
    assert third.status == "COMPLETED"
    executed = retrieval.requests[-1]
    assert [m.input for m in executed.metrics] == ["含税销售额", "订单量"]
    assert executed.time_range.start == date(2026, 8, 1)
    assert "订单量" in executed.rewritten_question
    assert await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation") is None


@pytest.mark.asyncio
async def test_real_dimension_choice_preserves_other_grouping():
    retrieval = AmbiguousRetrieval("dimension", "区域")
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月按区域和渠道拆分销售额"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert retrieval.requests[0].dimensions == ["区域", "渠道"]
    second = await agent.handle(chat("2", "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    assert retrieval.requests[-1].dimensions == ["城市", "渠道"]


@pytest.mark.asyncio
async def test_missing_target_cannot_execute_or_repeat_question():
    retrieval = AmbiguousRetrieval(phrase=None)
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    count = len(retrieval.requests)
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "SAFE_FALLBACK"
    assert not second.clarification_questions
    assert len(retrieval.requests) == count
    stored = await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation")
    assert stored is not None
    assert [m.input for m in stored.request.metrics] == ["销售额", "订单量"]


@pytest.mark.asyncio
async def test_terminal_failure_clears_resolved_pending_without_losing_slots():
    retrieval = AmbiguousRetrieval(terminal_error=True)
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "SAFE_FALLBACK"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["含税销售额", "订单量"]
    assert await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [81, 82])
async def test_complete_new_task_does_not_apply_old_candidate_or_inherit_slots(model):
    retrieval = AmbiguousRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    current = chat("查询2026年8月销售量", "message-2").model_copy(update={"semantic_model_id": model})
    await agent.handle(current, IDENTITY)
    executed = retrieval.requests[-1]
    assert [m.input for m in executed.metrics] == ["销售量"]
    assert executed.semantic_model_id == model
    assert executed.time_range.start == date(2026, 8, 1)
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" not in executed.assumptions


class TwoAmbiguitiesRetrieval(AmbiguousRetrieval):
    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if not any(m.input == "含税销售额" for m in request.metrics):
            other = ambiguity("dimension", "区域")
            other.ambiguity_id = "other-catalog-choice"
            details = [ambiguity().model_dump(mode="json"), other.model_dump(mode="json")]
            raise AdapterError("ASL_AMBIGUOUS", json.dumps(details, ensure_ascii=False), details=details)
        return await self.delegate.query(request, identity, **kwargs)


@pytest.mark.asyncio
async def test_real_sequential_clarifications_keep_both_collections():
    retrieval = TwoAmbiguitiesRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月按区域和渠道拆分销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    count = len(retrieval.requests)
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "NEEDS_CLARIFICATION"
    assert second.clarification_items[0].options == ["省份", "城市"]
    assert len(retrieval.requests) == count
    third = await agent.handle(chat("2", "message-3"), IDENTITY)
    assert third.status == "COMPLETED"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["含税销售额", "订单量"]
    assert retrieval.requests[-1].dimensions == ["城市", "渠道"]
