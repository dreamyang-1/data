"""An explicit historical reference cannot fall back to another task."""
import pytest

from app.adapters.mock import MockDataRetrievalAdapter
from app.adapters.base import AdapterError
from app.domain.models import CanonicalAnalysisRequest, MetricRef, PendingState, PrimaryIntent
from app.services.working_memory import requires_prior_task_resolution, select_recalled_task_frame
from test_semantic_choice_contract import IDENTITY, chat, service


class CapturingRetrieval:
    def __init__(self):
        self.requests = []
        self.delegate = MockDataRetrievalAdapter()

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        return await self.delegate.query(request, identity, **kwargs)


def frame(metric):
    return CanonicalAnalysisRequest(
        conversation_id="recall", tenant_id="tenant", user_id="user",
        original_question="查询" + metric, primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=81, metrics=[MetricRef(input=metric)],
    )


@pytest.mark.asyncio
async def test_recalled_task_keeps_its_region_and_period_over_latest_verified_task():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    for index, question in enumerate([
        "查询2026年7月订单量，地区上海", "查询2026年8月销售额，地区江苏",
        "回到之前的订单量",
    ]):
        result = await agent.handle(chat(question, f"message-{index}"), IDENTITY)
        assert result.status == "COMPLETED"
    original, latest, recalled = retrieval.requests
    assert original.filters == [{"field": "业务城市", "operator": "EQ", "value": "上海市"}]
    assert latest.filters != original.filters
    assert recalled.filters == original.filters
    assert recalled.time_range == original.time_range
    assert [m.input for m in recalled.metrics] == ["订单量"]


@pytest.mark.asyncio
async def test_ambiguous_historical_reference_cannot_execute_latest_task():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    for index, question in enumerate([
        "查询2026年7月销售额，地区上海", "查询2026年7月销售额，地区江苏", "查询2026年8月订单量，地区浙江",
    ]):
        result = await agent.handle(chat(question, f"message-{index}"), IDENTITY)
        assert result.status == "COMPLETED"
    count = len(retrieval.requests)
    result = await agent.handle(chat("回到之前的销售额", "message-recall"), IDENTITY)
    assert len(retrieval.requests) == count
    assert result.status == "SAFE_FALLBACK"
    assert not result.clarification_questions


def test_named_reference_is_stronger_than_recency_word():
    selected = select_recalled_task_frame("回到刚才的订单量", [frame("销售额"), frame("订单量")])
    assert selected is not None
    assert [m.input for m in selected.metrics] == ["订单量"]


@pytest.mark.parametrize("question", ["回到刚才的问题", "恢复上一个任务", "继续刚才的任务"])
def test_bare_latest_reference_still_selects_latest(question):
    selected = select_recalled_task_frame(question, [frame("销售额"), frame("订单量")])
    assert selected is not None and selected.metrics[0].input == "销售额"


@pytest.mark.parametrize("question", ["回到之前的库存金额", "回到刚才的库存金额"])
def test_unmatched_named_target_does_not_become_latest(question):
    assert select_recalled_task_frame(question, [frame("销售额"), frame("订单量")]) is None


def test_recency_breaks_tie_only_between_matching_named_tasks():
    older = frame("销售额")
    older.filters = [{"field": "地区", "operator": "EQ", "value": "上海"}]
    latest_matching = frame("销售额")
    latest_matching.filters = [{"field": "地区", "operator": "EQ", "value": "江苏"}]
    selected = select_recalled_task_frame("回到刚才的销售额", [frame("订单量"), latest_matching, older])
    assert selected is not None and selected.filters == latest_matching.filters


def test_following_edit_does_not_outscore_the_named_historical_target():
    selected = select_recalled_task_frame("回到之前的订单量，改成含税销售总额", [frame("含税销售总额"), frame("订单量")])
    assert selected is not None and selected.metrics[0].input == "订单量"


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", ["memory", "redis"])
async def test_four_turn_recall_and_followup_keep_recalled_region(store_kind):
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    if store_kind == "redis":
        import fakeredis.aioredis
        from app.stores import RedisSessionStore
        agent.sessions = RedisSessionStore(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_seconds=60, prefix="recall-test")
    for index, text in enumerate([
        "查询2026年7月订单量，地区上海", "查询2026年8月销售额，地区江苏",
        "回到刚才的订单量", "那2026年9月呢？",
    ]):
        # FakeRedis has no Lua EVAL; exercise real Redis frame persistence and
        # the full inner request path without the unrelated response-cache Lua.
        handle = agent._handle if store_kind == "redis" else agent.handle
        response = await handle(chat(text, f"turn-{index}"), IDENTITY)
        assert response.status == "COMPLETED"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["订单量"]
    assert retrieval.requests[-1].filters == retrieval.requests[0].filters
    assert retrieval.requests[-1].time_range.start.isoformat() == "2026-09-01"


