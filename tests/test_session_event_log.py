import fakeredis.aioredis
import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity
from app.intent.classifier import RuleBasedIntentClassifier
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.stores.events import (
    EventPayloadSanitizer,
    InMemorySessionEventStore,
    RedisSessionEventStore,
    SessionEvent,
    SessionEventType,
)


def event(index=1, **updates):
    value = SessionEvent(
        session_id="conversation",
        user_id="user",
        tenant_id="tenant",
        application_id="app",
        message_id=f"m{index}",
        event_type=SessionEventType.USER_QUERY,
        trace_id=f"trace-{index}",
        payload={"question": f"query {index}"},
    )
    return value.model_copy(update=updates)


@pytest.mark.asyncio
async def test_in_memory_event_store_is_append_only_scoped_and_bounded():
    store = InMemorySessionEventStore(max_events_per_session=2)
    await store.append(event(1))
    await store.append(event(2))
    await store.append(event(3))

    values = await store.list_events("tenant", "user", "app", "conversation")
    assert [item.message_id for item in values] == ["m2", "m3"]
    assert await store.list_events("tenant", "another", "app", "conversation") == []
    assert [item.message_id for item in await store.list_events(
        "tenant", "user", "app", "conversation", trace_id="trace-3"
    )] == ["m3"]


def test_event_payload_sanitizer_redacts_secrets_and_omits_raw_rows():
    value = EventPayloadSanitizer.sanitize({
        "authorization": "Bearer secret",
        "nested": {"api_key": "secret"},
        "rows": [{"customer": "private"}] * 100,
    })

    assert value["authorization"] == "<redacted>"
    assert value["nested"]["api_key"] == "<redacted>"
    assert value["rows"] == {"item_count": 100, "content_omitted": True}


@pytest.mark.asyncio
async def test_redis_event_store_uses_bounded_list_and_ttl():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisSessionEventStore(
        redis, prefix="test", ttl_seconds=60, max_events_per_session=2
    )
    await store.append(event(1))
    await store.append(event(2))
    await store.append(event(3))

    values = await store.list_events("tenant", "user", "app", "conversation")
    assert [item.message_id for item in values] == ["m2", "m3"]
    key = store._key("tenant", "user", "app", "conversation")
    assert await redis.ttl(key) > 0


@pytest.mark.asyncio
async def test_orchestrator_records_replayable_request_intent_and_final_events():
    events = InMemorySessionEventStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        event_store=events,
    )
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app", conversation_id="conversation",
            message_id="m1", question="你好",
        ),
        identity,
    )

    assert response.status == "COMPLETED"
    recorded = await events.list_events(
        "tenant", "user", "app", "conversation", trace_id="m1"
    )
    assert [item.event_type for item in recorded] == [
        SessionEventType.USER_QUERY,
        SessionEventType.INTENT_RESULT,
        SessionEventType.FINAL_INSIGHT,
        SessionEventType.TRACE_SUMMARY,
    ]
    assert recorded[-2].payload["status"] == "COMPLETED"


class FailingEventStore:
    async def append(self, _event):
        raise RuntimeError("event backend unavailable")


@pytest.mark.asyncio
async def test_event_backend_failure_never_breaks_business_response():
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        event_store=FailingEventStore(),
    )
    response = await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app", conversation_id="conversation",
            message_id="m1", question="你好",
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "COMPLETED"
