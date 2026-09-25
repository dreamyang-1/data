"""ISSUE-20260918-032000: "换成…去年同期…" must shift the previous window -1 year.

The deterministic context resolver must never re-execute the previous window
unchanged and must never reinterpret the wording as a natural year.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.domain.models import (
    CanonicalAnalysisRequest,
    MetricRef,
    PrimaryIntent,
    TimeRange,
)
from app.semantic_v2.context_question import (
    _replace_time,
    _replace_yoy_window,
    build_context_question,
)
from app.semantic_v2.models import ContextQuestionState, ContextQuestionTime

from test_v2_authorized_catalog_bridge import IDENTITY, provider, request
from test_v2_context_v1_execution_bridge import (
    handler,
    install_fallback_resolution,
    query_response,
)
from test_v2_limited_scalar_deployment import DeploymentRedis
from test_v2_persisted_scalar_api import NOW


def _frame_with_calendar_year() -> ContextQuestionState:
    return ContextQuestionState(
        original_question="查询2025年一次性使用静脉留置针的含税销售额。",
        execution_question="查询2025年一次性使用静脉留置针的含税销售额。",
        metrics=["含税销售额"],
        time=ContextQuestionTime(
            surface="2025年",
            start=date(2025, 1, 1),
            end_exclusive=date(2026, 1, 1),
            evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
        ),
        source_message_id="yoy-unit-first",
    )


def _frame_with_rolling_default_window() -> ContextQuestionState:
    return ContextQuestionState(
        original_question="查询一次性使用静脉留置针的含税销售额。",
        execution_question="查询一次性使用静脉留置针的含税销售额。",
        metrics=["含税销售额"],
        default_time_window=ContextQuestionTime(
            surface="2025年9月18日至2026年9月17日",
            start=date(2025, 9, 18),
            end_exclusive=date(2026, 9, 18),
            evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
        ),
        source_message_id="yoy-default-first",
    )


@pytest.mark.parametrize("question", [
    "上一轮结果换成去年同期的口径，重新算一次。",
    "上一轮结果换成去年同期，重新算一次。",
    "换成去年同期",
    "换成去年同期。",
    "把时间换成去年同期？",
    "改成去年同期，重新算一次。",
    "将上一轮数据改为去年同期口径",
])
def test_yoy_shift_replaces_a_calendar_year_window(question):
    updated, evidence = _replace_yoy_window(
        _frame_with_calendar_year(), question, NOW
    )

    assert updated is not None
    assert updated.execution_question == (
        "查询2024年一次性使用静脉留置针的含税销售额。"
    )
    assert updated.time is not None
    assert updated.time.start == date(2024, 1, 1)
    assert updated.time.end_exclusive == date(2025, 1, 1)
    assert updated.default_time_window is None
    assert evidence == question.strip("？?。.").strip()


@pytest.mark.parametrize("question", [
    "上一轮结果换成去年同期的口径，重新算一次。",
    "换成去年同期",
])
def test_yoy_shift_moves_the_rolling_default_window_back_one_year(question):
    updated, _evidence = _replace_yoy_window(
        _frame_with_rolling_default_window(), question, NOW
    )

    assert updated is not None
    assert updated.execution_question == (
        "查询2024年9月18日至2025年9月17日一次性使用静脉留置针的含税销售额。"
    )
    assert updated.time.start == date(2024, 9, 18)
    assert updated.time.end_exclusive == date(2025, 9, 18)


def test_yoy_shift_clamps_february_29_windows():
    frame = ContextQuestionState(
        original_question="查询2024年2月的订单笔数。",
        execution_question="查询2024年2月1日至2024年2月29日的订单笔数。",
        metrics=["订单笔数"],
        time=ContextQuestionTime(
            surface="2024年2月1日至2024年2月29日",
            start=date(2024, 2, 1),
            end_exclusive=date(2024, 3, 1),
            evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
        ),
        source_message_id="yoy-leap-first",
    )

    updated, _evidence = _replace_yoy_window(frame, "换成去年同期", NOW)

    assert updated is not None
    assert updated.time.start == date(2023, 2, 1)
    assert updated.time.end_exclusive == date(2023, 3, 1)


@pytest.mark.parametrize("question", [
    "上一轮结果换成去年同期的口径，重新算一次。",
    "换成去年同期",
    "与去年同期比较",
    "换今年",
    "去年同期呢",
])
def test_yoy_shift_fails_closed_without_a_previous_window(question):
    frame = ContextQuestionState(
        original_question="查询一次性使用静脉留置针的含税销售额。",
        execution_question="查询一次性使用静脉留置针的含税销售额。",
        metrics=["含税销售额"],
        source_message_id="yoy-empty-first",
    )

    assert _replace_yoy_window(frame, question, NOW) is None


@pytest.mark.parametrize("question", [
    "与去年同期比较",
    "换今年",
    "去年同期呢",
    "换成最近一个月",
])
def test_yoy_shift_leaves_other_time_wording_to_existing_paths(question):
    assert _replace_yoy_window(
        _frame_with_calendar_year(), question, NOW
    ) is None
    # The pre-existing closed-form time replacement stays intact for its own
    # sentence family.
    if question == "换今年":
        replaced = _replace_time(_frame_with_calendar_year(), question, NOW)
        assert replaced is not None
        assert replaced[0].time.surface == "2026年"


def test_build_context_question_records_the_executed_default_window():
    chat = request(
        question="查询一次性使用静脉留置针的含税销售额。",
        message_id="yoy-seed-first",
        conversation_id="yoy-seed",
    )
    v1_request = CanonicalAnalysisRequest(
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        original_question=chat.question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=chat.semantic_model_id,
        metrics=[MetricRef(input="含税销售额")],
        time_range=TimeRange(
            start=date(2025, 9, 18), end_exclusive=date(2026, 9, 18)
        ),
    )

    frame = build_context_question(
        chat=chat, parse=None, v1_request=v1_request, catalog_version="v1"
    )

    assert frame.time is None
    assert frame.default_time_window is not None
    assert frame.default_time_window.start == date(2025, 9, 18)
    assert frame.default_time_window.end_exclusive == date(2026, 9, 18)
    assert frame.default_time_window.surface == "2025年9月18日至2026年9月17日"


def test_build_context_question_keeps_explicit_surface_as_display_time():
    chat = request(
        question="查询2025年一次性使用静脉留置针的含税销售额。",
        message_id="yoy-seed-explicit",
        conversation_id="yoy-seed",
    )
    parse = SimpleNamespace(mentions=[
        SimpleNamespace(surface="2025年", candidate_roles=["TIME_RANGE"]),
    ])
    v1_request = CanonicalAnalysisRequest(
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        original_question=chat.question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=chat.semantic_model_id,
        metrics=[MetricRef(input="含税销售额")],
        time_range=TimeRange(
            start=date(2025, 1, 1), end_exclusive=date(2026, 1, 1)
        ),
    )

    frame = build_context_question(
        chat=chat, parse=parse, v1_request=v1_request, catalog_version="v1"
    )

    assert frame.time is not None
    assert frame.time.surface == "2025年"
    assert frame.default_time_window is None


@pytest.mark.asyncio
async def test_yoy_followup_executes_the_shifted_window_not_the_old_one(provider):
    redis = DeploymentRedis()
    executions = []
    executed_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        executed_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=executed_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.METRIC_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            source_dataset_id=executed_responses[-1].dataset_id,
            metrics=[MetricRef(input="含税销售额")],
            time_range=TimeRange(
                start=date(2025, 9, 18), end_exclusive=date(2026, 9, 18)
            ),
        )

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    first = request(
        question="查询一次性使用静脉留置针的含税销售额。",
        message_id="yoy-bridge-first",
        conversation_id="yoy-bridge",
    )
    await bridge.handle(first, IDENTITY)
    del bridge._resolve

    followup = request(
        question="上一轮结果换成去年同期的口径，重新算一次。",
        message_id="yoy-bridge-followup",
        conversation_id=first.conversation_id,
    )
    result = await bridge.handle(followup, IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == (
        "查询2024年9月18日至2025年9月17日一次性使用静脉留置针的含税销售额。"
    )
    assert executions[-1].question != first.question
    snapshot = await bridge.store.load(followup, IDENTITY)
    from app.semantic_v2.state_machine import ConversationState

    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert frame.time.start == date(2024, 9, 18)
    assert frame.time.end_exclusive == date(2025, 9, 18)
    assert frame.default_time_window is None
    assert frame.last_edit.slot == "time_spec"
    assert frame.last_edit.operation == "REPLACE"
