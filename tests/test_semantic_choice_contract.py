"""A catalog clarification resolves one slot member, preserving the task."""
import json
from datetime import date

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterBundle, AdapterError
from app.adapters.mock import MockDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest, ChatRequest, MetricRef, PendingState, PrimaryIntent,
    SemanticAmbiguity, TimeRange, TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from app.stores import InMemorySessionStore


IDENTITY = TrustedIdentity(tenant_id="choice-tenant", user_id="choice-user")


def ambiguity(kind="metric", phrase="销售额"):
    names = ["含税销售额", "不含税销售额"] if kind == "metric" else ["省份", "城市"]
    return SemanticAmbiguity(
        type=kind, phrase=phrase, ambiguity_id="catalog-choice",
        question="请选择本次使用的业务口径。", candidates=names,
        candidate_details=[{"canonical_name": name, "canonical_code": f"option_{i}", "version": "v1"}
                           for i, name in enumerate(names)],
        affected_slots=["metrics" if kind == "metric" else "dimensions"],
        semantic_model_id=81,
    )


def pending(kind="metric", phrase="销售额"):
    return CanonicalAnalysisRequest(
        application_id="choice-app", conversation_id="choice-conversation",
        tenant_id=IDENTITY.tenant_id, user_id=IDENTITY.user_id,
        original_question="查询2026年7月按区域和渠道拆分销售额和订单量",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=81,
        metrics=[MetricRef(input="销售额"), MetricRef(input="订单量", metric_id="81:orders", version="v2")],
        dimensions=["区域", "渠道"],
        filters=[{"field": "地区", "operator": "EQ", "value": "上海市"}],
        time_range=TimeRange(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)),
        missing_slots=["semantic_ambiguity"],
        semantic_ambiguities=[ambiguity(kind, phrase)],
    )


def choose(request, answer="1"):
    choice = DataAnalysisOrchestrator._semantic_clarification_choice(request, answer)
    assert choice is not None
    return DataAnalysisOrchestrator._apply_semantic_clarification_choice(
        request, request.model_copy(deep=True), choice,
    )


def test_metric_choice_keeps_other_metric_and_metadata():
    request = pending()
    result = choose(request)
    assert [m.input for m in result.metrics] == ["含税销售额", "订单量"]
    assert result.metrics[1] == request.metrics[1]
    assert result.dimensions == request.dimensions
    assert result.filters == request.filters
    assert result.time_range == request.time_range
    assert request.metrics[0].input == "销售额"


def test_dimension_choice_keeps_other_grouping():
    request = pending("dimension", "区域")
    result = choose(request)
    assert result.dimensions == ["省份", "渠道"]
    assert result.metrics == request.metrics
    assert result.filters == request.filters


class AmbiguousRetrieval:
    def __init__(self, kind="metric", phrase="销售额", terminal_error=False):
        self.requests = []
        self.delegate = MockDataRetrievalAdapter()
        self.ambiguity = ambiguity(kind, phrase)
        self.terminal_error = terminal_error

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        members = [m.input for m in request.metrics] if self.ambiguity.type == "metric" else request.dimensions
        if not any(name in self.ambiguity.candidates for name in members):
            details = [self.ambiguity.model_dump(mode="json")]
            raise AdapterError("ASL_AMBIGUOUS", json.dumps(details, ensure_ascii=False), details=details)
        if self.terminal_error:
            raise AdapterError("SQL_EXECUTION_FAILED", "synthetic offline dependency failure")
        return await self.delegate.query(request, identity, **kwargs)


