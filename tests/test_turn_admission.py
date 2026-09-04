from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.adapters.base import AdapterBundle, AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.adapters.mock import build_mock_adapters
from app.adapters.semantic_query import CompositeSemanticQueryTool
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    DataQueryResult,
    Dataset,
    HistoryMessage,
    PrimaryIntent,
    TrustedIdentity,
    TurnRelation,
    SlotOperationType,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.orchestrator import select_data_execution_question
from app.services.turn_admission import TurnAdmissionGate
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore, SessionEventType


IDENTITY = TrustedIdentity(tenant_id="tenant-1", user_id="user-1")


def _finalized_standalone(
    classifier: RuleBasedIntentClassifier,
    gate: TurnAdmissionGate,
    question: str,
    conversation_id: str,
) -> CanonicalAnalysisRequest:
    raw = classifier.classify(question, IDENTITY, conversation_id)
    decision = gate.evaluate(
        question=question,
        current=raw,
        previous=None,
        message_id="m1",
    )
    decision.selected_thread_id = "thread-1"
    request = gate.apply_explicit_slot_protection(
        raw.model_copy(deep=True), raw, decision
    )
    request.analysis_thread_id = "thread-1"
    request.turn_admission = decision
    return request


def _decision(
    previous_question: str,
    current_question: str,
    conversation_id: str,
):
    classifier = RuleBasedIntentClassifier()
    gate = TurnAdmissionGate()
    previous = _finalized_standalone(
        classifier, gate, previous_question, conversation_id
    )
    current = classifier.classify(current_question, IDENTITY, conversation_id)
    decision = gate.evaluate(
        question=current_question,
        current=current,
        previous=previous,
        message_id="m2",
    )
    return gate, previous, current, decision


def _filter_value(request: CanonicalAnalysisRequest, field: str) -> str | None:
    return next(
        (
            str(item.get("value"))
            for item in request.filters
            if str(item.get("field") or "") == field
        ),
        None,
    )


def test_existing_monthly_trend_phrase_continues_active_business_task():
    _, _, current, decision = _decision(
        "按月分析费森尤斯产品的销售趋势",
        "月份的趋势",
        "existing-monthly-trend-followup",
    )

    assert current.missing_slots
    assert decision.relation == TurnRelation.CURRENT_TOPIC_FOLLOWUP
    assert decision.inherit_business_context is True
    assert decision.needs_clarification is False
    assert "EXISTING_MONTHLY_TREND" in decision.current_turn_facts.followup_signals


@pytest.mark.parametrize(
    "question",
    ("查询次要科室", "只看次要科室", "查询主科室", "次要的科室有哪些？"),
)
def test_department_relation_qualifier_is_current_topic_modification(question):
    _, previous, current, decision = _decision(
        "查询 TDC-3 产品的主要适用科室",
        question,
        "department-relation-modification",
    )

    assert previous.semantic_entity_mentions == ["TDC-3"]
    assert current.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert decision.relation == TurnRelation.CURRENT_TOPIC_MODIFICATION
    assert decision.inherit_business_context is True
    assert decision.needs_clarification is False
    assert "SEMANTIC_RELATION_QUALIFIER_REPLACEMENT" in decision.reason_codes


@pytest.mark.asyncio
async def test_real_department_followup_keeps_spec_and_replaces_relation_type():
    sessions = InMemorySessionStore()
    adapters = build_mock_adapters()
    planned_requests: list[CanonicalAnalysisRequest] = []
    original_query = adapters.retrieval.query

    async def capture_query(
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult:
        planned_requests.append(request.model_copy(deep=True))
        return await original_query(
            request,
            identity,
            semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )

    adapters.retrieval.query = capture_query
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test",
            adapter_mode="mock",
            intent_model_enabled=False,
            multi_question_model_enabled=False,
            analysis_synthesis_enabled=False,
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=adapters,
        sessions=sessions,
    )
    conversation_id = "real-department-relation-followup"
    first = await agent.handle(ChatRequest(
        application_id="app-1",
        conversation_id=conversation_id,
        message_id="m1",
        question="查询 TDC-3 产品的主要适用科室",
        semantic_model_id=81,
    ), IDENTITY)
    second = await agent.handle(ChatRequest(
        application_id="app-1",
        conversation_id=conversation_id,
        message_id="m2",
        question="查询次要科室",
        semantic_model_id=81,
    ), IDENTITY)
    executed = await sessions.get_last_request(
        "tenant-1", "user-1", "app-1", conversation_id
    )

    assert first.status == "COMPLETED"
    assert second.status == "COMPLETED"
    assert executed is not None
    assert executed.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert executed.metrics == []
    assert executed.semantic_entity_mentions == ["TDC-3"]
    assert executed.filters == [{
        "field": "适用科室类型", "operator": "EQ", "value": 2,
    }]
    assert executed.time_range is None
    assert executed.turn_relation == TurnRelation.CURRENT_TOPIC_MODIFICATION
    assert executed.rewritten_question == "查询 TDC-3 产品的次要适用科室"
    assert len(planned_requests) == 2
    assert planned_requests[0].asl_template is None
    # The previous verified ASL contains relation_type=1.  A short qualifier
    # replacement must be replanned from the canonical request, never reused.
    assert planned_requests[1].asl_template is None


