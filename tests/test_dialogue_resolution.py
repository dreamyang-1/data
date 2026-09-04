from __future__ import annotations

from app.domain.models import (
    CanonicalAnalysisRequest,
    ContextMode,
    CurrentTurnFacts,
    PrimaryIntent,
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
