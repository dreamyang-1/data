import pytest
from pydantic import ValidationError
from datetime import datetime

from app.domain.models import CanonicalAnalysisRequest, PendingState, PrimaryIntent
from app.stores import InMemorySessionStore
from app.stores.long_memory import MemoryCandidate, MemoryScope, MemoryType


def canonical_request(*, conversation_id: str = "conversation") -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id=conversation_id,
        application_id="app",
        tenant_id="tenant",
        user_id="user",
        original_question="查询销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        missing_slots=["time_range"],
    )


@pytest.mark.asyncio
async def test_pending_clear_uses_version_and_cannot_delete_newer_state() -> None:
    store = InMemorySessionStore()
    request = canonical_request()
    await store.put_pending(
        PendingState(request=request, clarification_rounds=1, state_version=1),
        expected_version=0,
    )
    await store.put_pending(
        PendingState(request=request, clarification_rounds=2, state_version=2),
        expected_version=1,
    )

    assert await store.clear_pending(
        "tenant", "user", "app", "conversation", expected_version=1
    ) is False
    current = await store.get_pending("tenant", "user", "app", "conversation")
    assert current is not None and current.state_version == 2
    assert await store.clear_pending(
        "tenant", "user", "app", "conversation", expected_version=2
    ) is True
    assert await store.get_pending("tenant", "user", "app", "conversation") is None


def candidate(**updates) -> MemoryCandidate:
    values = dict(
        scope=MemoryScope(tenant_id="tenant", user_id="user", application_id="app"),
        memory_type=MemoryType.USER_PREFERENCE,
        memory_key="default_metric",
        summary="默认查询销售额",
        value={"metric": "销售额"},
        confidence=0.95,
        source_session_id="conversation",
        source_message_id="message",
        created_by="user",
    )
    values.update(updates)
    return MemoryCandidate(**values)


@pytest.mark.parametrize(
    ("memory_type", "memory_key", "value"),
    [
        (MemoryType.DISPLAY_PREFERENCE, "default_metric", {"metric": "销售额"}),
        (MemoryType.USER_PREFERENCE, "metric_alias:gmv", {"alias": "GMV", "canonical_metric": "成交额"}),
        (MemoryType.USER_PREFERENCE, "default_filter:", {"filter_field": "地区", "filter_operator": "=", "filter_value": "华东"}),
    ],
)
def test_memory_type_and_key_must_match(memory_type, memory_key, value) -> None:
    with pytest.raises(ValidationError):
        candidate(memory_type=memory_type, memory_key=memory_key, value=value)


def test_memory_rejects_extra_or_nested_sensitive_fields() -> None:
    with pytest.raises(ValidationError):
        candidate(value={"metric": "销售额", "unexpected": "not governed"})
    with pytest.raises(ValidationError):
        candidate(value={"metric": "销售额", "metadata": {"sql": "select 1"}})


def test_memory_filter_operators_are_allowlisted() -> None:
    with pytest.raises(ValidationError):
        candidate(
            memory_type=MemoryType.DEFAULT_FILTER,
            memory_key="default_filter:region",
            value={
                "filter_field": "地区",
                "filter_operator": "raw_sql",
                "filter_value": "华东",
            },
        )


def test_valid_governed_memory_still_passes() -> None:
    item = candidate(
        memory_type=MemoryType.METRIC_ALIAS,
        memory_key="metric_alias:gmv",
        value={"alias": "GMV", "canonical_metric": "成交额"},
    )
    assert item.value["canonical_metric"] == "成交额"


def test_memory_rejects_non_finite_numbers_and_naive_expiry() -> None:
    with pytest.raises(ValidationError):
        candidate(
            memory_type=MemoryType.DEFAULT_FILTER,
            memory_key="default_filter:threshold",
            value={
                "filter_field": "金额",
                "filter_operator": ">",
                "filter_value": float("nan"),
            },
        )
    with pytest.raises(ValidationError):
        candidate(expires_at=datetime(2027, 1, 1))
