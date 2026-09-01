import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest, HistoryMessage, PrimaryIntent, TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


def service() -> DataAnalysisOrchestrator:
    return DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=False
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )


IDENTITY = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")


@pytest.mark.asyncio
async def test_clarification_echoes_normalized_date_and_only_asks_for_metric():
    response = await service().handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="date-clarification",
            message_id="message-1",
            question="分析2026年7月1号到30号的",
        ),
        IDENTITY,
    )

    assert response.status == "NEEDS_CLARIFICATION"
    assert response.missing_slots == ["metric"]
    assert response.clarification_questions == ["要查询或分析哪个指标？"]
    assert response.clarification_items[0].model_dump() == {
        "slot": "metric",
        "title": "指标",
        "question": "要查询或分析哪个指标？",
        "options": [],
        "multi_select": True,
        "allow_free_text": True,
    }
    assert response.understood_slots["time_range"] == {
        "start": "2026-07-01",
        "end_inclusive": "2026-07-30",
        "timezone": "Asia/Shanghai",
    }
    assert "时间范围=2026-07-01 至 2026-07-30（含首尾）" in response.answer
    assert response.clarification_round == 1


@pytest.mark.asyncio
async def test_natural_date_reply_completes_the_existing_metric_request():
    agent = service()
    first = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="metric-then-date",
            message_id="message-1",
            question="销售额是多少？",
        ),
        IDENTITY,
    )
    assert first.missing_slots == ["time_range"]
    assert first.understood_slots["metrics"] == ["销售额"]
    assert first.clarification_items[0].slot == "time_range"
    assert first.clarification_items[0].options == [
        "今天", "昨天", "本周", "上周", "本月", "上月"
    ]

    second = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="metric-then-date",
            message_id="message-2",
            question="2026年7月1号到30号",
        ),
        IDENTITY,
    )

    assert second.status == "COMPLETED"
    assert second.intent == PrimaryIntent.METRIC_QUERY
    assert "metric_id=metric.sales_amount" in second.answer
    assert "None" not in second.answer


@pytest.mark.asyncio
async def test_explicit_new_chat_interrupts_an_unrelated_pending_request():
    agent = service()
    await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="interrupt-chat",
            message_id="message-1",
            question="查询销售额",
        ),
        IDENTITY,
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="interrupt-chat",
            message_id="message-2",
            question="你好",
        ),
        IDENTITY,
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.CHAT
    assert await agent.sessions.get_pending(
        "tenant-1", "user-1", "app-1", "interrupt-chat"
    ) is None


@pytest.mark.asyncio
async def test_explicit_detail_task_replaces_an_unrelated_pending_metric_query():
    agent = service()
    await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="interrupt-detail",
            message_id="message-1",
            question="查询销售额",
        ),
        IDENTITY,
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="interrupt-detail",
            message_id="message-2",
            question="查询昨天的订单明细，显示订单号和金额",
        ),
        IDENTITY,
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.DETAIL_QUERY


@pytest.mark.asyncio
async def test_short_slot_reply_does_not_replace_the_pending_request():
    agent = service()
    await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="slot-reply",
            message_id="message-1",
            question="查询销售额",
        ),
        IDENTITY,
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="slot-reply",
            message_id="message-2",
            question="本月",
        ),
        IDENTITY,
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.METRIC_QUERY


@pytest.mark.asyncio
async def test_comparison_object_names_close_pending_comparison():
    agent = service()
    first = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="comparison-objects",
            message_id="message-1",
            question="对比3家经销商3个月业绩增长率与合作时长",
        ),
        IDENTITY,
    )

    assert first.status == "NEEDS_CLARIFICATION"
    assert first.missing_slots == ["comparison_objects"]
    assert "具体经销商或供应商名称" in first.clarification_questions[0]

    second = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="comparison-objects",
            message_id="message-2",
            question="甲经销商、乙经销商、丙经销商",
        ),
        IDENTITY,
    )

    assert second.status != "NEEDS_CLARIFICATION"
    assert second.intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert await agent.sessions.get_pending(
        "tenant-1", "user-1", "app-1", "comparison-objects"
    ) is None


@pytest.mark.asyncio
async def test_explicit_new_task_wording_resets_a_pending_analysis():
    agent = service()
    await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="explicit-reset",
            message_id="message-1",
            question="分析数据下降原因",
        ),
        IDENTITY,
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="explicit-reset",
            message_id="message-2",
            question="换个问题，重新查询本月销售额",
        ),
        IDENTITY,
    )

    assert response.intent == PrimaryIntent.METRIC_QUERY
    assert response.status == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected_intent", "expected_status"),
    [
        ("你好", PrimaryIntent.CHAT, "COMPLETED"),
        ("查询昨天订单明细，显示订单号和金额", PrimaryIntent.DETAIL_QUERY, "COMPLETED"),
        ("取消", PrimaryIntent.METRIC_QUERY, "CANCELLED"),
    ],
)
async def test_history_cold_recovery_does_not_swallow_new_task_or_cancel(
    question: str, expected_intent: PrimaryIntent, expected_status: str
):
    response = await service().handle(
        ChatRequest(
            application_id="app-1",
            conversation_id=f"history-{expected_intent.value}-{expected_status}",
            message_id="message-3",
            question=question,
            history=[
                HistoryMessage(
                    role="user", content="查询销售额", message_id="message-1"
                ),
                HistoryMessage(
                    role="assistant",
                    content="还需要补充：要分析哪个时间范围？",
                    message_id="message-2",
                ),
            ],
        ),
        IDENTITY,
    )

    assert response.intent == expected_intent
    assert response.status == expected_status
