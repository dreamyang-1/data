from __future__ import annotations

import pytest

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ContextMode,
    CurrentTurnFacts,
    PrimaryIntent,
    MetricRef,
    TurnAdmissionDecision,
    TurnRelation,
)
from app.services.orchestrator import DataAnalysisOrchestrator


def _ambiguous_relation_request() -> CanonicalAnalysisRequest:
    decision = TurnAdmissionDecision(
        relation=TurnRelation.AMBIGUOUS_RELATION,
        confidence=0.72,
        context_mode=ContextMode.NONE,
        current_turn_facts=CurrentTurnFacts(
            raw_query="分析数据",
            is_self_contained=False,
        ),
        context_before={
            "entity": "经销商",
            "metrics": ["含税销售总额"],
            "dimensions": ["经销商"],
            "fields": [],
            "filters": [
                {"field": "商品品牌", "operator": "EQ", "value": "费森尤斯"}
            ],
            "time_range": None,
            "comparison": None,
            "top_n": None,
        },
        needs_clarification=True,
    )
    return CanonicalAnalysisRequest(
        conversation_id="relation-clarification",
        tenant_id="tenant-1",
        user_id="user-1",
        original_question="分析数据",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        missing_slots=["turn_relation"],
        turn_relation=TurnRelation.AMBIGUOUS_RELATION,
        turn_admission=decision,
    )


def test_relation_clarification_restores_only_confirmed_active_context():
    pending = _ambiguous_relation_request()

    resolved = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        pending.model_copy(deep=True),
        clarification_answer="补充或修改上一轮问题",
    )

    assert resolved.turn_relation == TurnRelation.CURRENT_TOPIC_FOLLOWUP
    assert resolved.context_mode == ContextMode.CURRENT_THREAD
    assert resolved.missing_slots == []
    assert [item.input for item in resolved.metrics] == ["含税销售总额"]
    assert resolved.entity == "经销商"
    assert resolved.filters == [
        {"field": "商品品牌", "operator": "EQ", "value": "费森尤斯"}
    ]
    assert resolved.turn_admission.needs_clarification is False


@pytest.mark.parametrize(
    ("question", "expected_direction"),
    [
        ("区域医院覆盖率按从高到低排序", "DESC"),
        ("区域医院覆盖率按从大到小排序", "DESC"),
        ("区域医院覆盖率按从小到大排序", "ASC"),
    ],
)
def test_sort_relation_confirmation_restores_last_completed_contract(
    question,
    expected_direction,
):
    pending = _ambiguous_relation_request()
    pending.original_question = question
    pending.metrics = [MetricRef(input="区域医院覆盖率")]
    pending.dimensions = ["城市名称", "医院"]
    pending.turn_admission.current_turn_facts.raw_query = pending.original_question
    completed = CanonicalAnalysisRequest(
        conversation_id=pending.conversation_id,
        tenant_id=pending.tenant_id,
        user_id=pending.user_id,
        original_question="统计上海市各个经销商的区域医院覆盖率",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(
            input="区域医院覆盖率",
            canonical_name="区域医院覆盖率",
            metric_id="81:screening_area_hospital_coverage",
        )],
        entity="经销商",
        dimensions=["dealer"],
        filters=[{
            "field": "dim_city.city_name",
            "operator": "EQ",
            "value": "上海市",
        }],
        assumptions=["TIME_SCOPE=ALL_TIME"],
    )

    resolved = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        pending.model_copy(deep=True),
        clarification_answer="补充上一轮",
        completed_before_pending=completed,
    )

    assert resolved.turn_relation == TurnRelation.CURRENT_TOPIC_FOLLOWUP
    assert resolved.metrics[0].metric_id == "81:screening_area_hospital_coverage"
    assert resolved.dimensions == ["dealer"]
    assert resolved.filters == [{
        "field": "dim_city.city_name",
        "operator": "EQ",
        "value": "上海市",
    }]
    assert resolved.time_range is None
    assert AnalysisOperator.SORT in resolved.operators
    assert f"SORT_DIRECTION={expected_direction}" in resolved.assumptions
    assert "COMPLETED_FRAME_RESTORED_AFTER_RELATION_CONFIRMATION" in resolved.assumptions


def test_relation_clarification_can_keep_current_turn_standalone():
    pending = _ambiguous_relation_request()

    resolved = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        pending.model_copy(deep=True),
        clarification_answer="作为独立新问题",
    )

    assert resolved.turn_relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert resolved.context_mode == ContextMode.NONE
    assert resolved.metrics == []
    assert resolved.filters == []
    assert resolved.missing_slots == []


def test_relation_confirmation_replays_department_qualifier_over_previous_frame():
    pending = _ambiguous_relation_request()
    pending.original_question = "查询次要科室"
    pending.fields = ["商品名称", "适用科室"]
    pending.filters = [
        {"field": "product_dept_relation.relation_type", "operator": "=", "value": 1}
    ]
    pending.turn_admission.current_turn_facts.raw_query = "查询次要科室"
    pending.turn_admission.context_before.update({
        "entity": "产品",
        "metrics": [],
        "dimensions": ["产品"],
        "fields": ["商品名称", "适用科室"],
        "filters": [
            {"field": "适用科室类型", "operator": "EQ", "value": 1}
        ],
        "semantic_entity_mentions": ["TDC-3"],
    })

    resolved = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        pending.model_copy(deep=True),
        clarification_answer="补充上一轮",
    )

    assert resolved.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert resolved.metrics == []
    assert resolved.semantic_entity_mentions == ["TDC-3"]
    assert resolved.filters == [{
        "field": "适用科室类型", "operator": "EQ", "value": 2,
    }]
    assert resolved.rewritten_question == "查询 TDC-3 产品的次要适用科室"
    assert resolved.time_range is None


def test_relation_clarification_is_presented_as_a_bounded_choice():
    request = _ambiguous_relation_request()

    items = DataAnalysisOrchestrator._clarification_items(request)

    assert items == [{
        "slot": "turn_relation",
        "title": "对话关系",
        "question": "请确认这句话是在补充上一轮，还是一个独立新问题？",
        "options": ["补充或修改上一轮问题", "作为独立新问题"],
        "multi_select": False,
        "allow_free_text": True,
    }]
