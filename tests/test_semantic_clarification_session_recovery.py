"""Session recovery around semantic clarification under the current contract.

The governed clarification contract (``app/services/clarification_policy.py``)
only allows ASK for a semantic ambiguity when the user has at least two real
options; empty candidates are a system binding failure and must end safely.
The rule classifier also supplies a controlled default time range for report
requests, so a first report turn no longer has a missing ``time_range`` slot.
These fixtures therefore rebuild the legally clarifiable scenarios instead of
re-asserting the obsolete shapes.
"""
import json

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterBundle, AdapterError
from app.adapters.mock import MockDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    ChatRequest,
    HistoryMessage,
    PendingState,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")
REPORT_QUESTION = "我在上海卖外周插管中心静脉导管，给我生成分析报告"
SUBJECT_REPLY = "外周插管中心静脉导管（成人型）"
SUBJECT_ALTERNATIVE = "外周插管中心静脉导管（儿童型）"
DATE_ANSWER = "2025年10月17日至2025年12月30日"

USER_RESOLVABLE_CANDIDATES = [
    {
        "label": SUBJECT_REPLY,
        "canonical_name": SUBJECT_REPLY,
        "semantic_id": "product-adult-cvc",
    },
    {
        "label": SUBJECT_ALTERNATIVE,
        "canonical_name": SUBJECT_ALTERNATIVE,
        "semantic_id": "product-pediatric-cvc",
    },
]


class ModelPathRecordingClassifier:
    """Record every asynchronous intent-model call without changing decisions."""

    def __init__(self) -> None:
        self.rules = RuleBasedIntentClassifier()
        self.model_path_questions: list[str] = []

    async def classify(self, question, identity, conversation_id):
        self.model_path_questions.append(question)
        return self.rules.classify(question, identity, conversation_id)

    def merge_clarification(self, previous, answer):
        return self.rules.merge_clarification(previous, answer)


class SubjectAmbiguousThenSuccessfulRetrieval:
    """Raise the governed subject ambiguity once, then serve the retry."""

    def __init__(self, candidates) -> None:
        self.requests = []
        self.candidates = candidates
        self.delegate = MockDataRetrievalAdapter()

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if len(self.requests) == 1:
            ambiguity = [{
                "type": "subject",
                "question": "请确认具体商品。",
                "candidates": self.candidates,
            }]
            raise AdapterError(
                "ASL_AMBIGUOUS",
                json.dumps(ambiguity, ensure_ascii=False),
                details=ambiguity,
            )
        return await self.delegate.query(request, identity, **kwargs)


class SuccessfulRetrieval:
    def __init__(self) -> None:
        self.requests = []
        self.delegate = MockDataRetrievalAdapter()

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        return await self.delegate.query(request, identity, **kwargs)


def service(retrieval):
    defaults = build_mock_adapters()
    classifier = ModelPathRecordingClassifier()
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


def chat(message_id: str, question: str, conversation_id: str, history=None):
    return ChatRequest(
        semantic_model_id=81,
        application_id="app-1",
        conversation_id=conversation_id,
        message_id=message_id,
        question=question,
        history=history or [],
    )


async def advance_to_subject_clarification(agent):
    """The report turn now reaches retrieval directly (default time range).

    The governed two-option subject ambiguity is what legally creates the
    pending clarification state.
    """
    first = await agent.handle(
        chat("message-1", REPORT_QUESTION, "subject-session"),
        IDENTITY,
    )
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.intent == PrimaryIntent.REPORT_GENERATION
    assert first.missing_slots == ["semantic_ambiguity"]
    return first


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True])
async def test_user_choice_recovers_pending_report_request(include_history: bool):
    retrieval = SubjectAmbiguousThenSuccessfulRetrieval(
        USER_RESOLVABLE_CANDIDATES
    )
    agent, classifier, retrieval, sessions = service(retrieval)
    first = await advance_to_subject_clarification(agent)
    item = first.clarification_items[0]
    assert item.slot == "semantic_ambiguity"
    assert item.options == [SUBJECT_REPLY, SUBJECT_ALTERNATIVE]

    pending = await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "subject-session"
    )
    assert pending is not None
    assert pending.state_version == 1
    assert pending.request.primary_intent == PrimaryIntent.REPORT_GENERATION
    assert pending.request.missing_slots == ["semantic_ambiguity"]
    ambiguity = pending.request.semantic_ambiguities[0]
    assert ambiguity.candidates == [SUBJECT_REPLY, SUBJECT_ALTERNATIVE]

    history = []
    if include_history:
        history = [
            HistoryMessage(
                role="user", content=REPORT_QUESTION, message_id="message-1"
            ),
            HistoryMessage(
                role="assistant", content=first.answer, message_id="reply-1"
            ),
        ]
    second = await agent.handle(
        chat("message-2", SUBJECT_REPLY, "subject-session", history=history),
        IDENTITY,
    )

    # The user's option selection must restore the original report task, not
    # start a new one, and must clear the pending clarification state.
    assert second.status == "COMPLETED"
    assert second.intent == PrimaryIntent.REPORT_GENERATION
    assert len(retrieval.requests) == 2
    assert retrieval.requests[-1].entity == SUBJECT_REPLY
    assert SUBJECT_REPLY not in classifier.model_path_questions
    assert await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "subject-session"
    ) is None


