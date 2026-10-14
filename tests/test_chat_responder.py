import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest, PrimaryIntent, TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="t1", user_id="u1")


def test_everyday_food_turn_is_classified_as_chat():
    request = RuleBasedIntentClassifier().classify(
        "我想吃西瓜", IDENTITY, "casual-food"
    )

    assert request.primary_intent == PrimaryIntent.CHAT
    assert request.metrics == []
    assert request.missing_slots == []


@pytest.mark.parametrize(
    "question",
    (
        "我还想喝可乐",
        "那草莓和可乐可以一起吃吗",
        "苹果与牛奶能一起吃吗？",
    ),
)
def test_food_followups_remain_standalone_chat_turns(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "casual-food-followup"
    )
    assert request.primary_intent == PrimaryIntent.CHAT
    assert request.metrics == []
    assert request.filters == []


@pytest.mark.asyncio
async def test_chat_mode_switch_does_not_resurrect_previous_data_task():
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )
    conversation_id = "data-then-food-chat"
    first = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="27", conversation_id=conversation_id,
            message_id="m1", question="查询空心纤维血液透析器产品合作的经销商名单。",
        ),
        IDENTITY,
    )
    assert first.intent == PrimaryIntent.DETAIL_QUERY

    for index, question in enumerate(
        ("我想吃草莓", "我还想喝可乐", "那草莓和可乐可以一起吃吗"),
        start=2,
    ):
        response = await agent.handle(
            ChatRequest(semantic_model_id=81,
                application_id="27", conversation_id=conversation_id,
                message_id=f"m{index}", question=question,
            ),
            IDENTITY,
        )
        assert response.status == "COMPLETED"
        assert response.intent == PrimaryIntent.CHAT, (question, response.answer)
        assert "时间范围" not in response.answer
        assert "空心纤维" not in response.answer


def test_business_question_containing_want_is_not_swallowed_as_chat():
    request = RuleBasedIntentClassifier().classify(
        "我想查询最近一年销售额", IDENTITY, "business-want"
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY


@pytest.mark.asyncio
async def test_chat_intent_replies_fixed_greeting():
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )

    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="27",
            conversation_id="gentle-chat",
            message_id="m1",
            question="我想吃西瓜",
        ),
        IDENTITY,
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.CHAT
    assert response.answer == "你好！我是您的专属数据智能体，随时准备为您进行业务数据的查询与分析。"
