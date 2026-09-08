"""Temporal/geographic query syntax cannot become a fabricated product scope."""
from datetime import date

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.intent import HybridIntentClassifier, RuleBasedIntentClassifier
from app.intent.structured import StructuredIntentOutput
from app.services.authorized_scope import bind_authorized_scope
from app.services.intent_asl_contract import build_intent_asl_contract
from app.services.question_rewriter import QuestionRewriter
from test_question_rewriter import FakeSearcher
from test_semantic_choice_contract import IDENTITY, chat, service
from test_task_recall_contract import CapturingRetrieval


@pytest.fixture(autouse=True)
def fixed_business_clock(monkeypatch):
    import app.intent.classifier as classifier

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)

    monkeypatch.setattr(classifier, "date", FixedDate)


class OfflineIntentModel:
    def __init__(self):
        self.calls = []

    async def classify(self, question):
        self.calls.append(question)
        return StructuredIntentOutput(
            primary_intent="METRIC_QUERY", confidence=0.99,
            metrics=["订单量"], evidence=["订单量"],
        )


def configured_agent():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    model = OfflineIntentModel()
    agent.settings.intent_model_enabled = True
    agent.classifier = HybridIntentClassifier(agent.settings, model_client=model)
    searcher = FakeSearcher([{
        "score": 1.0, "record_id": "scope-city-shanghai", "entity_name": "市",
        "attribute_name": "业务城市", "attribute_code": "business_city",
        "attribute_value": "上海市", "semantic_model_id": 81,
        "business_domain_id": 205,
    }])
    agent.question_rewriter = QuestionRewriter(searcher)
    return agent, retrieval, model, searcher


@pytest.mark.asyncio
async def test_compact_time_region_does_not_pollute_executable_contract():
    agent, retrieval, model, searcher = configured_agent()
    response = await agent.handle(chat("查询2026年7月上海订单量"), IDENTITY)
    assert model.calls and searcher.calls
    assert response.status == "COMPLETED"
    assert len(retrieval.requests) == 1
    request = retrieval.requests[0]
    contract = build_intent_asl_contract(request)
    assert request.filters == [{"field": "业务城市", "operator": "EQ", "value": "上海市"}]
    assert set(contract["semantic_entity_mentions"]) <= {"上海市"}
    assert request.time_range.start == date(2026, 7, 1)
    assert request.time_range.end_exclusive == date(2026, 8, 1)


@pytest.mark.parametrize("question,field,value", [
    ("查询2026年7月上海订单量", "业务城市", "上海市"),
    ("查询上海2026年7月订单量", "业务城市", "上海市"),
    ("查询2026年7月上海地区的订单量", "业务城市", "上海市"),
    ("查询上海地区2026年7月订单量", "业务城市", "上海市"),
    ("查询2026年7月江苏地区的销售额", "业务省份", "江苏省"),
    ("上海2026年7月订单量", "业务城市", "上海市"),
    ("查询2026年7月订单量，地区上海", "业务城市", "上海市"),
])
def test_date_region_word_order_preserves_only_the_requested_filter(question, field, value):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "scope-word-order")
    assert request.filters == [{"field": field, "operator": "EQ", "value": value}]
    assert set(request.semantic_entity_mentions) <= {value}
    assert request.time_range.start == date(2026, 7, 1)
    assert request.time_range.end_exclusive == date(2026, 8, 1)


@pytest.mark.parametrize("question,field,value,start,end", [
    ("查询去年上海订单量", "业务城市", "上海市", date(2025, 1, 1), date(2026, 1, 1)),
    ("统计江苏本月订单量", "业务省份", "江苏省", date(2026, 9, 1), date(2026, 10, 1)),
    ("查看2026年7月1日北京市订单量", "业务城市", "北京市", date(2026, 7, 1), date(2026, 7, 2)),
    ("查询2026年7月的浙江省订单量", "业务省份", "浙江省", date(2026, 7, 1), date(2026, 8, 1)),
    ("查询广西壮族自治区2026年7月订单量", "业务省份", "广西壮族自治区", date(2026, 7, 1), date(2026, 8, 1)),
    ("查询2026年7月香港特别行政区订单量", "业务省份", "香港特别行政区", date(2026, 7, 1), date(2026, 8, 1)),
])
def test_existing_time_and_region_grammars_compose_without_entity_guess(question, field, value, start, end):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "scope-composition")
    assert request.filters == [{"field": field, "operator": "EQ", "value": value}]
    assert set(request.semantic_entity_mentions) <= {value}
    assert (request.time_range.start, request.time_range.end_exclusive) == (start, end)


