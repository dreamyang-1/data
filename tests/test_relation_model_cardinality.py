"""Model cardinality drives direct recommendations without replacing field evidence."""
from copy import deepcopy
import json

import pytest

from app.services.semantic_recommend import SemanticRecommendService
from test_semantic_recommend import FakeGateway, base_reader, field


def recommend(payload, reader=None, *, enabled=True):
    gateway = FakeGateway(json.dumps(payload, ensure_ascii=False))
    service = SemanticRecommendService(reader or base_reader(relations=[]), gateway, llm_enabled=enabled)
    return service.recommend_relations(81, 'et-1', 'et-2'), gateway


@pytest.mark.parametrize('cardinality,display,source,target,clauses', [
    ('1:1', '1:1', 'et-1', 'et-2', 1),
    ('1:N', '1:N', 'et-1', 'et-2', 1),
    ('N:1', '1:N', 'et-2', 'et-1', 1),
    ('N:N', 'M:N', 'et-1', 'et-2', 2),
    ('M:N', 'M:N', 'et-1', 'et-2', 2),
    (' n : 1 ', '1:N', 'et-2', 'et-1', 1),
    ('1：1', '1:1', 'et-1', 'et-2', 1),
    ('一对一', '1:1', 'et-1', 'et-2', 1),
    ('一对多', '1:N', 'et-1', 'et-2', 1),
    ('多对一', '1:N', 'et-2', 'et-1', 1),
    ('多对多', 'M:N', 'et-1', 'et-2', 2),
])
def test_model_cardinality_updates_type_direction_and_description(cardinality, display, source, target, clauses):
    rows, _ = recommend([{'candidate': 1, 'cardinality': cardinality, 'relationName': '包含'}])
    assert len(rows) == 1
    row = rows[0]
    assert (row['cardinality'], row['sourceEntityTypeId'], row['targetEntityTypeId']) == (display, source, target)
    assert row['semanticDescription'].count('，') + 1 == clauses
    if source == 'et-2':
        assert row['fieldPairs'][0]['sourceTableName'] == 'sales_order'
        assert row['fieldPairs'][0]['targetTableName'] == 'customer'
        assert '1 个【订单】' in row['semanticDescription']
    else:
        assert row['fieldPairs'][0]['sourceTableName'] == 'customer'
        assert row['fieldPairs'][0]['targetTableName'] == 'sales_order'
        assert '1 个【客户】' in row['semanticDescription']


@pytest.mark.parametrize('value', [None, '', 'invalid', '0:0', True, 1, {}, []])
def test_missing_or_invalid_model_cardinality_keeps_rule_candidate(value):
    rows, _ = recommend([{'candidate': 1, 'cardinality': value, 'relationName': '拥有'}])
    assert len(rows) == 1 and rows[0]['cardinality'] == '1:N'
    assert rows[0]['relationName'] == '拥有'


def test_model_can_correct_type_without_a_valid_relation_name():
    rows, _ = recommend([{'candidate': 1, 'cardinality': '1:1', 'relationName': 'invalid'}])
    assert rows[0]['cardinality'] == '1:1'
    assert rows[0]['relationName'] == '关联'
    assert rows[0]['semanticDescription'] == '1 个【客户】关联1 个【订单】'


def test_model_cannot_change_field_evidence_or_invent_entities():
    original = SemanticRecommendService(base_reader(relations=[]), None).recommend_relations(81, 'et-1', 'et-2')[0]
    rows, _ = recommend([{'candidate': 1, 'cardinality': 'N:N', 'confirmed': False,
        'sourceEntityTypeId': 'invented', 'fieldPairs': [{'sourceTableName': 'invented'}],
        'semanticDescription': '包含第三个实体'}, {'candidate': 99, 'cardinality': '1:1'}])
    assert len(rows) == 1 and rows[0]['cardinality'] == 'M:N'
    assert rows[0]['fieldPairs'] == original['fieldPairs']
    assert rows[0]['sourceEntityTypeId'] == 'et-1'
    assert '第三个实体' not in rows[0]['semanticDescription']


def test_partial_batch_only_changes_the_referenced_candidate():
    reader = base_reader(relations=[], fields={
        1: [field(11, 'customer_code'), field(12, 'customer_no')],
        2: [field(21, 'customer_code'), field(22, 'customer_no')],
    })
    rows, _ = recommend([{'candidate': 2, 'cardinality': '1:1'}], reader)
    assert [row['cardinality'] for row in rows] == ['1:N', '1:1']


def test_existing_configuration_keeps_its_cardinality():
    reader = base_reader(relations=[{'id': 9, 'name': '拥有', 'category': '关联关系', 'type': '1:N',
        'source_entity_type_id': 'et-1', 'target_entity_type_id': 'et-2',
        'source_table_column_name': 'customer-id', 'target_table_column_name': 'sales_order-customer_id'}])
    before = deepcopy(reader._relations)
    rows, _ = recommend([{'candidate': 1, 'cardinality': 'N:N', 'relationName': '开设'}], reader)
    assert all(row['cardinality'] == '1:N' for row in rows)
    assert all('，' not in row['semanticDescription'] for row in rows)
    assert reader._relations == before


@pytest.mark.parametrize('answer', [None, '', 'not-json', '[]'])
def test_gateway_bad_response_keeps_direct_relation(answer):
    rows = SemanticRecommendService(base_reader(relations=[]), FakeGateway(answer)).recommend_relations(81, 'et-1', 'et-2')
    assert len(rows) == 1 and rows[0]['cardinality'] == '1:N'


def test_disabled_model_keeps_rule_candidate():
    rows, gateway = recommend([{'candidate': 1, 'cardinality': 'N:N'}], enabled=False)
    assert rows[0]['cardinality'] == '1:N' and not gateway.calls


def test_no_direct_evidence_does_not_create_model_only_relation():
    reader = base_reader(relations=[], fields={1: [field(11, 'status')], 2: [field(21, 'status')]})
    rows, gateway = recommend([{'candidate': 1, 'cardinality': 'N:N'}], reader)
    assert rows == [] and not gateway.calls


def test_prompt_uses_entity_meaning_and_field_evidence_without_rule_type_bias():
    _, gateway = recommend([{'candidate': 1, 'cardinality': '1:N'}])
    prompt = gateway.calls[0][1]
    assert '源实体【客户】' in prompt and '目标实体【订单】' in prompt
    assert 'customer.id' in prompt and 'sales_order.customer_id' in prompt
    assert 'cardinality' in prompt and 'N:1' in prompt and '1:1' in prompt and 'N:N' in prompt
    assert '实体名称' in prompt and '业务含义' in prompt
    assert '候选1（基数' not in prompt


def test_detection_prompt_contains_entity_ids_and_same_cardinality_guide():
    gateway = FakeGateway('[{"otherEntityTypeId":"et-2","cardinality":"M:N","confirmed":true}]')
    rows = SemanticRecommendService(base_reader(relations=[]), gateway).detect_relations(81, 'et-1')
    assert rows[0]['llmCardinality'] == 'N:N'
    prompt = gateway.calls[0][1]
    assert 'et-2' in prompt and '实体名称' in prompt and '业务含义' in prompt
