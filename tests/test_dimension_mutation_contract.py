"""Dimension edits change the requested grain without losing the active query."""
from datetime import date

import pytest

from app.domain.models import SlotOperationType, TurnRelation
from app.intent import RuleBasedIntentClassifier
from app.services.turn_admission import TurnAdmissionGate
from app.services.intent_asl_contract import build_intent_asl_contract
from test_semantic_choice_contract import IDENTITY, chat, service
from test_task_recall_contract import CapturingRetrieval


INITIAL = "查询销售额，按渠道和商品分组，2026年7月，地区上海"
EDITS = [
    ("不要渠道", ["商品"], SlotOperationType.REMOVE),
    ("去掉所有维度", [], SlotOperationType.CLEAR),
    ("再加医院维度", ["商品", "渠道", "医院"], SlotOperationType.ADD),
    ("换成按医院分组", ["医院"], SlotOperationType.REPLACE),
]


@pytest.fixture(autouse=True)
def fixed_business_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)

    monkeypatch.setattr("app.intent.classifier.date", FixedDate)


@pytest.mark.parametrize("question,expected,operation", EDITS)
def test_dimension_operation_is_applied_once_and_has_correct_provenance(question, expected, operation):
    from test_phase0b_critical import turn, parse

    before = parse(INITIAL)
    before.asl_template = {"fixture": "old grouping plan"}
    before.source_dataset_id = "old-result"
    after, decision = turn(before, question)
    assert after.dimensions == expected
    assert after.filters == before.filters and after.time_range == before.time_range
    assert after.asl_template is None and after.source_dataset_id is None
    assert [op.operation for op in decision.slot_operations if op.slot == "dimensions"] == [operation]
    # The same edit may cross more than one preservation boundary. Set edits
    # must be idempotent and cannot restore a removed member on reapplication.
    again = TurnAdmissionGate.apply_explicit_slot_protection(after, parse(question), decision)
    assert again.dimensions == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("question,expected,operation", EDITS)
async def test_following_time_turn_keeps_dimension_edit(question, expected, operation):
    agent, retrieval = await start()
    response = await agent.handle(chat(question, "edit"), IDENTITY)
    assert response.status == "COMPLETED"
    edited = retrieval.requests[-1]
    assert edited.dimensions == expected
    assert [op.operation for op in edited.slot_operations if op.slot == "dimensions"] == [operation]
    assert edited.asl_template is None and edited.source_dataset_id is None
    response = await agent.handle(chat("那2026年8月呢？", "time"), IDENTITY)
    assert response.status == "COMPLETED"
    final = retrieval.requests[-1]
    assert final.dimensions == expected
    assert final.filters == retrieval.requests[0].filters
    assert [m.input for m in final.metrics] == ["销售额"]
    assert final.time_range.start == date(2026, 8, 1)


@pytest.mark.parametrize("question", ["增加医院维度", "加上医院分组", "同时增加医院维度"])
@pytest.mark.asyncio
async def test_explicit_addition_variants_preserve_existing_dimensions(question):
    agent, retrieval = await start()
    response = await agent.handle(chat(question, "add-variant"), IDENTITY)
    assert response.status == "COMPLETED"
    assert set(retrieval.requests[-1].dimensions) == {"商品", "渠道", "医院"}


@pytest.mark.parametrize("question", [
    "查询医院销售额", "查询新增医院数量", "再加TDC-3产品", "增加医院维度的销售额",
    "不要渠道销售额", "再加未发布字段维度",
    "不要渠道，按医院分组", "去掉所有维度再加医院维度", "不要渠道再按医院分组",
])
def test_other_business_requests_do_not_become_dimension_additions(question):
    current = RuleBasedIntentClassifier().classify(question, IDENTITY, "contrast")
    assert TurnAdmissionGate._dimension_edit(question, current) is None


