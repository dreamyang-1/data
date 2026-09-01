import pytest

from app.domain.models import CanonicalAnalysisRequest, PendingState, PrimaryIntent
from app.stores import InMemorySessionStore, SessionConflictError


def state(version: int) -> PendingState:
    request = CanonicalAnalysisRequest(
        conversation_id="c1", application_id="app1", tenant_id="t1", user_id="u1",
        original_question="查询销售额", primary_intent=PrimaryIntent.METRIC_QUERY,
    )
    return PendingState(request=request, clarification_rounds=version, state_version=version)


@pytest.mark.asyncio
async def test_pending_state_uses_compare_and_set_version():
    store = InMemorySessionStore(ttl_seconds=60)
    await store.put_pending(state(1), expected_version=0)
    with pytest.raises(SessionConflictError):
        await store.put_pending(state(2), expected_version=0)
    await store.put_pending(state(2), expected_version=1)
    assert (await store.get_pending("t1", "u1", "app1", "c1")).state_version == 2