def test_department_relation_sql_guard_is_column_scoped_for_numeric_values():
    gate, previous, current, decision = _decision(
        "查询 TDC-3 产品的主要适用科室",
        "查询次要科室",
        "department-relation-sql-guard",
    )
    request = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )
    request.turn_admission = decision

    # Unrelated occurrences of the old enum value (SELECT 1, attribute IDs,
    # LIMIT 1, and so on) must not be treated as a stale relation filter.
    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request,
        "SELECT 1 AS ordinal, department.dept_name "
        "FROM product_dept_relation "
        "JOIN department ON department.id = product_dept_relation.dept_id "
        "WHERE product_dept_relation.relation_type = 2",
    )
    assert report["status"] == "PASS"

    # A SQL plan that really keeps both the replaced and current predicates is
    # still rejected, now using the relation column rather than a bare digit.
    with pytest.raises(AdapterError) as stale:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT department.dept_name FROM product_dept_relation "
            "JOIN department ON department.id = product_dept_relation.dept_id "
            "WHERE product_dept_relation.relation_type IN (1, 2)",
        )
    assert stale.value.code == "STALE_CONTEXT_CONFLICT"


def test_model_grounded_short_followup_defers_entity_role_to_semantic_layer():
    gate, previous, current, decision = _decision(
        "空心纤维血液透析器产品的经销商有哪些",
        "那费森尤斯呢",
        "model-grounded-entity-replacement",
    )
    previous.asl_template = {"query_object": "dealer", "filters": [
        {"field": "商品名称", "operator": "EQ", "value": "空心纤维血液透析器"}
    ]}
    previous.source_dataset_id = "old-product-dataset"
    current.semantic_entity_mentions = ["费森尤斯"]
    current.intent_confidence = 0.95

    gate.promote_model_entity_replacement(
        decision=decision,
        current=current,
        previous=previous,
        raw_question="那费森尤斯呢",
    )
    merged = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )

    assert decision.relation == TurnRelation.CURRENT_TOPIC_MODIFICATION
    assert "MODEL_GROUNDED_ENTITY_REPLACEMENT" in decision.reason_codes
    assert _filter_value(merged, "商品名称") is None
    assert all(
        str(item.get("value") or "") != "空心纤维血液透析器"
        for item in merged.filters
    )
    assert current.semantic_entity_mentions == ["费森尤斯"]
    assert merged.asl_template is None
    assert merged.source_dataset_id is None
    assert "CORE_SUBJECT_CHANGE_REPLAN_REQUIRED" in merged.assumptions
    replacement = next(
        item for item in decision.slot_operations
        if item.slot == "filters"
    )
    assert replacement.operation == SlotOperationType.REPLACE
    assert replacement.new_value == []


def test_incomplete_unreferenced_turn_exposes_relation_clarification_state():
    gate, _, _, decision = _decision(
        "查询2026年8月含税销售总额",
        "分析数据",
        "ambiguous-relation-state",
    )

    assert decision.relation == TurnRelation.AMBIGUOUS_RELATION
    assert decision.needs_clarification is True
    assert decision.inherit_business_context is False
    assert not any(
        item.operation == SlotOperationType.INHERIT
        for item in decision.slot_operations
    )


