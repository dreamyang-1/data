import pytest

from app.analysis import AnalysisEngine, AnalysisError
from app.domain.models import (
    CanonicalAnalysisRequest,
    KnowledgeContext,
    MetricRef,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user")


def forecast_request(*, horizon=1, granularity="month") -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="forecast",
        tenant_id="tenant",
        user_id="user",
        original_question="基于过去12个月预测下个月销售额",
        primary_intent=PrimaryIntent.FORECAST_ANALYSIS,
        metrics=[MetricRef(input="销售额", canonical_name="销售额")],
        forecast_horizon_periods=horizon,
        forecast_granularity=granularity,
        forecast_history_provided=True,
    )


def requirement_codes(error: AnalysisError) -> set[str]:
    return {item.code for item in error.requirements}


def test_question_preflight_requests_history_window_before_query() -> None:
    request = RuleBasedIntentClassifier().classify(
        "预测下个月销售额", IDENTITY, "conversation"
    )
    assert request.forecast_horizon_periods == 1
    assert request.forecast_granularity == "month"
    assert request.missing_slots == ["forecast_history_range"]


def test_question_preflight_requests_target_and_history_when_both_missing() -> None:
    request = RuleBasedIntentClassifier().classify(
        "预测销售额", IDENTITY, "conversation"
    )
    assert request.missing_slots == ["forecast_horizon", "forecast_history_range"]


def test_question_preflight_accepts_explicit_history_and_target() -> None:
    request = RuleBasedIntentClassifier().classify(
        "基于过去12个月预测下个月销售额", IDENTITY, "conversation"
    )
    assert request.missing_slots == []


def test_question_preflight_accepts_absolute_target_month() -> None:
    request = RuleBasedIntentClassifier().classify(
        "基于2025年1月至2026年7月历史数据，预测2026年8月销售额",
        IDENTITY,
        "conversation",
    )
    assert request.forecast_horizon_periods == 1
    assert request.forecast_granularity == "month"
    assert request.missing_slots == []


def test_explicit_period_comparison_does_not_ask_for_comparison_type() -> None:
    request = RuleBasedIntentClassifier().classify(
        "对比2026年6月和2026年7月销售额", IDENTITY, "conversation"
    )
    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "指定时段对比"
    assert "comparison_type" not in request.missing_slots


def test_dataset_preflight_reports_exact_history_shortage() -> None:
    with pytest.raises(AnalysisError) as captured:
        AnalysisEngine().analyze(
            forecast_request(),
            ["月份", "销售额"],
            [{"月份": f"2026-{month:02d}", "销售额": month} for month in range(1, 5)],
            KnowledgeContext(query=""),
        )
    assert requirement_codes(captured.value) == {"forecast_minimum_history"}
    assert "至少补充2期" in captured.value.requirements[0].action


def test_dataset_preflight_rejects_irregular_time_axis() -> None:
    months = [1, 2, 3, 5, 6, 7]
    with pytest.raises(AnalysisError) as captured:
        AnalysisEngine().analyze(
            forecast_request(),
            ["月份", "销售额"],
            [{"月份": f"2026-{month:02d}", "销售额": month} for month in months],
            KnowledgeContext(query=""),
        )
    assert requirement_codes(captured.value) == {"forecast_regular_frequency"}


def test_dataset_preflight_rejects_target_data_grain_mismatch() -> None:
    with pytest.raises(AnalysisError) as captured:
        AnalysisEngine().analyze(
            forecast_request(granularity="week"),
            ["月份", "销售额"],
            [{"月份": f"2026-{month:02d}", "销售额": month} for month in range(1, 7)],
            KnowledgeContext(query=""),
        )
    assert requirement_codes(captured.value) == {"forecast_granularity_mismatch"}


def test_dataset_preflight_rejects_unvalidated_multi_step_model() -> None:
    with pytest.raises(AnalysisError) as captured:
        AnalysisEngine().analyze(
            forecast_request(horizon=3),
            ["月份", "销售额"],
            [{"月份": f"2026-{month:02d}", "销售额": month} for month in range(1, 7)],
            KnowledgeContext(query=""),
        )
    assert requirement_codes(captured.value) == {"multi_step_forecast_model"}


@pytest.mark.asyncio
async def test_orchestrator_returns_structured_requirements_instead_of_prediction() -> None:
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )
    response = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="forecast-readiness",
            message_id="message-1",
            question="基于过去12个月预测下个月销售额",
        ),
        IDENTITY,
    )
    assert response.status == "SAFE_FALLBACK"
    assert response.missing_slots == ["forecast_minimum_history"]
    assert [item.code for item in response.requirements] == [
        "forecast_minimum_history"
    ]
    assert "至少" in response.requirements[0].action
    assert "需要补充或处理" in response.answer


@pytest.mark.asyncio
async def test_orchestrator_returns_user_input_requirements_before_query() -> None:
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )
    response = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="forecast-question-readiness",
            message_id="message-1",
            question="预测下个月销售额",
        ),
        IDENTITY,
    )
    assert response.status == "NEEDS_CLARIFICATION"
    assert [item.code for item in response.requirements] == [
        "forecast_history_range"
    ]
    assert response.requirements[0].category == "USER_INPUT"
