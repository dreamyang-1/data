"""Explicit same-level region lists survive parsing and subsequent turns."""
from datetime import date

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.intent import RuleBasedIntentClassifier
from app.services.authorized_scope import bind_authorized_scope
from test_product_clear_contract import configured_agent
from test_semantic_choice_contract import IDENTITY, chat, service
from test_structural_scope_contract import StopAtAslTransport
from test_task_recall_contract import CapturingRetrieval


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)
    monkeypatch.setattr("app.intent.classifier.date", FixedDate)


def city_filter(*values):
    return {"field": "业务城市", "operator": "IN", "value": list(values)}


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["上海和北京销售额", "查询上海和北京销售额"])
@pytest.mark.parametrize("hybrid", [False, True], ids=["rule", "hybrid"])
async def test_complete_region_list_reaches_execution(question, hybrid):
    agent, retrieval, model = configured_agent(hybrid)
    response = await agent.handle(chat(question), IDENTITY)
    assert response.status == "COMPLETED" and not response.clarification_questions
    request = retrieval.requests[-1]
    assert request.filters == [city_filter("上海市", "北京市")]
    assert set(request.semantic_entity_mentions) <= {"上海市", "北京市"}
    assert [m.input for m in request.metrics] == ["销售额"]
    assert not request.dimensions  # A union filter does not request a comparison grain.
    if hybrid:
        assert model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("hybrid", [False, True], ids=["rule", "hybrid"])
async def test_list_replacement_survives_later_time_edit(hybrid):
    agent, retrieval, _ = configured_agent(hybrid)
    response = await agent.handle(chat("查询销售额，2026年7月，地区上海"), IDENTITY)
    assert response.status == "COMPLETED"
    assert retrieval.requests[-1].filters == [{"field": "业务城市", "operator": "EQ", "value": "上海市"}]
    for index, question in enumerate(["那北京和天津呢？", "那2026年8月呢？"]):
        response = await agent.handle(chat(question, f"replace-{index}"), IDENTITY)
        assert response.status == "COMPLETED"
        request = retrieval.requests[-1]
        assert request.filters == [city_filter("北京市", "天津市")]
        assert set(request.semantic_entity_mentions) <= {"北京市", "天津市"}
        assert [m.input for m in request.metrics] == ["销售额"]
    assert request.time_range.start == date(2026, 8, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["查询上海和北京销售额", "查询销售额，2026年7月，地区上海和北京"])
async def test_http_contract_preserves_the_entire_region_set(question):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "region-http")
    bind_authorized_scope(request, chat(question).authorized_semantic_scope)
    client = StopAtAslTransport()
    adapter = HttpDataRetrievalAdapter(service(CapturingRetrieval()).settings, client)
    with pytest.raises(AdapterError, match="Synthetic offline transport stop"):
        await adapter.query(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
    contract = client.payloads[0]["intent_asl_contract"]
    assert contract is not None
    assert contract["filters"] == [city_filter("上海市", "北京市")]
    assert set(contract["semantic_entity_mentions"]) <= {"上海市", "北京市"}


def test_admitted_region_list_replacement_drops_old_entity_evidence():
    from app.services.turn_admission import TurnAdmissionGate
    from test_phase0b_critical import parse

    previous = parse("查询销售额，2026年7月，地区上海")
    previous.analysis_thread_id = "existing-task"
    current = parse("那北京和天津呢？")
    # Supply an already parsed IN contract to isolate the state/sanitizer
    # boundary independently of the classifier's original list-loss defect.
    current.filters = [city_filter("北京市", "天津市")]
    current.semantic_entity_mentions = ["北京市", "天津市"]
    decision = TurnAdmissionGate().evaluate(question=current.original_question, current=current, previous=previous, message_id="replace")
    assert decision.inherit_business_context
    request = previous.model_copy(deep=True)
    request.turn_admission = decision
    TurnAdmissionGate.apply_explicit_slot_protection(request, current, decision)
    RuleBasedIntentClassifier.sanitize_semantic_entity_mentions(request)
    assert request.filters == current.filters
    assert set(request.semantic_entity_mentions) <= {"北京市", "天津市"}


@pytest.mark.parametrize("question,field,values", [
    ("查询销售额，2026年7月，地区江苏和浙江", "业务省份", ["江苏省", "浙江省"]),
    ("查询上海市与北京市销售额", "业务城市", ["上海市", "北京市"]),
    ("上海以及北京销售额", "业务城市", ["上海市", "北京市"]),
    ("查询销售额，地区上海、北京、天津", "业务城市", ["上海市", "北京市", "天津市"]),
    ("查询江苏省及浙江省销售额", "业务省份", ["江苏省", "浙江省"]),
    ("上海地区和北京地区销售额", "业务城市", ["上海市", "北京市"]),
    ("查询2026年7月上海和北京销售额", "业务城市", ["上海市", "北京市"]),
    ("查询上海和北京2026年7月销售额", "业务城市", ["上海市", "北京市"]),
    ("查询广西和宁夏销售额", "业务省份", ["广西壮族自治区", "宁夏回族自治区"]),
    ("查询销售额，地区上海和上海市", "业务城市", ["上海市"]),
])
def test_known_aliases_and_connectors_preserve_the_complete_set(question, field, values):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "region-alias")
    expected = {"field": field, "operator": "IN" if len(values) > 1 else "EQ", "value": values if len(values) > 1 else values[0]}
    assert request.filters == [expected]
    assert set(request.semantic_entity_mentions) <= set(values)
    assert request.dimensions == []


