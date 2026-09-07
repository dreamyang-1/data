"""Current business acceptance, independent of the historical failing nodes."""
from datetime import date

import pytest

from app.domain.models import AnalysisOperator, PrimaryIntent, TrustedIdentity, TurnRelation
from app.intent import RuleBasedIntentClassifier
from app.services.dataset_followup import plan_dataset_followup
from app.services.turn_admission import TurnAdmissionGate

IDENTITY = TrustedIdentity(tenant_id='fixture-tenant', user_id='fixture-user')


@pytest.fixture(autouse=True)
def fixed_business_date(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 7)
    monkeypatch.setattr('app.intent.classifier.date', FixedDate)


def parse(text):
    return RuleBasedIntentClassifier().classify(text, IDENTITY, 'critical')


def turn(previous, text, *, pending=False):
    rules, gate = RuleBasedIntentClassifier(), TurnAdmissionGate()
    current = parse(text)
    previous.analysis_thread_id = 'fixture-task'
    decision = gate.evaluate(question=text, current=current, previous=previous, message_id='m2', pending=pending)
    merged = rules.merge_clarification(previous.model_copy(deep=True), text) if decision.inherit_business_context else current.model_copy(deep=True)
    result = gate.apply_explicit_slot_protection(merged, current, decision)
    return result, decision


def names(request):
    return [m.input for m in request.metrics]


def regions(request):
    return [f['value'] for f in request.filters if f['field'] in {'地区', '业务省份', '业务城市'}]


def test_c01_region_followup_preserves_metric_and_period():
    before = parse('上海最近一年销售额')
    after, decision = turn(before, '那江苏呢？')
    assert decision.inherit_business_context
    assert regions(after) == ['江苏省']
    assert names(after) == ['销售额']
    assert after.time_range == before.time_range


def test_c02_complete_new_hospital_task_does_not_inherit_sales():
    after, decision = turn(parse('上海最近一年销售额'), '江苏有哪些医院？')
    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert after.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert names(after) == []
    assert regions(after) == ['江苏省']
    assert not any(f['field'] == '商品名称' for f in after.filters)


def test_c03_add_preserves_both_previous_metrics():
    after, _ = turn(parse('销售额和销售量'), '再加订单笔数')
    assert names(after) == ['销售额', '销售量', '订单笔数']


def test_c04_replace_region_removes_previous_region():
    after, _ = turn(parse('上海销售额'), '换成江苏')
    assert regions(after) == ['江苏省']
    assert names(after) == ['销售额']


def test_c05_remove_metric_does_not_remove_remaining_metric():
    after, _ = turn(parse('销售额和订单笔数'), '不要订单笔数')
    assert names(after) == ['销售额']


def test_c06_clear_region_survives_next_grain_turn():
    cleared, _ = turn(parse('上海销售额'), '不限地区')
    after, _ = turn(cleared, '按季度')
    assert regions(cleared) == regions(after) == []
    assert names(after) == ['销售额']


def test_c07_pending_cannot_capture_complete_new_task_with_missing_execution_slot():
    previous = parse('上海销售额')
    previous.missing_slots = ['semantic_ambiguity']
    current = parse('江苏有哪些医院？')
    current.fields = []
    current.missing_slots = ['fields']
    decision = TurnAdmissionGate().evaluate(question=current.original_question, current=current, previous=previous, message_id='m2', pending=True)
    assert decision.relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert not decision.inherit_business_context


def test_c08_field_lineage_does_not_require_metric():
    request = parse('医院名称字段来自哪个表？')
    assert request.primary_intent == PrimaryIntent.DATA_LINEAGE
    assert 'metric' not in request.missing_slots


def test_c09_governed_default_display_is_used_without_field_question():
    # A controlled catalog fixture, not a claim that the production catalog
    # already publishes this policy. The missing-policy contrast stays blocked.
    from app.services import legacy_guards
    request = parse('列出TDC-3合作医院')
    request.fields = []
    policy = {'entity': '医院', 'version': 'fixture-v1', 'default_display_attributes': ['医院名称'], 'allowed_attributes': ['医院名称']}
    assert legacy_guards.apply_catalog_display_default(request, policy)
    assert request.fields == ['医院名称']
    assert 'fields' not in RuleBasedIntentClassifier().required_missing_slots(request)


def test_c10_display_limit_preserves_row_order():
    rows = [{'销售额': 2}, {'销售额': 9}]
    assert plan_dataset_followup('只看前5条', ['销售额'], rows) == {'type': 'limit', 'count': 5}


def test_c11_ranking_uses_requested_count_and_order():
    operation = plan_dataset_followup('销售额最高5名', ['销售额'], [{'销售额': 2}, {'销售额': 9}])
    assert operation == {'type': 'sort_limit', 'field': '销售额', 'descending': True, 'count': 5}


def test_c12_truncated_dataset_cannot_establish_global_top5():
    assert plan_dataset_followup('销售额最高5名', ['销售额'], [{'销售额': 2}], source_complete=False) is None


def test_c13_quantity_surface_is_not_lost_as_attribute():
    request = parse('销售数量')
    assert names(request) == ['销售量']
    assert 'metric' not in request.missing_slots


def test_c14_product_name_role_depends_on_requested_operation():
    bare = parse('商品名称')
    assert bare.missing_slots == ['semantic_ambiguity']
    assert bare.semantic_ambiguities[0].type == 'operation_intent'
    assert len(bare.semantic_ambiguities[0].candidates) == 2
    listing, grouping = parse('列出商品名称'), parse('按商品名称统计销售额')
    assert listing.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert '商品名称' in listing.fields
    assert '商品' in grouping.dimensions
    assert names(grouping) == ['销售额']


def test_c15_default_time_cannot_determine_turn_relation():
    previous, current = parse('上海销售额'), parse('查询订单笔数')
    gate = TurnAdmissionGate()
    first = gate.evaluate(question=current.original_question, current=current, previous=previous, message_id='m2')
    current.time_range = None
    current.missing_slots = ['time_range']
    second = gate.evaluate(question=current.original_question, current=current, previous=previous, message_id='m2')
    assert first.relation == second.relation == TurnRelation.STANDALONE_NEW_TOPIC
