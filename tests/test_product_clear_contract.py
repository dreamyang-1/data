"""Clearing a product condition preserves the rest of the active query."""
from datetime import date

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.intent import HybridIntentClassifier
from app.intent.structured import StructuredIntentOutput
from app.services.question_rewriter import QuestionRewriter
from test_semantic_choice_contract import IDENTITY, chat, service
from test_structural_scope_contract import StopAtAslTransport
from test_task_recall_contract import CapturingRetrieval

INITIAL = "查询TDC-3产品销售额，2026年7月，地区上海"
REGION = {"field": "业务城市", "operator": "EQ", "value": "上海市"}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)
    monkeypatch.setattr("app.intent.classifier.date", FixedDate)


class OfflineModel:
    def __init__(self):
        self.calls = []

    async def classify(self, question):
        self.calls.append(question)
        return StructuredIntentOutput(primary_intent="METRIC_QUERY", confidence=.99, metrics=["销售额"])


def configured_agent(hybrid=True):
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    model = OfflineModel()
    agent.question_rewriter = QuestionRewriter(None)
    if hybrid:
        agent.settings.intent_model_enabled = True
        agent.classifier = HybridIntentClassifier(agent.settings, model_client=model)
    return agent, retrieval, model


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["不限产品", "去掉产品条件"])
@pytest.mark.parametrize("hybrid", [False, True], ids=["rule", "hybrid"])
async def test_product_clear_executes_without_the_old_product(question, hybrid):
    agent, retrieval, model = configured_agent(hybrid)
    response = await agent.handle(chat(INITIAL), IDENTITY)
    assert response.status == "COMPLETED"
    before = retrieval.requests[-1]
    assert before.filters == [REGION, {"field": "商品名称", "operator": "EQ", "value": "TDC-3"}]
    assert set(before.semantic_entity_mentions) == {"TDC-3", "上海市"}
    response = await agent.handle(chat(question, "clear"), IDENTITY)
    assert response.status == "COMPLETED" and not response.clarification_questions
    cleared = retrieval.requests[-1]
    assert cleared.filters == [REGION]
    assert cleared.semantic_entity_mentions == ["上海市"]
    assert "TDC-3" not in (cleared.rewritten_question or "")
    assert cleared.time_range == before.time_range
    assert [m.input for m in cleared.metrics] == ["销售额"]
    assert cleared.asl_template is None and cleared.source_dataset_id is None
    response = await agent.handle(chat("那2026年8月呢？", "after-clear"), IDENTITY)
    assert response.status == "COMPLETED"
    assert retrieval.requests[-1].filters == [REGION]
    assert retrieval.requests[-1].semantic_entity_mentions == ["上海市"]
    assert retrieval.requests[-1].time_range.start == date(2026, 8, 1)
    if hybrid:
        assert model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["不限产品", "去掉产品条件"])
async def test_product_clear_http_payload_has_no_stale_constraint(question):
    from app.services.authorized_scope import bind_authorized_scope
    from test_phase0b_critical import parse, turn

    request, decision = turn(parse(INITIAL), question)
    bind_authorized_scope(request, chat(question).authorized_semantic_scope)
    client = StopAtAslTransport()
    adapter = HttpDataRetrievalAdapter(service(CapturingRetrieval()).settings, client)
    with pytest.raises(AdapterError, match="Synthetic offline transport stop"):
        await adapter.query(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
    contract = client.payloads[0]["intent_asl_contract"]
    assert contract is not None
    assert contract["filters"] == [REGION]
    assert contract["semantic_entity_mentions"] == ["上海市"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,value", [
    ("查询TDC-4产品销售额，2026年8月，地区上海", "TDC-4"),
    ("那TDC-4产品呢？", "TDC-4"), ("那TDC-3产品呢？", "TDC-3"),
])
async def test_explicit_product_after_clear_can_open_a_new_constraint(question, value):
    agent, retrieval, _ = configured_agent()
    await agent.handle(chat(INITIAL), IDENTITY)
    await agent.handle(chat("不限产品", "clear"), IDENTITY)
    response = await agent.handle(chat(question, "reopen"), IDENTITY)
    assert response.status == "COMPLETED"
    assert any(f.get("field") == "商品名称" and f.get("value") == value for f in retrieval.requests[-1].filters)
    await agent.handle(chat("那2026年9月呢？", "later"), IDENTITY)
    assert any(f.get("field") == "商品名称" and f.get("value") == value for f in retrieval.requests[-1].filters)