@pytest.mark.parametrize("subject", [
    "上海和江苏", "上海和新城", "上海和北京医院", "上海和北京产品", "上海换成北京",
    "上海不要北京", "上海和", "上海北京", "不要上海和北京", "上海和北京再加天津",
])
def test_partial_negative_mixed_and_named_spans_are_not_region_lists(subject):
    assert RuleBasedIntentClassifier._coordinated_region_values(subject) == []


@pytest.mark.parametrize("field,value", [
    ("商品名称", "上海和北京"), ("商品名称", ["上海", "北京"]),
    ("医院名称", "上海和北京医院"), ("经销商名称", "上海和北京有限公司"),
])
def test_typed_entity_values_keep_their_protected_region_spans(field, value):
    request = RuleBasedIntentClassifier().classify("查询销售额", IDENTITY, "protected-name")
    request.filters = [{"field": field, "operator": "IN" if isinstance(value, list) else "EQ", "value": value}]
    before = list(request.filters)
    RuleBasedIntentClassifier._apply_common_region_filter(request, "查询上海和北京医院销售额" if field == "医院名称" else "查询上海和北京有限公司销售额" if field == "经销商名称" else "查询上海和北京产品销售额")
    assert request.filters == before


@pytest.mark.asyncio
@pytest.mark.parametrize("commands,expected", [
    (["那江苏和浙江呢？", "那2026年8月呢？"], [{"field": "业务省份", "operator": "IN", "value": ["江苏省", "浙江省"]}]),
    (["那天津呢？", "那2026年8月呢？"], [{"field": "业务城市", "operator": "EQ", "value": "天津市"}]),
    (["不限地区", "那2026年8月呢？"], []),
    (["不限地区", "那北京和天津呢？", "那2026年8月呢？"], [city_filter("北京市", "天津市")]),
])
async def test_list_changes_keep_product_and_do_not_restore_old_regions(commands, expected):
    agent, retrieval, _ = configured_agent()
    product = {"field": "商品名称", "operator": "EQ", "value": "TDC-3"}
    response = await agent.handle(chat("查询TDC-3产品销售额，2026年7月，地区上海和北京"), IDENTITY)
    assert response.status == "COMPLETED"
    assert retrieval.requests[-1].filters == [city_filter("上海市", "北京市"), product]
    for index, question in enumerate(commands):
        response = await agent.handle(chat(question, f"list-edit-{index}"), IDENTITY)
        assert response.status == "COMPLETED"
    request = retrieval.requests[-1]
    assert sorted(request.filters, key=lambda item: item["field"]) == sorted([*expected, product], key=lambda item: item["field"])
    allowed = {"TDC-3"}
    for item in expected:
        allowed.update(item["value"] if isinstance(item["value"], list) else [item["value"]])
    assert set(request.semantic_entity_mentions) <= allowed
    assert "上海" not in (request.rewritten_question or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    {"semantic_model_id": 82}, {"business_domain_ids": [205]}, {"database_id": 99},
    {"knowledge_base_names": ["different-catalog"]}, {"conversation_id": "different-conversation"},
])
async def test_list_followup_cannot_inherit_from_an_incompatible_scope(update):
    from app.domain.models import ChatRequest
    agent, retrieval, _ = configured_agent()
    await agent.handle(chat("查询销售额，2026年7月，地区上海和北京"), IDENTITY)
    payload = chat("那江苏和浙江呢？", "mismatch").model_dump()
    payload.update(update)
    response = await agent.handle(ChatRequest.model_validate(payload), IDENTITY)
    assert response.status != "COMPLETED" and len(retrieval.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("domains", [[], [205]])
async def test_region_lists_never_change_authorized_business_domains(domains):
    from app.domain.models import ChatRequest
    agent, retrieval, _ = configured_agent()
    for index, question in enumerate(["查询销售额，2026年7月，地区上海和北京", "那江苏和浙江呢？"]):
        payload = chat(question, f"authorized-{index}").model_dump()
        payload["business_domain_ids"] = domains
        request = ChatRequest.model_validate(payload)
        response = await agent.handle(request, IDENTITY)
        assert response.status == "COMPLETED"
        assert retrieval.requests[-1].authorized_semantic_scope == request.authorized_semantic_scope


@pytest.mark.parametrize("text", ["不要上海和北京", "排除上海和北京", "上海换成北京", "上海不要北京"])
def test_negative_and_corrective_spans_do_not_gain_positive_union_semantics(text):
    request = RuleBasedIntentClassifier().classify("查询销售额", IDENTITY, "region-contrast")
    RuleBasedIntentClassifier._apply_common_region_filter(request, text)
    # Existing singleton/negative mutation debt is outside this list contract;
    # do not promote it to a newly invented positive IN predicate.
    assert not any(item.get("operator") == "IN" for item in request.filters)


def test_existing_comparison_still_requests_region_grain():
    from app.domain.models import PrimaryIntent
    request = RuleBasedIntentClassifier().classify("比较上海和北京销售额", IDENTITY, "comparison")
    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.filters == [{"field": "地区", "operator": "IN", "value": ["上海市", "北京市"]}]
    assert "地区" in request.dimensions


def test_replacing_region_keeps_shared_literals_owned_by_other_filters():
    from app.services.turn_admission import TurnAdmissionGate
    from test_phase0b_critical import parse
    before = parse("查询销售额，地区上海")
    before.filters += [{"field": "商品品牌", "operator": "EQ", "value": "上海市"}]
    before.analysis_thread_id = "existing-task"
    current = parse("那北京和天津呢？")
    decision = TurnAdmissionGate().evaluate(question=current.original_question, current=current, previous=before, message_id="shared")
    request = before.model_copy(deep=True)
    request.turn_admission = decision
    TurnAdmissionGate.apply_explicit_slot_protection(request, current, decision)
    RuleBasedIntentClassifier.sanitize_semantic_entity_mentions(request)
    assert "上海市" in request.semantic_entity_mentions
    assert {"field": "商品品牌", "operator": "EQ", "value": "上海市"} in request.filters


@pytest.mark.parametrize("text", [
    "地区上海和北京和新城", "地区新城和上海和北京", "地区上海和北京以及新城",
    "地区新城、上海、北京", "地区上海与北京与某地",
])
def test_unrecognized_outer_list_members_are_not_cut_out_of_a_positive_union(text):
    request = RuleBasedIntentClassifier().classify("查询销售额", IDENTITY, "partial-list")
    RuleBasedIntentClassifier._apply_common_region_filter(request, text)
    assert not any(item.get("operator") == "IN" for item in request.filters)