@pytest.mark.asyncio
async def test_new_task_after_clear_keeps_its_own_explicit_grouping():
    agent, retrieval = await start()
    await agent.handle(chat("去掉所有维度", "clear"), IDENTITY)
    response = await agent.handle(chat("查询订单量，按渠道分组，2026年8月，地区江苏", "new"), IDENTITY)
    assert response.status == "COMPLETED"
    request = retrieval.requests[-1]
    assert request.turn_relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert request.dimensions == ["渠道"]
    assert [m.input for m in request.metrics] == ["订单量"]
    assert request.filters == [{"field": "业务省份", "operator": "EQ", "value": "江苏省"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_update", [
    {"semantic_model_id": 82}, {"business_domain_ids": [205]},
    {"database_id": 99}, {"knowledge_base_names": ["another-catalog"]},
    {"conversation_id": "another-conversation"},
])
async def test_dimension_edit_does_not_reuse_incompatible_state(scope_update):
    from app.domain.models import ChatRequest

    agent, retrieval = await start()
    payload = chat("再加医院维度", "different-scope").model_dump()
    payload.update(scope_update)
    response = await agent.handle(ChatRequest.model_validate(payload), IDENTITY)
    assert len(retrieval.requests) == 1
    assert response.status != "COMPLETED"


def test_catalog_rename_retains_remove_operation():
    from test_phase0b_critical import parse

    before = parse(INITIAL)
    before.dimensions = ["商品", "销售渠道"]
    before.analysis_thread_id = "dimension-task"
    current = parse("不要渠道")
    gate = TurnAdmissionGate()
    decision = gate.evaluate(question="不要渠道", current=current, previous=before, message_id="rename")
    # This boundary receives labels already grounded by the current scoped
    # catalog; it cannot reinterpret a REMOVE as a positive grouping request.
    current.dimensions = ["销售渠道"]
    gate.rebind_current_semantic_shape(decision, current)
    result = gate.apply_explicit_slot_protection(before.model_copy(deep=True), current, decision)
    assert result.dimensions == ["商品"]
    assert [op.operation for op in result.slot_operations if op.slot == "dimensions"] == [SlotOperationType.REMOVE]


def test_catalog_cannot_expand_an_addition_to_a_substring_parent():
    from test_phase0b_critical import parse

    before = parse(INITIAL)
    before.analysis_thread_id = "dimension-task"
    current = parse("再加医院等级维度")
    gate = TurnAdmissionGate()
    decision = gate.evaluate(question=current.original_question, current=current, previous=before, message_id="nested")
    current.dimensions = ["医院", "医院等级"]
    gate.rebind_current_semantic_shape(decision, current)
    result = gate.apply_explicit_slot_protection(before.model_copy(deep=True), current, decision)
    assert result.dimensions == ["商品", "渠道", "医院等级"]


def test_remove_complete_label_does_not_remove_its_parent_dimension():
    from test_phase0b_critical import parse, turn

    before = parse(INITIAL)
    before.dimensions = ["医院", "医院等级", "渠道"]
    after, decision = turn(before, "不要医院等级")
    assert after.dimensions == ["医院", "渠道"]
    assert [op.new_value for op in decision.slot_operations if op.slot == "dimensions"] == [["医院等级"]]


@pytest.mark.asyncio
async def test_add_dimension_still_works_with_hybrid_classifier_and_rewriter():
    from app.intent import HybridIntentClassifier
    from app.intent.structured import StructuredIntentOutput
    from app.services.question_rewriter import QuestionRewriter

    class OfflineModel:
        calls = 0

        async def classify(self, question):
            self.calls += 1
            return StructuredIntentOutput(primary_intent="METRIC_QUERY", confidence=0.99, metrics=["销售额"])

    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    model = OfflineModel()
    agent.settings.intent_model_enabled = True
    agent.classifier = HybridIntentClassifier(agent.settings, model_client=model)
    agent.question_rewriter = QuestionRewriter(None)
    await agent.handle(chat(INITIAL), IDENTITY)
    response = await agent.handle(chat("再加医院维度", "hybrid-add"), IDENTITY)
    assert model.calls >= 2 and response.status == "COMPLETED"
    assert set(retrieval.requests[-1].dimensions) == {"商品", "渠道", "医院"}


