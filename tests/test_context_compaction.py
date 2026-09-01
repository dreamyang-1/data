import pytest

from app.domain.models import (
    CanonicalAnalysisRequest,
    HistoryMessage,
    MetricRef,
    PrimaryIntent,
    TimeRange,
)
from app.services.context_compaction import ContextCompactor
from datetime import date


def request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="conversation",
        application_id="application",
        tenant_id="tenant",
        user_id="user",
        original_question="查询销售额",
        rewritten_question="查询上海本月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额", canonical_name="含税销售总额")],
        dimensions=["经销商"],
        filters=[{"field": "地区", "operator": "=", "value": "上海"}],
        time_range=TimeRange(
            start=date(2026, 8, 1), end_exclusive=date(2026, 9, 1)
        ),
    )


def test_short_history_is_not_compacted():
    history = [HistoryMessage(role="user", content="查询销售额")]
    assert ContextCompactor().compact(history, request=request()) is None


def test_compaction_preserves_structured_request_and_user_correction():
    history = [HistoryMessage(role="user", content="查询订单量")]
    history.append(HistoryMessage(role="user", content="不是订单量，改成销售额"))
    history.extend(
        HistoryMessage(role="assistant" if index % 2 else "user", content=f"消息 {index}")
        for index in range(30)
    )

    summary = ContextCompactor().compact(history, request=request())

    assert summary is not None
    assert summary.active_goal == "查询上海本月销售额"
    assert summary.confirmed_metrics == ["含税销售总额"]
    assert summary.confirmed_dimensions == ["经销商"]
    assert summary.confirmed_filters[0]["value"] == "上海"
    assert summary.confirmed_time_range["start"] == "2026-08-01"
    assert any("改成销售额" in item for item in summary.user_corrections)
    assert summary.compacted_message_count == len(history) - 12


def test_only_explicit_assistant_conclusions_are_retained():
    history = [
        HistoryMessage(role="assistant", content="整体销售额下降，12月出现恢复。"),
        HistoryMessage(role="assistant", content="欢迎使用数据智能体。"),
    ]
    history.extend(HistoryMessage(role="user", content=f"继续 {i}") for i in range(25))

    summary = ContextCompactor().compact(history, request=request())

    assert summary is not None
    assert summary.key_conclusions == ["整体销售额下降，12月出现恢复。"]
    assert all("欢迎使用" not in item for item in summary.key_conclusions)


def test_resolved_request_drops_stale_clarification():
    history = [
        HistoryMessage(role="assistant", content="请补充时间范围。"),
        *[HistoryMessage(role="user", content=f"消息 {i}") for i in range(25)],
    ]

    summary = ContextCompactor().compact(history, request=request())

    assert summary is not None
    assert summary.unresolved_items == []


def test_retained_history_is_recent_and_bounded():
    history = [HistoryMessage(role="user", content=str(i)) for i in range(40)]
    retained = ContextCompactor(retained_recent_messages=10).retained_history(history)
    assert [item.content for item in retained] == [str(i) for i in range(30, 40)]