def test_catalog_category_scope_does_not_become_a_synthetic_product_fact():
    question = (
        "查询上海市江苏苏云品牌低值耗材的经销商清单，"
        "并显示各经销商含税销售总额。"
    )
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        question,
        "catalog-category-admission",
    )

    assert request.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
        {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
    ]
    assert request.dimensions == ["经销商", "城市", "商品品牌", "商品品类"]
    facts = request.turn_admission.current_turn_facts
    assert facts.core_subjects == {
        "region": "上海市",
        "brand": "江苏苏云",
        "category": "低值耗材",
    }
    assert facts.explicit_slots["filters"].value == request.filters
    assert "product" not in facts.core_subjects


def test_ranked_partner_query_object_survives_explicit_slot_protection():
    question = (
        "查询上海地区销售振德医疗品牌医用外科口罩的经销商，"
        "并按整体业务规模从高到低排序。"
    )
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        question,
        "ranked-partner-admission",
    )

    assert request.entity == "经销商"
    assert "经销商" in request.dimensions
    assert request.turn_admission is not None
    assert (
        "经销商"
        in request.turn_admission.current_turn_facts.explicit_slots["dimensions"].value
    )


def test_catalog_category_alignment_accepts_split_sql_and_still_rejects_loss():
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        "查询上海市江苏苏云品牌低值耗材的经销商清单，"
        "并显示各经销商含税销售总额。",
        "catalog-category-alignment",
    )
    split_sql = (
        "SELECT dealer.dealer_name, SUM(sales_order.amount_with_tax) "
        "AS 含税销售总额 FROM sales_order "
        "JOIN dealer ON sales_order.dealer_code = dealer.dealer_code "
        "JOIN product ON sales_order.product_code = product.product_code "
        "JOIN manufacturer ON product.manufacturer_code = "
        "manufacturer.manufacturer_code "
        "JOIN product_category ON product.category_code = "
        "product_category.category_code "
        "WHERE dealer.city = '上海市' "
        "AND manufacturer.manufacturer_name = '江苏苏云医疗器材有限公司' "
        "AND product_category.category_name = '低值耗材' "
        "GROUP BY dealer.dealer_name"
    )

    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request, split_sql
    )
    assert report == {"status": "PASS", "checked_filters": 3}

    with pytest.raises(AdapterError) as missing_category:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            split_sql.replace(
                "AND product_category.category_name = '低值耗材' ", ""
            ),
        )
    assert missing_category.value.code == "SQL_QUERY_ENTITY_ALIGNMENT_FAILED"
    assert missing_category.value.details == {
        "missing_entities": [{"field": "商品品类", "value": "低值耗材"}]
    }


def test_catalog_category_change_is_a_core_subject_change_not_a_followup_leak():
    gate, _, _, decision = _decision(
        "查询上海市江苏苏云品牌低值耗材的经销商清单。",
        "查询上海市江苏苏云品牌高值耗材的经销商清单。",
        "catalog-category-topic-change",
    )

    assert decision.current_turn_facts.core_subjects["category"] == "高值耗材"
    assert decision.core_subject_changed is True
    assert decision.inherit_business_context is False


def test_concrete_product_fact_remains_protected_when_no_category_was_parsed():
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        "查询上海市江苏苏云品牌医用外科口罩的经销商清单。",
        "catalog-product-admission",
    )

    facts = request.turn_admission.current_turn_facts
    assert facts.core_subjects["product"] == "医用外科口罩"
    assert _filter_value(request, "商品名称") == "医用外科口罩"


