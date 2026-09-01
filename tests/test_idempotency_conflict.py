import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest, HistoryMessage, TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.main import create_app
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore, MessageIdReuseConflictError


def service(*, sessions: InMemorySessionStore | None = None) -> DataAnalysisOrchestrator:
    return DataAnalysisOrchestrator(
        settings=Settings(
            env="test",
            adapter_mode="mock",
            intent_model_enabled=False,
            long_term_memory_mode="disabled",
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=sessions or InMemorySessionStore(),
    )


def request(**updates) -> ChatRequest:
    payload = {
        "application_id": "app1",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "question": "你好",
        "semantic_model_id": 11,
        "business_domain_id": 22,
        "knowledge_base_names": ["sales-kb"],
        "history": [],
        "use_longterm_memory": False,
    }
    payload.update(updates)
    return ChatRequest(**payload)


@pytest.mark.asyncio
async def test_identical_retry_returns_exact_cached_response() -> None:
    agent = service()
    identity = TrustedIdentity(tenant_id="tenant-1", user_id="user-1", roles=["analyst"])
    chat = request()

    first = await agent.handle(chat, identity)
    retry = await agent.handle(chat.model_copy(deep=True), identity)

    assert retry == first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"question": "查询本月销售额"},
        {"semantic_model_id": 12},
        {"business_domain_id": 23},
        {"knowledge_base_names": ["finance-kb"]},
        {
            "history": [
                HistoryMessage(
                    role="user",
                    content="之前的问题",
                    message_id="history-1",
                    created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                )
            ]
        },
        {"use_longterm_memory": True},
    ],
)
async def test_same_message_id_with_changed_effective_payload_is_rejected(changed) -> None:
    agent = service()
    identity = TrustedIdentity(tenant_id="tenant-1", user_id="user-1", roles=["analyst"])
    await agent.handle(request(), identity)

    with pytest.raises(MessageIdReuseConflictError) as raised:
        await agent.handle(request(**changed), identity)

    assert raised.value.code == "MESSAGE_ID_REUSE_CONFLICT"
    assert "new message_id" in str(raised.value)


def test_fingerprint_covers_scope_fields_and_is_stable() -> None:
    identity = TrustedIdentity(
        tenant_id="tenant-1", user_id="user-1", roles=["viewer", "analyst", "viewer"]
    )
    reordered_roles = TrustedIdentity(
        tenant_id="tenant-1", user_id="user-1", roles=["analyst", "viewer"]
    )
    original = request()
    fingerprint = DataAnalysisOrchestrator._request_fingerprint(original, identity)

    assert fingerprint == DataAnalysisOrchestrator._request_fingerprint(
        original.model_copy(deep=True), reordered_roles
    )
    assert fingerprint != DataAnalysisOrchestrator._request_fingerprint(
        request(application_id="app2"), identity
    )
    assert fingerprint != DataAnalysisOrchestrator._request_fingerprint(
        request(conversation_id="conversation-2"), identity
    )


@pytest.mark.asyncio
async def test_concurrent_different_payloads_cannot_both_claim_one_message_id() -> None:
    agent = service()
    identity = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")

    results = await asyncio.gather(
        agent.handle(request(question="你好"), identity),
        agent.handle(request(question="查询本月销售额"), identity),
        return_exceptions=True,
    )

    assert sum(isinstance(item, MessageIdReuseConflictError) for item in results) == 1
    assert sum(not isinstance(item, Exception) for item in results) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_requests_execute_pipeline_only_once() -> None:
    agent = service()
    identity = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")
    original = agent._handle
    calls = 0

    async def slow_handle(chat, trusted_identity):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return await original(chat, trusted_identity)

    agent._handle = slow_handle
    first, second = await asyncio.gather(
        agent.handle(request(), identity),
        agent.handle(request(), identity),
    )
    assert calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_legacy_cache_without_fingerprint_fails_safe() -> None:
    sessions = InMemorySessionStore()
    agent = service(sessions=sessions)
    identity = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")
    chat = request()
    await agent.handle(chat, identity)

    # Simulate a response written by the release before request fingerprints.
    sessions._response_fingerprints.clear()

    with pytest.raises(MessageIdReuseConflictError) as raised:
        await agent.handle(chat, identity)

    assert "legacy cached request" in str(raised.value)


def test_idempotency_reservation_ttl_is_at_least_session_ttl() -> None:
    sessions = InMemorySessionStore(ttl_seconds=7200, response_ttl_seconds=600)
    assert sessions.response_ttl_seconds == 7200


def test_http_endpoint_maps_reuse_conflict_to_409_with_machine_code() -> None:
    settings = Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        long_term_memory_mode="disabled",
    )
    headers = {"X-Tenant-Id": "tenant-1", "X-User-Id": "user-1"}
    body = request().model_dump(mode="json")

    with TestClient(create_app(settings)) as client:
        first = client.post("/agent_chat", headers=headers, json=body)
        changed = {**body, "question": "这是同一 ID 下的另一条问题"}
        conflict = client.post("/agent_chat", headers=headers, json=changed)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "MESSAGE_ID_REUSE_CONFLICT"
    assert "new message_id" in conflict.json()["detail"]["message"]
