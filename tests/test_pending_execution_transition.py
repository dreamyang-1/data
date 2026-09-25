import json
from datetime import date

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterBundle, AdapterError
from app.adapters.mock import MockDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    ConversationControl,
    MetricRef,
    PendingState,
    PrimaryIntent,
    TimeRange,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")


class AmbiguousThenSuccessfulRetrieval:
    def __init__(self) -> None:
        self.requests: list[CanonicalAnalysisRequest] = []
        self.delegate = MockDataRetrievalAdapter()

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if len(self.requests) == 1:
            ambiguity = [{
                "type": "metric",
                "question": "销售额请选择含税或不含税口径。",
                "candidates": ["含税销售额", "不含税销售额"],
            }]
            raise AdapterError(
                "ASL_AMBIGUOUS", json.dumps(ambiguity, ensure_ascii=False),
                details=ambiguity,
            )
        return await self.delegate.query(request, identity, **kwargs)


class FailingRetrieval:
    def __init__(self) -> None:
        self.requests: list[CanonicalAnalysisRequest] = []

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        raise AdapterError("SQL_EXECUTION_FAILED", "database unavailable")


class CapturingSuccessfulRetrieval:
    def __init__(self) -> None:
        self.requests: list[CanonicalAnalysisRequest] = []
        self.delegate = MockDataRetrievalAdapter()

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        return await self.delegate.query(request, identity, **kwargs)


def service(retrieval):
    defaults = build_mock_adapters()
    sessions = InMemorySessionStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=False
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=AdapterBundle(
            semantic=defaults.semantic,
            retrieval=retrieval,
            knowledge=defaults.knowledge,
        ),
        sessions=sessions,
    )
    return agent, sessions


class CountingHybridClassifier:
    def __init__(self):
        self.rules = RuleBasedIntentClassifier()
        self.model_path_calls = 0

    async def classify(self, question, identity, conversation_id):
        self.model_path_calls += 1
        return self.rules.classify(question, identity, conversation_id)

    def merge_clarification(self, previous, answer):
        return self.rules.merge_clarification(previous, answer)


async def seed_time_pending(sessions, conversation_id: str, question: str = "查询销售额") -> None:
    """Seed a genuine missing-``time_range`` pending through the official contract.

    The rule default now injects ``DEFAULT_TIME_RANGE=LATEST_ONE_YEAR`` (or an
    all-time business scope), so a natural first report turn no longer leaves a
    missing time slot. The scenarios below therefore seed the exact pending
    shape via ``PendingState``/``SessionStore.put_pending`` instead of relying
    on the obsolete natural first-turn assumption.
    """
    seeded = RuleBasedIntentClassifier().classify(question, IDENTITY, conversation_id)
    seeded.semantic_model_id = 81
    seeded.application_id = "app-1"
    seeded.time_range = None
    seeded.missing_slots = ["time_range"]
    seeded.assumptions = [
        assumption for assumption in seeded.assumptions
        if not assumption.startswith(("DEFAULT_TIME_RANGE", "TIME_SCOPE"))
    ]
    await sessions.put_pending(
        PendingState(request=seeded, clarification_rounds=1, state_version=1),
        expected_version=0,
    )


def pending_partner_request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="collection-delta",
        tenant_id="tenant-1",
        user_id="user-1",
        original_question="按整体业务规模对经销商排序",
        primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        metrics=[MetricRef(input="整体业务规模")],
        entity="经销商",
        fields=["经销商名称"],
        dimensions=["经销商"],
        filters=[{"field": "地区", "operator": "EQ", "value": "上海市"}],
        comparison_type="对象间比较",
        missing_slots=["time_range"],
    )


def resolved_partner_patch() -> CanonicalAnalysisRequest:
    patch = pending_partner_request().model_copy(deep=True)
    patch.time_range = TimeRange(
        start=date(2026, 8, 1),
        end_exclusive=date(2026, 9, 1),
    )
    patch.missing_slots = []
    patch.conversation_control = ConversationControl.CLARIFICATION_RESPONSE
    return patch


def test_time_clarification_can_add_explicit_exclusion_filter():
    pending = pending_partner_request()
    result = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        resolved_partner_patch(),
        clarification_answer="本月，并排除甲公司",
    )

    assert result.filters == [
        {"field": "地区", "operator": "EQ", "value": "上海市"},
        {"field": "经销商名称", "operator": "NE", "value": "甲公司"},
    ]
    assert result.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert [item.input for item in result.metrics] == ["整体业务规模"]


def test_time_clarification_can_add_explicit_field_without_replacing_old_fields():
    pending = pending_partner_request()
    result = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        resolved_partner_patch(),
        clarification_answer="本月，再加字段X",
    )

    assert result.fields == ["经销商名称", "X"]
    assert result.dimensions == ["经销商"]


def test_time_clarification_unions_explicit_dimension_delta_only():
    pending = pending_partner_request()
    patch = resolved_partner_patch()
    patch.dimensions = ["渠道"]

    result = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        patch,
        clarification_answer="本月，并按渠道拆分",
    )

    assert result.dimensions == ["经销商", "渠道"]
    assert result.fields == ["经销商名称"]


@pytest.mark.asyncio
async def test_closed_form_pending_reply_skips_async_intent_model_path():
    defaults = build_mock_adapters()
    classifier = CountingHybridClassifier()
    sessions = InMemorySessionStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=True),
        classifier=classifier,
        adapters=defaults,
        sessions=sessions,
    )
    # The default time policy now fills the first report turn, so the genuine
    # missing-time pending is seeded through the official SessionStore contract.
    await seed_time_pending(sessions, "fast-slot")
    seeded = await sessions.get_pending("tenant-1", "user-1", "app-1", "fast-slot")
    assert seeded is not None
    assert seeded.request.missing_slots == ["time_range"]
    classifier.model_path_calls = 0
    second = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1", conversation_id="fast-slot",
            message_id="message-2", question="本月",
        ),
        IDENTITY,
    )
    assert second.status == "COMPLETED"
    assert classifier.model_path_calls == 0