def test_semantic_dimension_rebind_updates_provenance_before_slot_protection():
    classifier = RuleBasedIntentClassifier()
    gate = TurnAdmissionGate()
    question = "查询上海市江苏苏云品牌低值耗材的经销商清单。"
    raw = classifier.classify(question, IDENTITY, "semantic-rebind")
    decision = gate.evaluate(
        question=question,
        current=raw,
        previous=None,
        message_id="semantic-rebind-message",
    )
    grounded = raw.model_copy(deep=True)
    grounded.filters = [
        {"field": "销售城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
        {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
    ]
    grounded.dimensions = ["经销商", "销售城市", "商品品牌", "商品品类"]

    gate.rebind_current_semantic_shape(decision, grounded)
    final = gate.apply_explicit_slot_protection(
        grounded.model_copy(deep=True), grounded, decision
    )

    assert final.filters == grounded.filters
    assert final.dimensions == grounded.dimensions
    assert decision.current_turn_facts.explicit_slots["filters"].value == grounded.filters
    assert decision.current_turn_facts.explicit_slots["dimensions"].value == grounded.dimensions


def test_case_1_complete_new_product_trend_is_standalone_new_topic():
    gate, previous, current, decision = _decision(
        "查询空心纤维血液透析器产品合作的经销商名单。",
        "按月分析外周插管中心静脉导管的销售趋势。",
        "case-1",
    )

    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert decision.current_turn_facts.is_self_contained is True
    assert decision.core_subject_changed is True
    assert decision.inherit_business_context is False
    assert decision.create_new_analysis_thread is True
    assert decision.context_mode.value == "NONE"
    assert decision.current_turn_facts.explicit_slots["time_grain"].value == "month"

    final = gate.apply_explicit_slot_protection(
        current.model_copy(deep=True), current, decision
    )
    assert final.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert _filter_value(final, "商品名称") == "外周插管中心静脉导管"
    assert "经销商" not in final.dimensions
    assert "空心纤维血液透析器" not in str(final.model_dump())
    assert previous.analysis_thread_id != decision.selected_thread_id


def test_interrogative_relationship_question_is_self_contained_new_topic():
    _, _, current, decision = _decision(
        "按月分析外周插管中心静脉导管的销售趋势。",
        "空心纤维血液透析器产品的经销商有哪些？",
        "interrogative-relationship-new-topic",
    )

    assert current.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert current.entity == "经销商"
    assert current.missing_slots == []
    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert decision.current_turn_facts.is_self_contained is True
    assert decision.inherit_business_context is False


def test_case_2_elliptical_product_replacement_is_current_topic_modification():
    gate, previous, current, decision = _decision(
        "分析空心纤维血液透析器销售趋势。",
        "外周插管中心静脉导管呢？",
        "case-2",
    )

    assert decision.relation == TurnRelation.CURRENT_TOPIC_MODIFICATION
    assert decision.inherit_business_context is True
    assert decision.core_subject_changed is True
    assert decision.current_turn_facts.explicit_slots["product"].value == (
        "外周插管中心静脉导管"
    )

    merged = previous.model_copy(deep=True)
    final = gate.apply_explicit_slot_protection(merged, current, decision)
    assert final.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert _filter_value(final, "商品名称") == "外周插管中心静脉导管"
    assert "空心纤维血液透析器" not in str(final.filters)


def test_case_3_complete_same_domain_query_is_still_new_topic():
    _, _, _, decision = _decision(
        "分析空心纤维血液透析器销售趋势。",
        "按月分析外周插管中心静脉导管销售趋势。",
        "case-3",
    )

    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert decision.inherit_business_context is False


def test_case_4_elliptical_analysis_action_inherits_product():
    gate, previous, current, decision = _decision(
        "查询A产品经销商。",
        "销售趋势呢？",
        "case-4",
    )

    assert _filter_value(previous, "商品名称") == "A"
    assert decision.relation == TurnRelation.CURRENT_TOPIC_FOLLOWUP
    assert decision.inherit_business_context is True

    final = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )
    assert final.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert _filter_value(final, "商品名称") == "A"


def test_case_5_complete_product_change_clears_dealer_task():
    gate, _, current, decision = _decision(
        "查询A产品经销商。",
        "分析B产品销售趋势。",
        "case-5",
    )

    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert decision.inherit_business_context is False
    final = gate.apply_explicit_slot_protection(
        current.model_copy(deep=True), current, decision
    )
    assert final.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert _filter_value(final, "商品名称") == "B"
    assert "经销商" not in final.dimensions
    assert "A" not in str(final.filters)


def test_case_6_region_ellipsis_is_current_topic_modification():
    gate, previous, current, decision = _decision(
        "上海A产品趋势。",
        "北京呢？",
        "case-6",
    )

    assert decision.relation == TurnRelation.CURRENT_TOPIC_MODIFICATION
    assert decision.inherit_business_context is True
    final = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )
    assert _filter_value(final, "业务城市") == "北京市"
    assert _filter_value(final, "商品名称") == "A"