@pytest.mark.asyncio
@pytest.mark.parametrize("question,expected,operation", EDITS)
async def test_real_http_adapter_transmits_only_the_resulting_groupings(question, expected, operation):
    from app.adapters.base import AdapterError
    from app.adapters.http import HttpDataRetrievalAdapter
    from app.services.authorized_scope import bind_authorized_scope
    from test_phase0b_critical import parse, turn
    from test_structural_scope_contract import StopAtAslTransport

    request, _ = turn(parse(INITIAL), question)
    bind_authorized_scope(request, chat(question).authorized_semantic_scope)
    client = StopAtAslTransport()
    adapter = HttpDataRetrievalAdapter(service(CapturingRetrieval()).settings, client)
    with pytest.raises(AdapterError, match="Synthetic offline transport stop"):
        await adapter.query(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
    assert client.payloads[0]["intent_asl_contract"]["required_groupings"] == expected


@pytest.mark.asyncio
async def test_clear_does_not_become_a_global_barrier_to_later_dimension_add():
    agent, retrieval = await start()
    await agent.handle(chat("去掉所有维度", "clear"), IDENTITY)
    response = await agent.handle(chat("再加医院维度", "add-after-clear"), IDENTITY)
    assert response.status == "COMPLETED"
    assert retrieval.requests[-1].dimensions == ["医院"]
    response = await agent.handle(chat("那2026年8月呢？", "time-after-clear"), IDENTITY)
    assert response.status == "COMPLETED"
    assert retrieval.requests[-1].dimensions == ["医院"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,added", [
    ("再加医院等级维度", ["医院等级"]),
    ("再加医院和医院等级维度", ["医院", "医院等级"]),
])
async def test_addition_uses_complete_labels_without_adding_substring_parents(question, added):
    agent, retrieval = await start()
    response = await agent.handle(chat(question, "nested-add"), IDENTITY)
    assert response.status == "COMPLETED"
    assert retrieval.requests[-1].dimensions == ["商品", "渠道", *added]


async def start():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    response = await agent.handle(chat(INITIAL), IDENTITY)
    assert response.status == "COMPLETED"
    request = retrieval.requests[0]
    assert set(request.dimensions) == {"商品", "渠道"}
    assert request.filters == [{"field": "业务城市", "operator": "EQ", "value": "上海市"}]
    assert request.semantic_entity_mentions == ["上海市"]
    assert request.time_range.start == date(2026, 7, 1)
    return agent, retrieval


@pytest.mark.asyncio
async def test_remove_dimension_reaches_execution_without_restoring_it():
    agent, retrieval = await start()
    response = await agent.handle(chat("不要渠道", "remove"), IDENTITY)
    assert response.status == "COMPLETED"
    request = retrieval.requests[-1]
    assert request.dimensions == ["商品"]
    assert build_intent_asl_contract(request)["required_groupings"] == ["商品"]


@pytest.mark.asyncio
async def test_clear_dimensions_reaches_execution_as_empty_grain():
    agent, retrieval = await start()
    response = await agent.handle(chat("去掉所有维度", "clear"), IDENTITY)
    assert response.status == "COMPLETED"
    request = retrieval.requests[-1]
    assert request.dimensions == []
    assert build_intent_asl_contract(request)["required_groupings"] == []


@pytest.mark.asyncio
async def test_add_dimension_preserves_task_instead_of_asking_for_metric():
    agent, retrieval = await start()
    response = await agent.handle(chat("再加医院维度", "add"), IDENTITY)
    assert response.status == "COMPLETED"
    assert not response.clarification_questions
    request = retrieval.requests[-1]
    assert set(request.dimensions) == {"商品", "渠道", "医院"}
    assert request.filters == retrieval.requests[0].filters
    assert request.time_range == retrieval.requests[0].time_range
    assert [m.input for m in request.metrics] == ["销售额"]
