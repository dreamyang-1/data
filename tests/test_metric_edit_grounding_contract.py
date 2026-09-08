"""Metric edit operators cannot become semantic entity constraints."""
from datetime import date

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.intent import HybridIntentClassifier, RuleBasedIntentClassifier
from app.intent.structured import StructuredIntentOutput
from app.services.authorized_scope import bind_authorized_scope
from app.services.question_rewriter import QuestionRewriter
from test_semantic_choice_contract import IDENTITY, chat, service
from test_structural_scope_contract import StopAtAslTransport
from test_task_recall_contract import CapturingRetrieval


INITIAL = "查询2026年7月销售额和销售量，地区上海"
EDITS = [("再加订单笔数", ["销售额", "销售量", "订单笔数"]),
         ("不要销售量", ["销售额"])]


@pytest.fixture(autouse=True)
def fixed_business_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)
    monkeypatch.setattr("app.intent.classifier.date", FixedDate)


class OfflineIntentModel:
    def __init__(self, unavailable=False, operator_entity=False):
        self.calls = []
        self.unavailable = unavailable
        self.operator_entity = operator_entity

    async def classify(self, question):
        self.calls.append(question)
        if self.unavailable:
            raise RuntimeError("Synthetic offline model unavailability")
        # Exercise both clean extraction and a schema-valid role mistake.
        return StructuredIntentOutput(primary_intent="METRIC_QUERY", confidence=0.99,
            metrics=["销售额", "销售量"],
            current_entity_values=[question[:2]] if self.operator_entity and question.startswith(("再加", "不要")) else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("question,expected", EDITS)
@pytest.mark.parametrize("unavailable", [False, True], ids=["successful", "fallback"])
async def test_hybrid_metric_edit_does_not_persist_an_operator_entity(question, expected, unavailable):
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    model = OfflineIntentModel(unavailable)
    agent.settings.intent_model_enabled = True
    agent.classifier = HybridIntentClassifier(agent.settings, model_client=model)
    agent.question_rewriter = QuestionRewriter(None)
    response = await agent.handle(chat(INITIAL), IDENTITY)
    assert response.status == "COMPLETED"
    before = retrieval.requests[-1]
    assert before.semantic_entity_mentions == ["上海市"]
    response = await agent.handle(chat(question, "edit"), IDENTITY)
    assert response.status == "COMPLETED"
    edited = retrieval.requests[-1]
    assert [m.input for m in edited.metrics] == expected
    assert edited.filters == before.filters and edited.time_range == before.time_range
    assert edited.semantic_entity_mentions == before.semantic_entity_mentions
    response = await agent.handle(chat("那2026年8月呢？", "next"), IDENTITY)
    assert response.status == "COMPLETED" and len(model.calls) >= 3
    after = retrieval.requests[-1]
    assert [m.input for m in after.metrics] == expected
    assert after.semantic_entity_mentions == before.semantic_entity_mentions
    assert after.filters == before.filters and after.time_range.start == date(2026, 8, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("question,expected", EDITS)
async def test_real_http_metric_edit_contract_excludes_operator_entities(question, expected):
    from test_phase0b_critical import parse, turn

    request, _ = turn(parse(INITIAL), question)
    bind_authorized_scope(request, chat(question).authorized_semantic_scope)
    client = StopAtAslTransport()
    adapter = HttpDataRetrievalAdapter(service(CapturingRetrieval()).settings, client)
    with pytest.raises(AdapterError, match="Synthetic offline transport stop"):
        await adapter.query(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
    assert [m.input for m in request.metrics] == expected
    assert client.payloads[0]["intent_asl_contract"]["semantic_entity_mentions"] == ["上海市"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,expected", EDITS)
@pytest.mark.parametrize("path", ["no-rewriter", "model-entity"])
async def test_orchestrator_reaches_http_without_operator_entity(question, expected, path):
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    model = OfflineIntentModel(unavailable=path == "no-rewriter", operator_entity=path == "model-entity")
    agent.settings.intent_model_enabled = True
    agent.classifier = HybridIntentClassifier(agent.settings, model_client=model)
    # Production DI supplies a rewriter even when semantic search is disabled.
    # The no-rewriter path is an additional supported constructor contrast.
    agent.question_rewriter = QuestionRewriter(None) if path == "model-entity" else None
    await agent.handle(chat(INITIAL), IDENTITY)
    response = await agent.handle(chat(question, "fallback-edit"), IDENTITY)
    assert response.status == "COMPLETED" and len(model.calls) >= 2
    request = retrieval.requests[-1].model_copy(deep=True)
    assert [m.input for m in request.metrics] == expected
    # Replace only mock catalog identifiers with unversioned semantic inputs;
    # the real HTTP adapter then resolves them through its normal ASL request.
    # The entity list, filters, period, scope and admitted state are untouched.
    for metric in request.metrics:
        metric.metric_id = None
        metric.version = None
    client = StopAtAslTransport()
    adapter = HttpDataRetrievalAdapter(agent.settings, client)
    with pytest.raises(AdapterError, match="Synthetic offline transport stop"):
        await adapter.query(request, IDENTITY, semantic_model_id=81, business_domain_id=None)
    assert client.payloads[0]["intent_asl_contract"]["semantic_entity_mentions"] == ["上海市"]


@pytest.mark.parametrize("question", [
    "再加订单笔数", "不要销售量", "加上销售额", "同时增加订单笔数", "并新增销售额",
    "补充销售额", "带上订单笔数", "不看销售额", "别看订单笔数", "去掉销售量",
    "移除销售量", "删掉销售量", "取消之前的销售量", "不要原来的销售量",
    "再加销售额和订单笔数", "再加销售额、订单笔数", "不要销售额和订单笔数",
    "再加订单笔数指标", " 再加 订单笔数！",
])
def test_complete_metric_commands_do_not_create_entity_mentions(question):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "operator")
    assert request.semantic_entity_mentions == []
    assert not request.filters


@pytest.mark.parametrize("question,value", [
    ("查询再加产品销售额", "再加"), ("查询不要产品销售额", "不要"),
    ("查询再加医疗器械销售额", "再加医疗器械"),
    ("查询不要忘记口罩销售额", "不要忘记口罩"),
    ("查询再加人民医院销售额", "再加人民医院"),
    ("查询不要有限公司销售额", "不要有限公司"),
])
def test_operation_words_inside_named_business_literals_remain_whole(question, value):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "literal")
    assert value in request.semantic_entity_mentions
    assert any(f.get("value") == value for f in request.filters)


@pytest.mark.parametrize("question", [
    "再加未发布业务指标", "不要商品", "再加订单笔数，地区江苏", "不要销售额再加订单笔数",
    "查询再加订单笔数", "再加医院维度", "再加TDC-3产品", "不限地区",
])
def test_non_metric_or_compound_commands_are_not_consumed(question):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "contrast")
    assert not RuleBasedIntentClassifier._metric_edit_scaffolding(request)


def test_explicit_typed_filter_value_is_not_removed_as_an_operator():
    from app.domain.models import MetricRef

    request = RuleBasedIntentClassifier().classify("再加订单笔数", IDENTITY, "typed")
    request.metrics = [MetricRef(input="订单笔数")]
    request.filters = [{"field": "商品名称", "operator": "EQ", "value": "再加"}]
    request.semantic_entity_mentions = ["再加"]
    RuleBasedIntentClassifier.sanitize_semantic_entity_mentions(request)
    assert request.semantic_entity_mentions == ["再加"]
    assert request.filters[0]["value"] == "再加"


def test_an_inherited_entity_with_the_same_spelling_keeps_its_business_role():
    from app.services.turn_admission import TurnAdmissionGate

    rules = RuleBasedIntentClassifier()
    before = rules.classify("查询再加产品销售额", IDENTITY, "inherited")
    # A verified prior entity may be represented by a semantic mention before
    # the current catalog binds it to a typed filter. Admission owns reuse.
    before.filters = []
    before.analysis_thread_id = "same-task"
    current = rules.classify("再加订单笔数", IDENTITY, "inherited")
    current.turn_admission = TurnAdmissionGate().evaluate(
        question=current.original_question, current=current, previous=before, message_id="entity-edit")
    assert current.turn_admission.inherit_business_context
    current.semantic_entity_mentions = ["再加"]
    rules.sanitize_semantic_entity_mentions(current)
    assert current.semantic_entity_mentions == ["再加"]


@pytest.mark.asyncio
async def test_model_operator_mistake_cannot_survive_add_remove_and_later_time_turn():
    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    agent.settings.intent_model_enabled = True
    agent.classifier = HybridIntentClassifier(agent.settings, model_client=OfflineIntentModel(operator_entity=True))
    agent.question_rewriter = QuestionRewriter(None)
    for index, question in enumerate([INITIAL, "再加订单笔数", "不要销售量", "那2026年8月呢？"]):
        response = await agent.handle(chat(question, f"sequence-{index}"), IDENTITY)
        assert response.status == "COMPLETED" and not response.clarification_questions
        assert retrieval.requests[-1].semantic_entity_mentions == ["上海市"]
    assert [m.input for m in retrieval.requests[-1].metrics] == ["销售额", "订单笔数"]
    assert retrieval.requests[-1].time_range.start == date(2026, 8, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    {"semantic_model_id": 82}, {"business_domain_ids": [205]}, {"database_id": 99},
    {"knowledge_base_names": ["different-catalog"]}, {"conversation_id": "different-conversation"},
])
async def test_cleaning_an_edit_never_grants_state_from_another_scope(update):
    from app.domain.models import ChatRequest

    retrieval = CapturingRetrieval()
    agent = service(retrieval)
    await agent.handle(chat(INITIAL), IDENTITY)
    payload = chat("不要销售量", "scope-change").model_dump()
    payload.update(update)
    response = await agent.handle(ChatRequest.model_validate(payload), IDENTITY)
    assert response.status != "COMPLETED" and len(retrieval.requests) == 1