def test_case_7_complete_region_product_ranking_is_new_topic():
    gate, _, current, decision = _decision(
        "上海A产品趋势。",
        "分析广东B产品医院销量排名。",
        "case-7",
    )

    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert decision.inherit_business_context is False
    final = gate.apply_explicit_slot_protection(
        current.model_copy(deep=True), current, decision
    )
    assert _filter_value(final, "业务省份") == "广东省"
    assert _filter_value(final, "商品名称") == "B"
    assert "上海" not in str(final.filters)
    assert "A" not in str(final.filters)


def test_explicit_topic_switch_prefix_starts_a_new_thread():
    _, _, current, decision = _decision(
        "查询A产品的含税销售总额。",
        "切换话题：查询B产品的订单笔数。",
        "explicit-topic-switch",
    )

    assert current.primary_intent == PrimaryIntent.METRIC_QUERY
    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert decision.inherit_business_context is False
    assert "EXPLICIT_TOPIC_SHIFT" in decision.reason_codes


def test_nationwide_followup_clears_only_region_scope():
    gate, previous, current, decision = _decision(
        "查询广东省空心纤维血液透析器产品的含税销售总额。",
        "那全国整体呢？",
        "clear-region-scope",
    )

    final = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )

    assert decision.inherit_business_context is True
    assert all(
        gate._semantic_field_family(str(item.get("field") or "")) != "region"
        for item in final.filters
    )
    assert _filter_value(final, "商品名称") == "空心纤维血液透析器"
    assert [metric.input for metric in final.metrics] == ["含税销售总额"]
    assert "EXPLICIT_REGION_SCOPE_CLEARED" in final.assumptions


def test_query_to_sql_alignment_rejects_missing_or_stale_current_entity():
    gate, previous, current, decision = _decision(
        "查询空心纤维血液透析器产品合作的经销商名单。",
        "按月分析外周插管中心静脉导管的销售趋势。",
        "sql-alignment",
    )
    request = gate.apply_explicit_slot_protection(
        current.model_copy(deep=True), current, decision
    )
    request.turn_admission = decision

    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request,
        "SELECT * FROM sales_order WHERE product_name = '外周插管中心静脉导管'",
    )
    assert report["status"] == "PASS"

    with pytest.raises(AdapterError) as missing:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT * FROM sales_order WHERE product_name = '空心纤维血液透析器产品'",
        )
    assert missing.value.code == "SQL_QUERY_ENTITY_ALIGNMENT_FAILED"

    with pytest.raises(AdapterError) as stale:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT * FROM sales_order WHERE product_name IN "
            "('外周插管中心静脉导管', '空心纤维血液透析器产品')",
        )
    assert stale.value.code == "STALE_CONTEXT_CONFLICT"


def test_product_name_alignment_accepts_only_the_generic_product_suffix_alias():
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        "查询空心纤维血液透析器产品合作的经销商名单。",
        "product-suffix-alias",
    )

    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request,
        "SELECT dealer_name FROM dealer LEFT JOIN product "
        "ON dealer.product_code = product.product_code "
        "WHERE product.product_name = '空心纤维血液透析器'",
    )
    assert report == {"status": "PASS", "checked_filters": 1}

    with pytest.raises(AdapterError) as different_product:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT dealer_name FROM dealer LEFT JOIN product "
            "ON dealer.product_code = product.product_code "
            "WHERE product.product_name = '腹膜透析器'",
        )
    assert different_product.value.code == "SQL_QUERY_ENTITY_ALIGNMENT_FAILED"


def test_product_suffix_alias_keeps_negative_filter_polarity_enforced():
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        "查询空心纤维血液透析器产品合作的经销商名单。",
        "negative-product-suffix",
    )
    request.filters[0]["operator"] = "NE"
    explicit_filters = request.turn_admission.current_turn_facts.explicit_slots[
        "filters"
    ].value
    explicit_filters[0]["operator"] = "NE"

    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request,
        "SELECT dealer_name FROM dealer LEFT JOIN product "
        "ON dealer.product_code = product.product_code "
        "WHERE product.product_name != '空心纤维血液透析器'",
    )
    assert report["status"] == "PASS"

    with pytest.raises(AdapterError) as reversed_polarity:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT dealer_name FROM dealer LEFT JOIN product "
            "ON dealer.product_code = product.product_code "
            "WHERE product.product_name = '空心纤维血液透析器'",
        )
    assert reversed_polarity.value.code == "SQL_QUERY_FILTER_POLARITY_FAILED"