class FilterAmbiguousRetrieval:
    def __init__(self):
        self.requests = []
        self.delegate = MockDataRetrievalAdapter()
        candidates = [
            "hospital.hospital_name=江苏安馨血液透析中心有限公司",
            "product.product_name=血液透析导管组",
            "product.product_name=血液透析用干粉",
            "product.product_name=血液透析设备",
            "product_category.category_level1=03血液净化及腹膜透析设备",
            "product_category.category_level1=04血液净化及腹膜透析器具",
            "product_category.category_level2=01血液透析器具",
            "product_category.category_level2=01血液透析设备",
            "project.project_name=巴德血透产品",
            "project.project_name=费森尤斯血透",
        ]
        self.ambiguity = SemanticAmbiguity(
            type="filter",
            phrase="血透",
            ambiguity_id="catalog-filter-choice",
            question="过滤值血透存在多个目录候选。",
            candidates=candidates,
            candidate_details=[
                {
                    "label": label,
                    "canonical_name": label.split("=", 1)[0],
                    "canonical_code": label.split("=", 1)[0],
                    "canonical_value": label.split("=", 1)[1],
                    "input_value": "血透",
                    "operation": "UPSERT_FILTER",
                    "operator": "EQ",
                    "business_domain_id": 205,
                }
                for label in candidates
            ],
            affected_slots=["filters"],
            semantic_model_id=81,
        )

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if not any(
            item.get("field") == "project.project_name"
            and item.get("value") == "巴德血透产品"
            for item in request.filters
        ):
            details = [self.ambiguity.model_dump(mode="json")]
            raise AdapterError(
                "ASL_AMBIGUOUS",
                json.dumps(details, ensure_ascii=False),
                details=details,
            )
        return await self.delegate.query(request, identity, **kwargs)


class SurfaceOnlyRoleRetrieval:
    def __init__(self):
        self.requests = []
        self.delegate = MockDataRetrievalAdapter()
        self.ambiguity = SemanticAmbiguity(
            type="entity_role",
            phrase="费森尤斯产品",
            ambiguity_id="product-brand-role",
            question="请选择费森尤斯产品的匹配方式。",
            candidates=["按商品名称模糊匹配", "按品牌/厂家字段过滤"],
            candidate_details=[{}, {}],
            affected_slots=["filters"],
        )

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if "按品牌/厂家字段过滤" not in request.rewritten_question:
            details = [self.ambiguity.model_dump(mode="json")]
            raise AdapterError(
                "ASL_AMBIGUOUS",
                json.dumps(details, ensure_ascii=False),
                details=details,
            )
        return await self.delegate.query(request, identity, **kwargs)


def service(retrieval):
    defaults = build_mock_adapters()
    return DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(), sessions=InMemorySessionStore(),
        adapters=AdapterBundle(semantic=defaults.semantic, retrieval=retrieval, knowledge=defaults.knowledge),
    )


def chat(question, message="message-1"):
    return ChatRequest(semantic_model_id=81, application_id="choice-app",
                       conversation_id="choice-conversation", message_id=message, question=question)


@pytest.mark.asyncio
async def test_real_clarification_round_preserves_second_metric():
    retrieval = AmbiguousRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.missing_slots == ["semantic_ambiguity"]
    assert [m.input for m in retrieval.requests[0].metrics] == ["销售额", "订单量"]
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["含税销售额", "订单量"]
    assert retrieval.requests[-1].time_range == retrieval.requests[0].time_range


@pytest.mark.asyncio
async def test_real_filter_choice_round_applies_ninth_catalog_option():
    retrieval = FilterAmbiguousRetrieval()
    agent = service(retrieval)

    first = await agent.handle(
        chat("查询最近一年浙江省经销商名单"), IDENTITY
    )
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.clarification_items[0].options[8] == (
        "project.project_name=巴德血透产品"
    )

    second = await agent.handle(chat("9", "message-2"), IDENTITY)

    assert second.status == "COMPLETED"
    executed = retrieval.requests[-1]
    assert any(
        item.get("field") == "project.project_name"
        and item.get("value") == "巴德血透产品"
        for item in executed.filters
    )
    assert "巴德血透产品" in executed.rewritten_question
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" in executed.assumptions