@pytest.mark.asyncio
async def test_filled_slot_can_transition_to_asl_clarification_then_complete():
    retrieval = AmbiguousThenSuccessfulRetrieval()
    agent, sessions = service(retrieval)

    # Seed the legally clarifiable missing-time pending instead of relying on
    # the obsolete natural first-turn assumption.
    await seed_time_pending(sessions, "multi-stage", question="帮我查销售额")

    second = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1",
            conversation_id="multi-stage",
            message_id="message-2",
            question="本月",
        ),
        IDENTITY,
    )

    assert second.status == "NEEDS_CLARIFICATION"
    assert second.missing_slots == ["semantic_ambiguity"]
    assert second.clarification_round == 2
    assert second.clarification_questions == ["销售额请选择含税或不含税口径。"]
    assert second.clarification_items[0].title == "指标口径"
    assert second.clarification_items[0].options == ["含税销售额", "不含税销售额"]
    assert second.clarification_items[0].multi_select is False
    pending = await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "multi-stage"
    )
    assert pending is not None
    assert pending.state_version == 2
    assert pending.clarification_rounds == 2
    assert "补充：本月" in pending.request.original_question
    assert "补充：" not in pending.request.rewritten_question
    assert "指标：销售额" in pending.request.rewritten_question
    assert "时间范围：" in pending.request.rewritten_question

    # Selecting the full catalog candidate exercises the governed choice
    # path, which applies the selection to the original metric slot, restores
    # the original task and clears the pending state. (The bare "含税" answer
    # no longer renders a confirmation line: the default one-year time scope
    # makes the isolated answer parse as a structured change.)
    third = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1",
            conversation_id="multi-stage",
            message_id="message-3",
            question="含税销售额",
        ),
        IDENTITY,
    )

    assert third.status == "COMPLETED"
    assert third.intent == PrimaryIntent.METRIC_QUERY
    assert len(retrieval.requests) == 2
    final_request = retrieval.requests[-1]
    assert final_request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in final_request.metrics] == ["含税销售额"]
    assert "补充：本月" in final_request.original_question
    # Confirmed choices enrich the completed question, not the source wording.
    assert final_request.original_question == pending.request.original_question
    assert "补充：" not in final_request.rewritten_question
    assert "指标：含税销售额" in final_request.rewritten_question
    assert (
        await sessions.get_pending(
            "tenant-1", "user-1", "app-1", "multi-stage"
        )
        is None
    )


@pytest.mark.asyncio
async def test_terminal_dependency_failure_clears_resolved_pending_state():
    retrieval = FailingRetrieval()
    agent, sessions = service(retrieval)

    # Seed the missing-time pending directly; the only user turn is the date
    # answer, so a terminal retrieval failure must happen exactly once.
    await seed_time_pending(sessions, "terminal-failure", question="帮我查销售额")

    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1",
            conversation_id="terminal-failure",
            message_id="message-2",
            question="本月",
        ),
        IDENTITY,
    )

    assert response.status == "SAFE_FALLBACK"
    assert len(retrieval.requests) == 1
    assert "补充：" not in retrieval.requests[0].rewritten_question
    assert "指标：销售额" in retrieval.requests[0].rewritten_question
    assert "时间范围：" in retrieval.requests[0].rewritten_question
    assert (
        await sessions.get_pending(
            "tenant-1", "user-1", "app-1", "terminal-failure"
        )
        is None
    )


@pytest.mark.asyncio
async def test_corrected_pending_request_sends_no_negated_metric_to_retrieval():
    retrieval = FailingRetrieval()
    agent, sessions = service(retrieval)

    # Seed the missing-time pending for the original metric so the correction
    # turn answers an existing clarification instead of starting a new task.
    await seed_time_pending(sessions, "metric-correction", question="查询销售额")

    await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1", conversation_id="metric-correction",
            message_id="message-2", question="不是销售额，是订单量，查2026年7月",
        ),
        IDENTITY,
    )

    assert len(retrieval.requests) == 1
    executed = retrieval.requests[0]
    assert [metric.input for metric in executed.metrics] == ["订单量"]
    assert "指标：订单量" in executed.rewritten_question
    assert "2026-07-01" in executed.rewritten_question
    assert "销售额" not in executed.rewritten_question
    assert "不是" not in executed.rewritten_question


@pytest.mark.asyncio
async def test_correction_after_completed_turn_replaces_old_dimension():
    retrieval = CapturingSuccessfulRetrieval()
    agent, _ = service(retrieval)

    first = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1", conversation_id="dimension-correction",
            message_id="message-1",
            question="查询2026年7月按区域拆分销售额",
        ),
        IDENTITY,
    )
    assert first.status == "COMPLETED"

    second = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app-1", conversation_id="dimension-correction",
            message_id="message-2", question="不看区域了，按渠道拆分",
        ),
        IDENTITY,
    )

    assert second.status == "COMPLETED"
    assert len(retrieval.requests) == 2
    executed = retrieval.requests[-1]
    assert executed.dimensions == ["渠道"]
    assert "分析维度：渠道" in executed.rewritten_question
    assert "分析维度：区域" not in executed.rewritten_question
    assert [metric.input for metric in executed.metrics] == ["销售额"]
    assert "2026-07-31（含首尾）" in executed.rewritten_question