def test_exact_product_filter_cannot_be_weakened_to_like():
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        "查询医用外科口罩产品合作的经销商名单。",
        "exact-filter-operator",
    )

    with pytest.raises(AdapterError) as weakened:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT dealer_name FROM dealer LEFT JOIN product "
            "ON dealer.product_code = product.product_code "
            "WHERE product.product_name LIKE '%医用外科口罩%'",
        )
    assert weakened.value.code == "SQL_QUERY_FILTER_OPERATOR_FAILED"


def test_hospital_level_filter_accepts_registered_value_without_object_suffix():
    request = _finalized_standalone(
        RuleBasedIntentClassifier(),
        TurnAdmissionGate(),
        "查询三级医院的含税销售总额。",
        "hospital-level-value-normalization",
    )

    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request,
        "SELECT SUM(amount) FROM hospital "
        "WHERE hospital.hospital_level = '三级'",
    )
    assert report["status"] == "PASS"


def test_semantic_filter_change_invalidates_inherited_asl_and_dataset():
    gate, previous, current, decision = _decision(
        "查询A产品合作的经销商名单。",
        "只看上海的。",
        "semantic-filter-replan",
    )
    previous.asl_template = {"subject": {"entity": "dealer"}}
    previous.source_dataset_id = "dealer-list-a"

    merged = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )

    assert merged.asl_template is None
    assert merged.source_dataset_id is None
    assert "SEMANTIC_SLOT_CHANGE_REPLAN_REQUIRED" in merged.assumptions


def test_complete_extrema_query_is_not_mistaken_for_result_followup():
    _, _, _, decision = _decision(
        "查询A产品合作的经销商名单。",
        "查询含税销售总额最高的经销商。",
        "standalone-extrema-query",
    )

    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC


def test_elliptical_extrema_question_is_admitted_as_result_followup():
    _, _, _, decision = _decision(
        "按月分析2025年含税销售总额趋势。",
        "哪个月最高？比最低月高多少？",
        "extrema-result-followup",
    )

    assert decision.relation == TurnRelation.CURRENT_TOPIC_FOLLOWUP
    assert "RESULT_EXTREMA" in decision.current_turn_facts.followup_signals


def test_stale_product_detection_understands_the_same_suffix_alias():
    gate, previous, current, decision = _decision(
        "查询空心纤维血液透析器产品合作的经销商名单。",
        "外周插管中心静脉导管呢？",
        "stale-product-suffix",
    )
    request = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, decision
    )
    request.turn_admission = decision

    with pytest.raises(AdapterError) as stale:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT dealer_name FROM dealer LEFT JOIN product "
            "ON dealer.product_code = product.product_code "
            "WHERE product.product_name IN "
            "('外周插管中心静脉导管', '空心纤维血液透析器')",
        )
    assert stale.value.code == "STALE_CONTEXT_CONFLICT"


def test_filter_polarity_is_preserved_in_asl_and_sql_guards():
    question = (
        "查询上海市医用外科口罩产品的经销商，"
        "排除上海洁安厂家，并按整体业务规模排序。"
    )
    request = _finalized_standalone(
        RuleBasedIntentClassifier(), TurnAdmissionGate(), question, "polarity"
    )

    with pytest.raises(AdapterError) as asl_error:
        HttpDataRetrievalAdapter._validate_request_filters(
            {
                "filters": [
                    {"field": "地区", "operator": "EQ", "value": "上海市"},
                    {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
                    {"field": "厂家名称", "operator": "EQ", "value": "上海洁安"},
                ]
            },
            request,
        )
    assert asl_error.value.code == "ASL_REQUIRED_FILTER_MISSING"

    with pytest.raises(AdapterError) as sql_error:
        HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
            request,
            "SELECT dealer_name FROM dealer WHERE city = '上海市' "
            "AND product_name = '医用外科口罩' "
            "AND manufacturer_name = '上海洁安'",
        )
    assert sql_error.value.code == "SQL_QUERY_FILTER_POLARITY_FAILED"

    report = HttpDataRetrievalAdapter._validate_query_to_sql_entity_alignment(
        request,
        "SELECT dealer_name FROM dealer WHERE city = '上海市' "
        "AND product_name = '医用外科口罩' "
        "AND manufacturer_name != '上海洁安'",
    )
    assert report["status"] == "PASS"