@pytest.mark.parametrize("question,field,value", [
    ("查询2026年7月上海产品订单量", "商品名称", "2026年7月上海"),
    ("查询2026年7月上海口罩订单量", "商品名称", "2026年7月上海口罩"),
    ("查询上海人民医院订单量", "医院名称", "上海人民医院"),
    ("查询上海康健有限公司销售额", "经销商名称", "上海康健有限公司"),
    ("查询TDC-3产品订单量", "商品名称", "TDC-3"),
])
def test_named_entity_literals_are_not_partially_trimmed(question, field, value):
    # Preserve the original literal for governed grounding. These cases do not
    # assert that a synthetic product exists in any deployed semantic catalog.
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "scope-literal")
    assert request.filters == [{"field": field, "operator": "EQ", "value": value}]
    assert value in request.semantic_entity_mentions


@pytest.mark.parametrize("subject", [
    "2026年13月上海", "2026年7月新城", "2026年7月上海和江苏", "2026年7月上海医疗器械",
])
def test_unrecognized_or_incomplete_scope_is_not_silently_removed(subject):
    request = RuleBasedIntentClassifier().classify("查询订单量", IDENTITY, "scope-unknown")
    assert not RuleBasedIntentClassifier._is_structural_metric_subject(request, subject, allow_region=True)


@pytest.mark.asyncio
async def test_compact_query_region_replace_and_clear_survive_following_turns():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    for index, question in enumerate(["查询2026年7月上海订单量", "那江苏呢？", "不限地区", "按月"]):
        response = await agent.handle(chat(question, f"scope-turn-{index}"), IDENTITY)
        # The fixed mock has one data row; a monthly trend correctly refuses
        # to invent a trend conclusion after executing the preserved query.
        assert response.status == ("PARTIAL_SUCCESS" if index == 3 else "COMPLETED")
    first, replaced, cleared, grouped = retrieval.requests
    assert first.filters == [{"field": "业务城市", "operator": "EQ", "value": "上海市"}]
    assert replaced.filters == [{"field": "业务省份", "operator": "EQ", "value": "江苏省"}]
    assert not cleared.filters and not grouped.filters
    for request in retrieval.requests:
        assert [metric.input for metric in request.metrics] == ["订单量"]
        assert request.time_range == first.time_range


@pytest.mark.asyncio
async def test_compact_complete_new_request_does_not_inherit_old_product():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    await agent.handle(chat("查询TDC-3产品销售额"), IDENTITY)
    response = await agent.handle(chat("查询2026年7月上海订单量", "new-task"), IDENTITY)
    assert response.status == "COMPLETED"
    request = retrieval.requests[-1]
    assert request.filters == [{"field": "业务城市", "operator": "EQ", "value": "上海市"}]
    assert [metric.input for metric in request.metrics] == ["订单量"]


class StopAtAslTransport:
    def __init__(self):
        self.payloads = []

    async def post(self, base, path, payload, **kwargs):
        self.payloads.append(payload)
        raise AdapterError("ASL_GENERATION_FAILED", "Synthetic offline transport stop")


@pytest.mark.asyncio
async def test_real_http_adapter_does_not_send_fabricated_product_requirement():
    question = "查询2026年7月上海订单量"
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "scope-http")
    bind_authorized_scope(request, chat(question).authorized_semantic_scope)
    client = StopAtAslTransport()
    agent = service(CapturingRetrieval())
    adapter = HttpDataRetrievalAdapter(agent.settings, client)
    with pytest.raises(AdapterError, match="Synthetic offline transport stop"):
        await adapter.query(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
    assert client.payloads
    contract = client.payloads[0]["intent_asl_contract"]
    assert set(contract["semantic_entity_mentions"]) <= {"上海市"}
    assert [f["value"] for f in contract["filters"]] == ["上海市"]
