import asyncio
from datetime import timedelta

import pytest

from app.stores.long_memory import (
    InMemoryLongTermMemoryStore,
    InvalidMemoryTransitionError,
    MemoryCandidate,
    MemoryNotFoundError,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    utc_now,
)


def candidate(
    scope: MemoryScope,
    *,
    message_id: str,
    memory_key: str = "default_time_grain",
    value: str = "month",
) -> MemoryCandidate:
    return MemoryCandidate(
        scope=scope,
        memory_type=MemoryType.USER_PREFERENCE,
        memory_key=memory_key,
        summary=f"默认按{value}查看",
        value={"time_grain": value},
        confidence=0.95,
        source_session_id="conversation-1",
        source_message_id=message_id,
        created_by=scope.user_id,
    )


@pytest.mark.asyncio
async def test_candidate_confirm_and_soft_delete_lifecycle():
    store = InMemoryLongTermMemoryStore()
    assert await store.healthcheck() is True
    scope = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-a")

    proposed = await store.create_candidate(candidate(scope, message_id="message-1"))
    assert proposed.status == MemoryStatus.CANDIDATE
    assert await store.list_active(scope) == []

    active = await store.confirm(scope, proposed.memory_id, confirmed_by="user-a")
    assert active.status == MemoryStatus.ACTIVE
    assert active.confirmed_at is not None
    assert active.version == 2
    assert [item.memory_id for item in await store.list_active(scope)] == [active.memory_id]

    deleted = await store.soft_delete(scope, active.memory_id, deleted_by="user-a")
    assert deleted.status == MemoryStatus.DELETED
    assert deleted.deleted_at is not None
    assert await store.get(scope, active.memory_id) is None
    assert (await store.get(scope, active.memory_id, include_deleted=True)).status == MemoryStatus.DELETED


@pytest.mark.asyncio
async def test_all_operations_enforce_tenant_user_and_application_isolation():
    store = InMemoryLongTermMemoryStore()
    owner = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-a")
    other_tenant = MemoryScope(tenant_id="tenant-b", user_id="user-a", application_id="app-a")
    other_user = MemoryScope(tenant_id="tenant-a", user_id="user-b", application_id="app-a")
    other_app = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-b")
    proposed = await store.create_candidate(candidate(owner, message_id="message-1"))

    for wrong_scope in (other_tenant, other_user, other_app):
        assert await store.get(wrong_scope, proposed.memory_id) is None
        assert await store.list_memories(wrong_scope) == []
        with pytest.raises(MemoryNotFoundError):
            await store.confirm(wrong_scope, proposed.memory_id, confirmed_by="actor")
        with pytest.raises(MemoryNotFoundError):
            await store.soft_delete(wrong_scope, proposed.memory_id, deleted_by="actor")

    assert (await store.get(owner, proposed.memory_id)).status == MemoryStatus.CANDIDATE


@pytest.mark.asyncio
async def test_confirming_same_logical_key_supersedes_old_active_memory():
    store = InMemoryLongTermMemoryStore()
    scope = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-a")
    old = await store.create_candidate(candidate(scope, message_id="message-1", value="month"))
    await store.confirm(scope, old.memory_id, confirmed_by="user-a")
    new = await store.create_candidate(candidate(scope, message_id="message-2", value="week"))

    confirmed = await store.confirm(scope, new.memory_id, confirmed_by="user-a")
    old_after = await store.get(scope, old.memory_id)

    assert confirmed.status == MemoryStatus.ACTIVE
    assert old_after.status == MemoryStatus.SUPERSEDED
    assert old_after.superseded_by == new.memory_id
    assert [item.memory_id for item in await store.list_active(scope)] == [new.memory_id]


@pytest.mark.asyncio
async def test_concurrent_confirmations_leave_only_one_logical_key_active():
    store = InMemoryLongTermMemoryStore()
    scope = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-a")
    first = await store.create_candidate(candidate(scope, message_id="message-1", value="month"))
    second = await store.create_candidate(candidate(scope, message_id="message-2", value="week"))

    await asyncio.gather(
        store.confirm(scope, first.memory_id, confirmed_by="user-a"),
        store.confirm(scope, second.memory_id, confirmed_by="user-a"),
    )

    records = await store.list_memories(scope)
    assert sum(item.status == MemoryStatus.ACTIVE for item in records) == 1
    assert sum(item.status == MemoryStatus.SUPERSEDED for item in records) == 1


@pytest.mark.asyncio
async def test_expired_candidate_cannot_be_confirmed_and_active_list_honors_expiry():
    store = InMemoryLongTermMemoryStore()
    scope = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-a")
    now = utc_now()
    proposed = await store.create_candidate(
        candidate(scope, message_id="message-1").model_copy(
            update={"valid_from": now - timedelta(hours=2), "expires_at": now - timedelta(hours=1)}
        )
    )
    with pytest.raises(InvalidMemoryTransitionError):
        await store.confirm(scope, proposed.memory_id, confirmed_by="user-a")


@pytest.mark.asyncio
async def test_candidate_creation_is_idempotent_for_same_source_and_logical_key():
    store = InMemoryLongTermMemoryStore()
    scope = MemoryScope(tenant_id="tenant-a", user_id="user-a", application_id="app-a")
    payload = candidate(scope, message_id="same-message")

    first = await store.create_candidate(payload)
    retry = await store.create_candidate(payload)

    assert retry.memory_id == first.memory_id
    assert len(await store.list_memories(scope)) == 1

    changed_payload = payload.model_copy(
        update={"summary": "不同内容", "value": {"time_grain": "week"}}
    )
    with pytest.raises(InvalidMemoryTransitionError):
        await store.create_candidate(changed_payload)
