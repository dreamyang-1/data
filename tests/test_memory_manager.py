import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest, ChatRequest, MetricRef, PrimaryIntent, TrustedIdentity,
)
from app.intent.classifier import RuleBasedIntentClassifier
from app.services.memory_manager import MemoryManager
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore, SessionEventType
from app.stores.long_memory import (
    InMemoryLongTermMemoryStore,
    MemoryScope,
    MemoryStatus,
)


SCOPE = MemoryScope(tenant_id="tenant", user_id="user", application_id="app")


def request(metric: str = "销售额") -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="conversation", application_id="app",
        tenant_id="tenant", user_id="user",
        original_question=f"以后默认使用{metric}指标",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input=metric)],
    )


@pytest.mark.asyncio
async def test_plain_query_never_writes_long_term_memory():
    store = InMemoryLongTermMemoryStore()
    written = await MemoryManager(store).remember_explicit_defaults(
        request(), question="查询销售额", scope=SCOPE,
        session_id="conversation", message_id="message", actor="user",
    )
    assert written == []
    assert await store.list_memories(SCOPE) == []


@pytest.mark.asyncio
async def test_explicit_default_is_confirmed_and_idempotent():
    store = InMemoryLongTermMemoryStore()
    manager = MemoryManager(store)
    kwargs = dict(
        question="记住，以后默认使用销售额指标", scope=SCOPE,
        session_id="conversation", message_id="message", actor="user",
    )
    first = await manager.remember_explicit_defaults(request(), **kwargs)
    second = await manager.remember_explicit_defaults(request(), **kwargs)
    assert first[0].status == MemoryStatus.ACTIVE
    assert second[0].memory_id == first[0].memory_id
    assert len(await store.list_memories(SCOPE)) == 1


@pytest.mark.asyncio
async def test_new_explicit_value_supersedes_same_logical_key():
    store = InMemoryLongTermMemoryStore()
    manager = MemoryManager(store)
    await manager.remember_explicit_defaults(
        request("销售额"), question="以后默认使用销售额指标", scope=SCOPE,
        session_id="conversation", message_id="message-1", actor="user",
    )
    latest = await manager.remember_explicit_defaults(
        request("订单量"), question="改一下，今后默认使用订单量指标", scope=SCOPE,
        session_id="conversation", message_id="message-2", actor="user",
    )
    active = await store.list_active(SCOPE)
    all_records = await store.list_memories(SCOPE)
    assert [item.value for item in active] == [{"metric": "订单量"}]
    assert latest[0].status == MemoryStatus.ACTIVE
    assert {item.status for item in all_records} == {
        MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED,
    }


@pytest.mark.asyncio
async def test_memory_scope_isolated_by_application():
    store = InMemoryLongTermMemoryStore()
    await MemoryManager(store).remember_explicit_defaults(
        request(), question="以后默认使用销售额指标", scope=SCOPE,
        session_id="conversation", message_id="message", actor="user",
    )
    other = MemoryScope(tenant_id="tenant", user_id="user", application_id="other")
    assert await store.list_active(other) == []


@pytest.mark.asyncio
async def test_orchestrator_records_memory_write_and_recall_events():
    store = InMemoryLongTermMemoryStore()
    events = InMemorySessionEventStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        memories=store,
        event_store=events,
    )
    await agent.handle(
        ChatRequest(
            application_id="app", conversation_id="conversation", message_id="m1",
            question="记住，以后默认使用销售额指标，查询本月销售额",
            use_longterm_memory=True,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    recorded = await events.list_events(
        "tenant", "user", "app", "conversation", trace_id="m1"
    )
    event_types = [item.event_type for item in recorded]
    assert SessionEventType.MEMORY_WRITE in event_types
    assert SessionEventType.MEMORY_RECALL in event_types
    assert {"metric": "销售额"} in [
        item.value for item in await store.list_active(SCOPE)
    ]
    assert all(
        item.memory_key != "default_time_period"
        for item in await store.list_active(SCOPE)
    )