@pytest.mark.asyncio
async def test_surface_only_choice_restores_full_task_before_planning():
    retrieval = SurfaceOnlyRoleRetrieval()
    agent = service(retrieval)

    class NoSplitPlanner:
        async def plan(self, question):
            return None

    agent.settings.multi_question_enabled = True
    agent.task_planner = NoSplitPlanner()
    request = pending()
    request.primary_intent = PrimaryIntent.DETAIL_QUERY
    request.entity = "医院"
    request.fields = ["医院名称"]
    request.metrics = []
    request.dimensions = ["医院"]
    request.filters = [{"field": "城市", "operator": "EQ", "value": "南京"}]
    request.original_question = "请提供南京哪些医院使用费森尤斯产品。"
    request.rewritten_question = request.original_question
    request.missing_slots = ["semantic_ambiguity"]
    request.semantic_ambiguities = [retrieval.ambiguity]
    await agent.sessions.put_pending(
        PendingState(request=request), expected_version=0,
    )

    events = []
    with progress_scope(events.append):
        second = await agent.handle(chat("2", "message-2"), IDENTITY)

    assert second.status == "COMPLETED"
    executed = retrieval.requests[-1]
    assert "按品牌/厂家字段过滤" in executed.rewritten_question
    planning = next(
        event for event in events
        if event["stage"] == "TASK_PLANNING" and event["status"] == "COMPLETED"
    )
    assert "任务1：2" not in planning["message"]
    assert "按品牌/厂家字段过滤" in planning["message"]
    assert await agent.sessions.get_pending(
        IDENTITY.tenant_id, IDENTITY.user_id,
        "choice-app", "choice-conversation",
    ) is None