def test_execution_query_policy_uses_raw_only_for_standalone_turns():
    classifier = RuleBasedIntentClassifier()
    gate = TurnAdmissionGate()
    raw_question = "查询上海市空心纤维血液透析器产品销售额。"
    standalone = classifier.classify(raw_question, IDENTITY, "execution-source")
    standalone_decision = gate.evaluate(
        question=raw_question,
        current=standalone,
        previous=None,
        message_id="m1",
    )

    execution, canonical, source = select_data_execution_question(
        standalone, standalone_decision, raw_question
    )
    assert execution == raw_question
    assert canonical != ""
    assert source == "RAW_STANDALONE"

    gate, previous, current, followup = _decision(
        "分析A产品销售趋势。",
        "北京呢？",
        "execution-followup",
    )
    contextual = gate.apply_explicit_slot_protection(
        previous.model_copy(deep=True), current, followup
    )
    execution, canonical, source = select_data_execution_question(
        contextual, followup, "北京呢？"
    )
    assert execution == canonical
    assert execution != "北京呢？"
    assert "A" in execution
    assert "北京市" in execution
    assert source == "CANONICAL_CONTEXTUAL"


class _CapturingTwoTurnRetrieval:
    def __init__(self) -> None:
        self.requests: list[CanonicalAnalysisRequest] = []
        self.sql: list[str] = []

    async def health(self) -> bool:
        return True

    async def rewrite_health(self) -> bool:
        return True

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult:
        self.requests.append(request.model_copy(deep=True))
        product = _filter_value(request, "商品名称") or ""
        if request.primary_intent == PrimaryIntent.TREND_ANALYSIS:
            columns = ["交易日期", "销售额"]
            rows = [
                {"交易日期": "2025-10", "销售额": 100.0},
                {"交易日期": "2025-11", "销售额": 80.0},
                {"交易日期": "2025-12", "销售额": 120.0},
            ]
            sql = (
                "SELECT month, SUM(sales_amount) FROM sales_order "
                f"WHERE product_name = '{product}' GROUP BY month"
            )
        else:
            columns = ["经销商名称"]
            rows = [{"经销商名称": "示例经销商"}]
            sql = (
                "SELECT DISTINCT dealer_name FROM sales_order "
                f"WHERE product_name = '{product}'"
            )
        self.sql.append(sql)
        return DataQueryResult(
            asl={"version": "2.0", "intent": "query", "ambiguity": []},
            sql=sql,
            dataset=Dataset(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                snapshot_id="context-leakage-test",
                data_as_of=datetime(2025, 12, 30, tzinfo=timezone.utc),
            ),
            data_source_id="mock",
        )