@pytest.mark.parametrize("text", ["继续按月统计", "恢复地区限制", "接着按渠道分析", "把刚才结果导出成Excel"])
def test_generic_slot_changes_do_not_require_a_historical_branch(text):
    assert not requires_prior_task_resolution(text)


@pytest.mark.asyncio
async def test_unknown_historical_target_preserves_unrelated_pending():
    retrieval = PendingRetrieval()
    agent = service(retrieval)
    await agent.handle(chat("查询2026年7月订单量，地区上海"), IDENTITY)
    retrieval.armed = True
    response = await agent.handle(chat("查询2026年8月销售额，地区江苏", "message-2"), IDENTITY)
    assert response.status == "NEEDS_CLARIFICATION"
    before = await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation")
    retrieval.armed = False
    count = len(retrieval.requests)
    response = await agent.handle(chat("回到之前的库存金额", "message-3"), IDENTITY)
    assert response.status == "SAFE_FALLBACK"
    assert len(retrieval.requests) == count
    after = await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation")
    assert after == before


@pytest.mark.asyncio
async def test_pending_version_change_stops_historical_restore(monkeypatch):
    retrieval = PendingRetrieval()
    agent = service(retrieval)
    await agent.handle(chat("查询2026年7月订单量，地区上海"), IDENTITY)
    retrieval.armed = True
    response = await agent.handle(chat("查询2026年8月销售额，地区江苏", "message-2"), IDENTITY)
    assert response.status == "NEEDS_CLARIFICATION"
    stored = await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation")
    delegate = agent.sessions.clear_pending

    async def racing_clear(*args, **kwargs):
        updated = PendingState(request=stored.request.model_copy(deep=True), clarification_rounds=2, state_version=2)
        await agent.sessions.put_pending(updated, expected_version=1)
        return await delegate(*args, **kwargs)

    monkeypatch.setattr(agent.sessions, "clear_pending", racing_clear)
    retrieval.armed = False
    count = len(retrieval.requests)
    response = await agent.handle(chat("回到之前的订单量", "message-3"), IDENTITY)
    assert response.status == "SAFE_FALLBACK"
    assert len(retrieval.requests) == count
    surviving = await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation")
    assert surviving.state_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["继续按月统计", "查询2026年9月销售量，地区浙江"])
async def test_generic_continuation_and_complete_new_query_are_not_blocked(question):
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额，地区上海"), IDENTITY)
    assert first.status == "COMPLETED"
    count = len(retrieval.requests)
    await agent.handle(chat(question, "message-next"), IDENTITY)
    assert len(retrieval.requests) > count


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_change", ["model", "domain", "database", "knowledge", "conversation"])
async def test_incompatible_historical_scope_does_not_restore_or_query(scope_change):
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额，地区上海"), IDENTITY)
    assert first.status == "COMPLETED"
    update = {
        "model": {"semantic_model_id": 82}, "domain": {"business_domain_ids": [205]},
        "database": {"database_id": 77}, "knowledge": {"knowledge_base_names": ["another"]},
        "conversation": {"conversation_id": "different-conversation"},
    }[scope_change]
    current = chat("回到之前的销售额", "message-next").model_copy(update=update)
    count = len(retrieval.requests)
    response = await agent.handle(current, IDENTITY)
    assert response.status == "SAFE_FALLBACK"
    assert len(retrieval.requests) == count


class PendingRetrieval(CapturingRetrieval):
    armed = False

    async def query(self, request, identity, **kwargs):
        if self.armed:
            raise AdapterError("ASL_AMBIGUOUS", "synthetic catalog ambiguity", details=[{
                "type": "metric", "phrase": "销售额", "question": "选择销售额口径",
                "candidates": ["含税销售额", "不含税销售额"],
            }])
        return await super().query(request, identity, **kwargs)


@pytest.mark.asyncio
async def test_explicit_historical_recall_can_leave_an_unrelated_pending():
    retrieval = PendingRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月订单量，地区上海"), IDENTITY)
    assert first.status == "COMPLETED"
    retrieval.armed = True
    second = await agent.handle(chat("查询2026年8月销售额，地区江苏", "message-2"), IDENTITY)
    assert second.status == "NEEDS_CLARIFICATION"
    retrieval.armed = False
    third = await agent.handle(chat("回到之前的订单量", "message-3"), IDENTITY)
    assert third.status == "COMPLETED"
    assert retrieval.requests[-1].filters == retrieval.requests[0].filters
    assert retrieval.requests[-1].time_range == retrieval.requests[0].time_range
    assert [m.input for m in retrieval.requests[-1].metrics] == ["订单量"]
