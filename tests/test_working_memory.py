import pytest
import fakeredis.aioredis

from app.domain.models import CanonicalAnalysisRequest, MetricRef, PrimaryIntent
from app.domain.models import ChatRequest, TrustedIdentity
from app.adapters import build_mock_adapters
from app.config import Settings
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.working_memory import recalls_prior_task, select_recalled_task_frame
from app.stores import InMemorySessionStore, RedisSessionStore


def frame(question: str, metric: str, *, entity: str | None = None):
    return CanonicalAnalysisRequest(
        conversation_id="conversation",
        application_id="app",
        tenant_id="tenant",
        user_id="user",
        original_question=question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input=metric)],
        entity=entity,
        semantic_model_id=6,
    )


def test_recall_requires_explicit_branch_reference():
    frames = [frame("查询库存金额", "库存金额")]
    assert select_recalled_task_frame("查询销售额", frames) is None
    assert recalls_prior_task("回到之前的库存金额") is True


def test_recall_selects_matching_older_task_instead_of_latest():
    latest = frame("查询订单量", "订单量")
    older = frame("查询库存金额", "库存金额", entity="商品")
    selected = select_recalled_task_frame("回到之前的库存金额分析", [latest, older])
    assert selected is not None
    assert selected.metrics[0].input == "库存金额"


def test_ambiguous_recall_does_not_guess_between_branches():
    frames = [frame("查询上海销售额", "销售额"), frame("查询北京销售额", "销售额")]
    assert select_recalled_task_frame("回到之前的销售额", frames) is None


def test_duplicate_snapshots_do_not_make_recall_ambiguous():
    inventory = frame("查询库存金额", "库存金额")
    selected = select_recalled_task_frame(
        "回到之前的库存金额", [inventory, inventory.model_copy(deep=True)]
    )
    assert selected is not None
    assert selected.metrics[0].input == "库存金额"


@pytest.mark.asyncio
async def test_session_keeps_bounded_deduplicated_task_recall():
    store = InMemorySessionStore(ttl_seconds=60)
    first = frame("查询库存金额", "库存金额")
    await store.put_task_frame(first)
    await store.put_task_frame(first.model_copy(deep=True))
    for index in range(15):
        await store.put_task_frame(frame(f"查询指标{index}", f"指标{index}"))

    recalled = await store.get_recent_task_frames(
        "tenant", "user", "app", "conversation", limit=20
    )
    assert len(recalled) == 12
    assert recalled[0].metrics[0].input == "指标14"
    assert all(item.asl_template is None for item in recalled)


@pytest.mark.asyncio
async def test_redis_task_recall_roundtrip_is_scope_isolated():
    store = RedisSessionStore(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=60,
        prefix="test-memory",
    )
    await store.put_task_frame(frame("查询库存金额", "库存金额"))

    recalled = await store.get_recent_task_frames(
        "tenant", "user", "app", "conversation", limit=12
    )
    assert [item.metrics[0].input for item in recalled] == ["库存金额"]
    assert await store.get_recent_task_frames(
        "other-tenant", "user", "app", "conversation", limit=12
    ) == []


@pytest.mark.asyncio
async def test_orchestrator_can_return_to_an_older_task_branch():
    sessions = InMemorySessionStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=sessions,
    )
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")
    questions = [
        "查询2026年7月订单量",
        "查询2026年7月销售额",
        "回到之前的订单量，按月统计",
    ]
    for index, question in enumerate(questions, 1):
        response = await agent.handle(
            ChatRequest(semantic_model_id=81,
                application_id="app",
                conversation_id="branch-recall",
                message_id=f"m{index}",
                question=question,
            ),
            identity,
        )
        assert response.status != "NEEDS_CLARIFICATION"

    current = await sessions.get_task_frame(
        "tenant", "user", "app", "branch-recall"
    )
    assert current is not None
    assert [item.input for item in current.metrics] == ["订单量"]
    assert "STRUCTURED_TASK_RECALL" in current.assumptions