@pytest.mark.parametrize("question", [
    "不限产品", "不限制商品", "不限商品", "不限制产品", "去掉产品条件",
    "移除商品筛选", "取消产品过滤条件", "删除商品筛选条件", " 不限 产品！",
])
def test_complete_clear_commands_preserve_other_slots_and_trace(question):
    from app.domain.models import SlotOperationType
    from test_phase0b_critical import parse, turn

    before = parse(INITIAL)
    before.dimensions = ["商品", "渠道"]
    before.fields = ["商品名称", "商品规格"]
    before.filters += [
        {"field": "母品牌", "operator": "EQ", "value": "品牌甲"},
        {"field": "商品品类", "operator": "EQ", "value": "品类乙"},
        {"field": "厂家名称", "operator": "NE", "value": "厂商丙"},
    ]
    before.asl_template = {"fixture": "old-plan"}
    before.source_dataset_id = "old-dataset"
    after, decision = turn(before, question)
    assert after.filters == [before.filters[0], *before.filters[2:]]
    assert after.dimensions == before.dimensions and after.fields == before.fields
    assert after.time_range == before.time_range and after.metrics == before.metrics
    assert "TDC-3" not in after.semantic_entity_mentions
    assert after.asl_template is None and after.source_dataset_id is None
    assert [(op.operation, op.new_value) for op in decision.slot_operations if op.slot == "product"] == [(SlotOperationType.CLEAR, [])]


@pytest.mark.parametrize("question", [
    "查询不限产品牌销售额", "查询去掉产品条件产品销售额", "不限产品但只看TDC-3",
    "去掉产品条件再加TDC-4", "取消产品维度", "不要产品", "去掉品牌条件",
    "去掉产品数量", "不限产品的销售额是多少", "去掉产品条件，地区江苏",
])
def test_other_requests_are_not_consumed_as_product_clear(question):
    from app.services.legacy_guards import product_filter_clear_requested
    assert not product_filter_clear_requested(question)


@pytest.mark.asyncio
@pytest.mark.parametrize("questions", [
    ["不限产品", "不限地区", "那2026年8月呢？"],
    ["不限地区", "不限产品", "那2026年8月呢？"],
    ["不限产品", "那江苏呢？", "那2026年8月呢？"],
    ["不限产品", "不限产品", "那2026年8月呢？"],
])
async def test_clear_barriers_survive_other_condition_and_time_edits(questions):
    agent, retrieval, _ = configured_agent()
    await agent.handle(chat(INITIAL), IDENTITY)
    for i, question in enumerate(questions):
        response = await agent.handle(chat(question, f"barrier-{i}"), IDENTITY)
        assert response.status == "COMPLETED"
    expected = [{"field": "业务省份", "operator": "EQ", "value": "江苏省"}] if "那江苏呢？" in questions else ([] if "不限地区" in questions else [REGION])
    assert retrieval.requests[-1].filters == expected
    assert "TDC-3" not in retrieval.requests[-1].semantic_entity_mentions