@pytest.mark.asyncio
async def test_closed_form_date_answer_merges_into_pending_request():
    retrieval = SuccessfulRetrieval()
    agent, classifier, retrieval, sessions = service(retrieval)
    # A governed upstream (for example the intent model) can leave the time
    # slot genuinely missing even though the rule default fills report turns.
    # Seed that exact pending shape instead of relying on the obsolete default.
    seeded = classifier.rules.classify(
        REPORT_QUESTION, IDENTITY, "report-time-session"
    )
    seeded.semantic_model_id = 81
    seeded.application_id = "app-1"
    seeded.time_range = None
    seeded.missing_slots = ["time_range"]
    seeded.assumptions = [
        assumption for assumption in seeded.assumptions
        if not assumption.startswith("DEFAULT_TIME_RANGE")
    ]
    await sessions.put_pending(
        PendingState(request=seeded, clarification_rounds=1, state_version=1),
        expected_version=0,
    )

    response = await agent.handle(
        chat("message-2", DATE_ANSWER, "report-time-session"),
        IDENTITY,
    )

    # The closed-form date resolved the existing report slot deterministically
    # and was never reclassified through the asynchronous intent model path.
    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.REPORT_GENERATION
    assert DATE_ANSWER not in classifier.model_path_questions
    assert len(retrieval.requests) == 1
    submitted = retrieval.requests[0]
    assert submitted.time_range is not None
    assert submitted.time_range.start.isoformat() == "2025-10-17"
    assert submitted.time_range.end_exclusive.isoformat() == "2025-12-31"
    assert {item["value"] for item in submitted.filters} == {
        "上海市", "外周插管中心静脉导管",
    }
    assert await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "report-time-session"
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected_intent", "expected_status"),
    [
        ("你好", PrimaryIntent.CHAT, "COMPLETED"),
        ("取消", PrimaryIntent.REPORT_GENERATION, "CANCELLED"),
        ("换个问题，查询本月销售额", PrimaryIntent.METRIC_QUERY, "COMPLETED"),
        (
            # Under the turn-admission gate only a self-contained detail
            # request interrupts; the dependent wording below would be held by
            # the UNBOUND pending guard instead of replacing the task.
            "帮我查询昨天订单明细，需要订单号和金额",
            PrimaryIntent.DETAIL_QUERY,
            "COMPLETED",
        ),
    ],
)
async def test_semantic_clarification_keeps_explicit_interrupt_semantics(
    question: str, expected_intent: PrimaryIntent, expected_status: str
):
    retrieval = SubjectAmbiguousThenSuccessfulRetrieval(
        USER_RESOLVABLE_CANDIDATES
    )
    agent, _, _, sessions = service(retrieval)
    await advance_to_subject_clarification(agent)

    response = await agent.handle(
        chat("message-2", question, "subject-session"),
        IDENTITY,
    )

    assert response.intent == expected_intent
    assert response.status == expected_status
    assert await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "subject-session"
    ) is None


@pytest.mark.asyncio
async def test_empty_candidate_ambiguity_is_a_system_failure_not_a_question():
    # Candidate-less ambiguity is a binding failure. The current contract must
    # suppress any user-facing question and end safely.
    retrieval = SubjectAmbiguousThenSuccessfulRetrieval([])
    agent, _, retrieval, sessions = service(retrieval)

    response = await agent.handle(
        chat("message-1", REPORT_QUESTION, "empty-candidate-session"),
        IDENTITY,
    )

    assert response.status == "SAFE_FALLBACK"
    assert response.intent == PrimaryIntent.REPORT_GENERATION
    traces = response.clarification_decision_traces
    assert traces
    assert all(trace.decision != "ASK" for trace in traces)
    assert any(trace.reason_type == "SYSTEM_FAILURE" for trace in traces)
    assert len(retrieval.requests) == 1
    assert await sessions.get_pending(
        "tenant-1", "user-1", "app-1", "empty-candidate-session"
    ) is None