@pytest.mark.parametrize("kind", ["metric", "dimension"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_choice_replaces_only_named_member_in_place(kind, position):
    names = ["成员甲", "成员乙", "成员丙"]
    request = pending(kind, names[position])
    if kind == "metric":
        request.metrics = [MetricRef(input=name, metric_id=f"81:old_{i}", version="v7") for i, name in enumerate(names)]
    else:
        request.dimensions = names.copy()
    before = request.model_dump(mode="json")
    result = choose(request, "2")
    expected = names.copy()
    expected[position] = request.semantic_ambiguities[0].candidates[1]
    actual = [m.input for m in result.metrics] if kind == "metric" else result.dimensions
    assert actual == expected
    assert request.model_dump(mode="json") == before
    assert not result.semantic_ambiguities


@pytest.mark.parametrize("alias", ["规范销售额", "81:old_sales"])
def test_catalog_phrase_can_target_existing_canonical_name_or_id(alias):
    request = pending(phrase=alias)
    request.metrics[0].canonical_name = "规范销售额"
    request.metrics[0].metric_id = "81:old_sales"
    result = choose(request)
    assert [m.input for m in result.metrics] == ["含税销售额", "订单量"]
    assert result.metrics[0].metric_id == "81:option_0"


@pytest.mark.parametrize("kind", ["metric", "dimension"])
@pytest.mark.parametrize("phrase", [None, "不存在的成员"])
def test_unknown_target_preserves_entire_pending_without_confirmation(kind, phrase):
    request = pending(kind)
    request.semantic_ambiguities[0].phrase = phrase
    result = choose(request)
    assert result.metrics == request.metrics
    assert result.dimensions == request.dimensions
    assert result.semantic_ambiguities == request.semantic_ambiguities
    assert result.missing_slots == ["semantic_ambiguity"]
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" not in result.assumptions
    assert "SEMANTIC_CHOICE_TARGET_UNRESOLVED" in result.assumptions


@pytest.mark.parametrize("kind", ["metric", "dimension"])
@pytest.mark.parametrize("empty", [False, True])
def test_legacy_no_phrase_can_resolve_empty_or_singleton_slot(kind, empty):
    request = pending(kind)
    request.semantic_ambiguities[0].phrase = None
    if kind == "metric":
        request.metrics = [] if empty else [request.metrics[0]]
    else:
        request.dimensions = [] if empty else [request.dimensions[0]]
    result = choose(request)
    members = [m.input for m in result.metrics] if kind == "metric" else result.dimensions
    assert members == [request.semantic_ambiguities[0].candidates[0]]
    assert not result.semantic_ambiguities


@pytest.mark.parametrize("kind", ["metric", "dimension"])
def test_duplicate_target_does_not_pick_first_member(kind):
    request = pending(kind, "重复名称")
    if kind == "metric":
        request.metrics = [MetricRef(input="重复名称", metric_id=f"81:{i}") for i in range(2)]
    else:
        request.dimensions = ["重复名称", "重复名称"]
    result = choose(request)
    assert result.metrics == request.metrics
    assert result.dimensions == request.dimensions
    assert result.semantic_ambiguities


def test_label_only_dimension_option_changes_only_its_target():
    request = pending("dimension", "区域")
    request.semantic_ambiguities[0].candidate_details = []
    result = choose(request, "2")
    assert result.dimensions == ["城市", "渠道"]


def test_catalog_filter_choice_adds_selected_scope_instead_of_dropping_it():
    request = pending()
    request.primary_intent = PrimaryIntent.DETAIL_QUERY
    request.entity = "经销商"
    request.fields = ["经销商名称"]
    request.metrics = []
    request.dimensions = ["经销商"]
    request.filters = [{"field": "省份名称", "operator": "EQ", "value": "浙江省"}]
    request.semantic_ambiguities = [SemanticAmbiguity(
        type="filter",
        phrase="血透",
        ambiguity_id="catalog-filter-choice",
        question="过滤值血透存在多个目录候选。",
        candidates=[
            "project.project_name=巴德血透产品",
            "project.project_name=费森尤斯血透",
        ],
        candidate_details=[
            {
                "label": "project.project_name=巴德血透产品",
                "canonical_name": "project.project_name",
                "canonical_code": "project.project_name",
                "canonical_value": "巴德血透产品",
                "input_value": "血透",
                "operation": "UPSERT_FILTER",
                "operator": "EQ",
                "business_domain_id": 205,
            },
            {
                "label": "project.project_name=费森尤斯血透",
                "canonical_name": "project.project_name",
                "canonical_code": "project.project_name",
                "canonical_value": "费森尤斯血透",
                "input_value": "血透",
                "operation": "UPSERT_FILTER",
                "operator": "EQ",
                "business_domain_id": 205,
            },
        ],
        affected_slots=["filters"],
        semantic_model_id=81,
    )]

    result = choose(request, "1")

    assert result.filters == [
        {"field": "省份名称", "operator": "EQ", "value": "浙江省"},
        {
            "field": "project.project_name",
            "operator": "EQ",
            "value": "巴德血透产品",
        },
    ]
    assert result.semantic_filter_bindings[-1].canonical_value == "巴德血透产品"
    assert result.semantic_filter_bindings[-1].input_value == "血透"
    assert "巴德血透产品" in result.rewritten_question
    assert not result.semantic_ambiguities
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" in result.assumptions


def test_label_only_filter_choice_is_preserved_in_completed_question_for_asl():
    request = pending()
    request.semantic_ambiguities = [SemanticAmbiguity(
        type="filter",
        question="过滤值血透存在多个目录候选。",
        candidates=["project.project_name=巴德血透产品"],
        candidate_details=[{}],
        affected_slots=["filters"],
    )]

    result = choose(request)

    assert result.filters == request.filters
    assert result.semantic_ambiguities == []
    assert result.missing_slots == []
    assert "project.project_name=巴德血透产品" in result.rewritten_question
    assert "SEMANTIC_CHOICE_TARGET_UNRESOLVED" not in result.assumptions
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" in result.assumptions
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_SURFACE_ONLY" in result.assumptions


def test_entity_role_choice_without_catalog_id_is_added_to_completed_question():
    request = pending()
    request.primary_intent = PrimaryIntent.DETAIL_QUERY
    request.entity = "医院"
    request.fields = ["医院名称"]
    request.metrics = []
    request.dimensions = ["医院"]
    request.filters = [{"field": "城市", "operator": "EQ", "value": "南京"}]
    request.semantic_ambiguities = [SemanticAmbiguity(
        type="entity_role",
        phrase="费森尤斯产品",
        ambiguity_id="product-brand-role",
        question="请选择费森尤斯产品的匹配方式。",
        candidates=[
            "按商品名称模糊匹配",
            "按品牌/厂家字段过滤",
        ],
        candidate_details=[{}, {}],
        affected_slots=["filters"],
    )]

    result = choose(request, "2")

    assert result.semantic_ambiguities == []
    assert result.missing_slots == []
    assert "按品牌/厂家字段过滤" in result.rewritten_question
    assert "南京" in result.rewritten_question
    assert "医院名称" in result.rewritten_question
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" in result.assumptions


def test_two_independent_choices_preserve_previously_confirmed_member():
    request = pending()
    second = ambiguity("dimension", "区域")
    second.ambiguity_id = "dimension-choice"
    request.semantic_ambiguities.append(second)
    first_result = choose(request)
    assert [a.ambiguity_id for a in first_result.semantic_ambiguities] == ["dimension-choice"]
    final = choose(first_result, "2")
    assert [m.input for m in final.metrics] == ["含税销售额", "订单量"]
    assert final.dimensions == ["城市", "渠道"]
    assert not final.semantic_ambiguities
    assert final.filters == request.filters


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["1", "第一个", "含税销售额"])
async def test_confirmed_metrics_survive_later_time_followup(answer):
    retrieval = AmbiguousRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.clarification_decision_traces[0].reason_type == "USER_SEMANTIC_AMBIGUITY"
    second = await agent.handle(chat(answer, "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    third = await agent.handle(chat("那2026年8月呢？", "message-3"), IDENTITY)
    assert third.status == "COMPLETED"
    executed = retrieval.requests[-1]
    assert [m.input for m in executed.metrics] == ["含税销售额", "订单量"]
    assert executed.time_range.start == date(2026, 8, 1)
    assert "订单量" in executed.rewritten_question
    assert await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation") is None


@pytest.mark.asyncio
async def test_real_dimension_choice_preserves_other_grouping():
    retrieval = AmbiguousRetrieval("dimension", "区域")
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月按区域和渠道拆分销售额"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert retrieval.requests[0].dimensions == ["区域", "渠道"]
    second = await agent.handle(chat("2", "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    assert retrieval.requests[-1].dimensions == ["城市", "渠道"]


@pytest.mark.asyncio
async def test_missing_target_cannot_execute_or_repeat_question():
    retrieval = AmbiguousRetrieval(phrase=None)
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    count = len(retrieval.requests)
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "SAFE_FALLBACK"
    assert not second.clarification_questions
    assert len(retrieval.requests) == count
    stored = await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation")
    assert stored is not None
    assert [m.input for m in stored.request.metrics] == ["销售额", "订单量"]


@pytest.mark.asyncio
async def test_terminal_failure_clears_resolved_pending_without_losing_slots():
    retrieval = AmbiguousRetrieval(terminal_error=True)
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "SAFE_FALLBACK"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["含税销售额", "订单量"]
    assert await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [81, 82])
async def test_complete_new_task_does_not_apply_old_candidate_or_inherit_slots(model):
    retrieval = AmbiguousRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    current = chat("查询2026年8月销售量", "message-2").model_copy(update={"semantic_model_id": model})
    await agent.handle(current, IDENTITY)
    executed = retrieval.requests[-1]
    assert [m.input for m in executed.metrics] == ["销售量"]
    assert executed.semantic_model_id == model
    assert executed.time_range.start == date(2026, 8, 1)
    assert "SEMANTIC_AMBIGUITY_CONFIRMED_BY_USER" not in executed.assumptions


class TwoAmbiguitiesRetrieval(AmbiguousRetrieval):
    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if not any(m.input == "含税销售额" for m in request.metrics):
            other = ambiguity("dimension", "区域")
            other.ambiguity_id = "other-catalog-choice"
            details = [ambiguity().model_dump(mode="json"), other.model_dump(mode="json")]
            raise AdapterError("ASL_AMBIGUOUS", json.dumps(details, ensure_ascii=False), details=details)
        return await self.delegate.query(request, identity, **kwargs)


@pytest.mark.asyncio
async def test_real_sequential_clarifications_keep_both_collections():
    retrieval = TwoAmbiguitiesRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月按区域和渠道拆分销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    count = len(retrieval.requests)
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "NEEDS_CLARIFICATION"
    assert second.clarification_items[0].options == ["省份", "城市"]
    assert len(retrieval.requests) == count
    third = await agent.handle(chat("2", "message-3"), IDENTITY)
    assert third.status == "COMPLETED"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["含税销售额", "订单量"]
    assert retrieval.requests[-1].dimensions == ["城市", "渠道"]