@pytest.mark.asyncio
async def test_real_two_turn_orchestration_does_not_leak_previous_product_or_dealer():
    base = build_mock_adapters()
    retrieval = _CapturingTwoTurnRetrieval()
    event_store = InMemorySessionEventStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=False
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=AdapterBundle(
            semantic=base.semantic,
            retrieval=retrieval,
            knowledge=base.knowledge,
            policy=base.policy,
            analysis=base.analysis,
            semantic_query=CompositeSemanticQueryTool(base.semantic, retrieval),
        ),
        sessions=InMemorySessionStore(),
        event_store=event_store,
    )
    conversation_id = "real-two-turn-context-leakage"
    first = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id=conversation_id,
            message_id="m1",
            question="查询空心纤维血液透析器产品合作的经销商名单。",
            semantic_model_id=81,
        ),
        IDENTITY,
    )
    second = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id=conversation_id,
            message_id="m2",
            question="按月分析外周插管中心静脉导管的销售趋势。",
            semantic_model_id=81,
            history=[
                HistoryMessage(
                    role="user",
                    content="查询空心纤维血液透析器产品合作的经销商名单。",
                    message_id="m1",
                ),
                HistoryMessage(
                    role="assistant",
                    content=first.answer,
                    message_id="a1",
                ),
            ],
        ),
        IDENTITY,
    )

    assert first.status == "COMPLETED"
    assert second.status == "COMPLETED", second.model_dump(mode="json")
    executed = retrieval.requests[-1]
    assert executed.turn_relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert executed.context_mode.value == "NONE"
    assert executed.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert executed.entity is None
    assert executed.dimensions == []
    assert executed.rewritten_question == "按月分析外周插管中心静脉导管的销售趋势。"
    assert _filter_value(executed, "商品名称") == "外周插管中心静脉导管"
    assert "经销商" not in executed.dimensions
    assert "空心纤维血液透析器" not in executed.rewritten_question
    assert "空心纤维血液透析器" not in str(executed.filters)
    assert "空心纤维血液透析器" not in str(executed.asl_template)
    assert "空心纤维血液透析器" not in retrieval.sql[-1]
    assert "外周插管中心静脉导管" in retrieval.sql[-1]
    assert executed.turn_admission is not None
    assert "空心纤维血液透析器" not in str(
        executed.turn_admission.context_after
    )
    assert "空心纤维血液透析器" not in second.answer

    events = await event_store.list_events(
        "tenant-1", "user-1", "app-1", conversation_id, limit=200
    )
    admission_event = next(
        item
        for item in reversed(events)
        if item.message_id == "m2"
        and item.event_type == SessionEventType.TURN_ADMISSION
    )
    merge_event = next(
        item
        for item in reversed(events)
        if item.message_id == "m2"
        and item.event_type == SessionEventType.CONTEXT_MERGE
    )
    assert admission_event.payload["turn_relation"] == "STANDALONE_NEW_TOPIC"
    assert admission_event.payload["inheritance_allowed"] is False
    assert admission_event.payload["new_thread_created"] is True
    assert merge_event.payload["context_conflicts"] == []
    assert "空心纤维血液透析器" not in str(merge_event.payload["context_after"])


def test_standalone_admission_rebases_a_precontaminated_semantic_frame():
    gate, previous, current, decision = _decision(
        "查询空心纤维血液透析器产品合作的经销商名单。",
        "按月分析外周插管中心静脉导管的销售趋势。",
        "standalone-precontaminated-frame",
    )
    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC

    contaminated = previous.model_copy(
        deep=True,
        update={
            "request_id": current.request_id,
            "original_question": current.original_question,
            "rewritten_question": current.rewritten_question,
            "primary_intent": current.primary_intent,
            "metrics": [item.model_copy(deep=True) for item in current.metrics],
        },
    )
    assert contaminated.entity == "经销商"
    assert "经销商" in contaminated.dimensions

    final = gate.apply_explicit_slot_protection(
        contaminated,
        current,
        decision,
    )

    assert final.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert final.entity is None
    assert final.dimensions == []
    assert final.fields == []
    assert _filter_value(final, "商品名称") == "外周插管中心静脉导管"
    assert "空心纤维血液透析器" not in str(final.filters)
    assert "空心纤维血液透析器" not in str(final.asl_template)


class _CountingModelClassifier:
    def __init__(self) -> None:
        self.rules = RuleBasedIntentClassifier()
        self.calls: list[str] = []

    async def classify(self, question, identity, conversation_id):
        self.calls.append(question)
        request = self.rules.classify(question, identity, conversation_id)
        request.intent_source = "STRUCTURED_MODEL"
        request.intent_confidence = 0.96
        request.assumptions.extend([
            "MODEL_QUESTION_COMPLETION_APPLIED",
            "MODEL_ENTITY_EXTRACTION_APPLIED",
        ])
        return request

    def merge_clarification(self, pending, answer):
        return self.rules.merge_clarification(pending, answer)


@pytest.mark.asyncio
async def test_complete_business_query_does_not_bypass_enabled_model_classifier():
    classifier = _CountingModelClassifier()
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=True
        ),
        classifier=classifier,
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app-1",
            conversation_id="model-required-complete-query",
            message_id="m1",
            question="查询本月销售额",
        ),
        IDENTITY,
    )

    assert response.status == "COMPLETED"
    assert classifier.calls == ["查询本月销售额"]
    assert response.intent_source == "STRUCTURED_MODEL"
