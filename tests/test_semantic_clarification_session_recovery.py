import json

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterBundle, AdapterError
from app.adapters.mock import MockDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    HistoryMessage,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")
REPORT_QUESTION = "我在上海卖外周插管中心静脉导管，给我生成分析报告"
SUBJECT_REPLY = "产品名称是外周插管中心静脉导管"


class SubjectReplyLooksLikeChatClassifier:
    """Reproduce the model-path misclassification seen in the real service."""

    def __init__(self) -> None:
        self.rules = RuleBasedIntentClassifier()
        self.model_path_questions: list[str] = []

    async def classify(self, question, identity, conversation_id):
        self.model_path_questions.append(question)
        request = self.rules.classify(question, identity, conversation_id)
        if question == SUBJECT_REPLY:
            request.primary_intent = PrimaryIntent.CHAT
            request.missing_slots = []
        return request

    def merge_clarification(self, previous, answer):
        return self.rules.merge_clarification(previous, answer)


class SubjectAmbiguousThenSuccessfulRetrieval:
    def __init__(self) -> None:
        self.requests: list[CanonicalAnalysisRequest] = []
        self.delegate = MockDataRetrievalAdapter()

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if len(self.requests) == 1:
            ambiguity = [{
                "type": "subject",
                "question": "请确认具体商品名称或商品编码。",
                "candidates": [],
            }]
            raise AdapterError(
                "ASL_AMBIGUOUS",
                json.dumps(ambiguity, ensure_ascii=False),
                details=ambiguity,
            )
        return await self.delegate.query(request, identity, **kwargs)


def service():
    defaults = build_mock_adapters()
    classifier = SubjectReplyLooksLikeChatClassifier()
    retrieval = SubjectAmbiguousThenSuccessfulRetrieval()
    sessions = InMemorySessionStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=True
        ),
        classifier=classifier,
        adapters=AdapterBundle(
            semantic=defaults.semantic,
            retrieval=retrieval,
            knowledge=defaults.knowledge,
        ),
        sessions=sessions,
    )
    return agent, classifier, retrieval, sessions


async def advance_to_subject_clarification(agent):
    first = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="subject-session",
            message_id="message-1",
            question=REPORT_QUESTION,
        ),
        IDENTITY,
    )
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.missing_slots == ["time_range"]

    second = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="subject-session",
            message_id="message-2",
            question="今年",
        ),
        IDENTITY,
    )
    assert second.status == "NEEDS_CLARIFICATION"
    assert second.intent == PrimaryIntent.REPORT_GENERATION
    assert second.missing_slots == ["semantic_ambiguity"]
    return first, second


@pytest.mark.asyncio
async def test_explicit_day_range_resolves_report_time_without_model_reinterpretation():
    agent, classifier, retrieval, sessions = service()
    first = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="report-time-session",
            message_id="message-1",
            question=REPORT_QUESTION,
        ),
        IDENTITY,
    )
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.missing_slots == ["time_range"]

    date_answer = "2025年10月17日至2025年12月30日"
    second = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="report-time-session",
            message_id="message-2",
            question=date_answer,
        ),
        IDENTITY,
    )

    # The retrieval stub deliberately asks a later subject clarification.  The
    # important invariant here is that the closed-form date answer resolved the
    # existing report slot and was never reclassified as a new model request.
    assert second.status == "NEEDS_CLARIFICATION"
    assert second.intent == PrimaryIntent.REPORT_GENERATION
    assert second.missing_slots == ["semantic_ambiguity"]
    assert date_answer not in classifier.model_path_questions
    assert len(retrieval.requests) == 1
    submitted = retrieval.requests[0]
    assert submitted.time_range is not None
    assert submitted.time_range.start.isoformat() == "2025-10-17"
    assert submitted.time_range.end_exclusive.isoformat() == "2025-12-31"
    assert {item["value"] for item in submitted.filters} == {
        "上海市", "外周插管中心静脉导管",
    }

    pending = await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "report-time-session"
    )
    assert pending is not None
    assert pending.request.time_range == submitted.time_range


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True])
async def test_three_turn_subject_clarification_recovers_pending_request(
    include_history: bool,
):
    agent, classifier, retrieval, sessions = service()
    first, second = await advance_to_subject_clarification(agent)

    pending = await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "subject-session"
    )
    assert pending is not None
    assert pending.state_version == 2
    assert pending.request.primary_intent == PrimaryIntent.REPORT_GENERATION
    assert pending.request.missing_slots == ["semantic_ambiguity"]

    history = []
    if include_history:
        history = [
            HistoryMessage(role="user", content=REPORT_QUESTION, message_id="message-1"),
            HistoryMessage(role="assistant", content=first.answer, message_id="reply-1"),
            HistoryMessage(role="user", content="今年", message_id="message-2"),
            HistoryMessage(role="assistant", content=second.answer, message_id="reply-2"),
        ]
    third = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="subject-session",
            message_id="message-3",
            question=SUBJECT_REPLY,
            history=history,
        ),
        IDENTITY,
    )

    assert third.status == "COMPLETED"
    assert third.intent == PrimaryIntent.REPORT_GENERATION
    assert len(retrieval.requests) == 2
    assert SUBJECT_REPLY in retrieval.requests[-1].rewritten_question
    assert SUBJECT_REPLY not in classifier.model_path_questions
    assert await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "subject-session"
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected_intent", "expected_status"),
    [
        ("你好", PrimaryIntent.CHAT, "COMPLETED"),
        ("取消", PrimaryIntent.REPORT_GENERATION, "CANCELLED"),
        ("换个问题，查询本月销售额", PrimaryIntent.METRIC_QUERY, "COMPLETED"),
        (
            "查询昨天订单明细，显示订单号和金额",
            PrimaryIntent.DETAIL_QUERY,
            "COMPLETED",
        ),
    ],
)
async def test_semantic_clarification_keeps_explicit_interrupt_semantics(
    question: str, expected_intent: PrimaryIntent, expected_status: str
):
    agent, _, _, sessions = service()
    await advance_to_subject_clarification(agent)

    response = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="subject-session",
            message_id="message-3",
            question=question,
        ),
        IDENTITY,
    )

    assert response.intent == expected_intent
    assert response.status == expected_status
    assert await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "subject-session"
    ) is None