def test_clear_removes_product_binding_and_reindexes_other_bindings():
    from app.domain.models import SemanticFilterBinding
    from app.services.legacy_guards import apply_product_clear_barrier, PRODUCT_CLEAR_BARRIER
    from test_phase0b_critical import parse

    request = parse(INITIAL)
    request.filters = [
        {"field": "catalog.product_title", "operator": "IN", "value": ["TDC-3", "TDC-4"]},
        REGION,
        {"field": "商品品牌", "operator": "EQ", "value": "TDC-3"},
    ]
    request.semantic_entity_mentions = ["TDC3", "TDC-3", "TDC-4", "上海市"]
    request.semantic_filter_bindings = [
        SemanticFilterBinding(filter_index=0, input_value="TDC3", canonical_value="TDC-3", canonical_name="商品名称", attribute_code="catalog.product_title", score=1, business_domain_id=205),
        SemanticFilterBinding(filter_index=1, input_value="上海", canonical_value="上海市", canonical_name="业务城市", attribute_code="city", score=1, business_domain_id=205),
    ]
    request.assumptions.append(PRODUCT_CLEAR_BARRIER)
    apply_product_clear_barrier(request)
    assert request.filters == [REGION, {"field": "商品品牌", "operator": "EQ", "value": "TDC-3"}]
    assert request.semantic_entity_mentions == ["TDC-3", "上海市"]
    assert len(request.semantic_filter_bindings) == 1
    assert request.semantic_filter_bindings[0].filter_index == 0
    assert request.semantic_filter_bindings[0].canonical_name == "业务城市"
    snapshot = request.model_dump(mode="json")
    apply_product_clear_barrier(request)
    assert request.model_dump(mode="json") == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    {"semantic_model_id": 82}, {"business_domain_ids": [205]}, {"database_id": 99},
    {"knowledge_base_names": ["different-catalog"]}, {"conversation_id": "different-conversation"},
])
async def test_clear_cannot_restore_state_from_a_different_scope(update):
    from app.domain.models import ChatRequest
    agent, retrieval, _ = configured_agent()
    await agent.handle(chat(INITIAL), IDENTITY)
    payload = chat("不限产品", "different-scope").model_dump()
    payload.update(update)
    response = await agent.handle(ChatRequest.model_validate(payload), IDENTITY)
    assert response.status != "COMPLETED" and len(retrieval.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("domains", [[], [205]])
async def test_clearing_business_filter_keeps_current_authorized_scope(domains):
    from app.domain.models import ChatRequest

    agent, retrieval, _ = configured_agent()
    for index, question in enumerate([INITIAL, "不限产品", "那2026年8月呢？"]):
        payload = chat(question, f"authorized-{index}").model_dump()
        payload["business_domain_ids"] = domains
        request = ChatRequest.model_validate(payload)
        response = await agent.handle(request, IDENTITY)
        assert response.status == "COMPLETED"
        assert retrieval.requests[-1].authorized_semantic_scope == request.authorized_semantic_scope
    assert retrieval.requests[-1].filters == [REGION]


@pytest.mark.asyncio
async def test_product_clear_does_not_change_existing_grouping_on_real_turns():
    agent, retrieval, _ = configured_agent()
    await agent.handle(chat("查询TDC-3产品销售额，按产品和渠道分组，2026年7月，地区上海"), IDENTITY)
    before = retrieval.requests[-1]
    assert set(before.dimensions) == {"产品", "渠道"}
    for index, question in enumerate(["不限产品", "那2026年8月呢？"]):
        response = await agent.handle(chat(question, f"group-{index}"), IDENTITY)
        assert response.status == "COMPLETED"
        assert retrieval.requests[-1].dimensions == before.dimensions
        assert retrieval.requests[-1].filters == [REGION]


@pytest.mark.parametrize("missing", [["metric"], ["semantic_ambiguity"]])
def test_clearing_product_does_not_answer_an_unrelated_pending_slot(missing):
    from app.intent import RuleBasedIntentClassifier
    from test_phase0b_critical import parse

    pending = parse(INITIAL)
    pending.missing_slots = missing
    after = RuleBasedIntentClassifier().merge_clarification(pending, "不限产品")
    assert after.filters == [REGION]
    assert after.missing_slots == missing


def test_late_restore_cannot_reintroduce_a_cleared_product_filter():
    from app.services.legacy_guards import apply_region_clear_barrier
    from test_phase0b_critical import parse, turn

    before = parse(INITIAL)
    after, _ = turn(before, "不限产品")
    after.filters.append({"field": "商品名称", "operator": "EQ", "value": "TDC-3"})
    after.semantic_entity_mentions.append("TDC-3")
    after.asl_template = {"filters": list(after.filters)}
    after.source_dataset_id = "stale-materialization"
    apply_region_clear_barrier(after)
    assert after.filters == [REGION] and after.semantic_entity_mentions == ["上海市"]
    assert after.asl_template is None and after.source_dataset_id is None
