"""Direct relation evidence remains visible regardless of model review."""
import json

import pytest

from app.services.semantic_recommend import SemanticRecommendService
from test_semantic_recommend import FakeGateway, base_reader, field


def city_reader(table='dim_city', key='city_code', with_id=True):
    reader = base_reader(relations=[], fields={
        1: ([field(11, 'id', ftype='bigint')] if with_id else []) + [field(12, key, '城市编码')],
        2: [field(21, 'id', ftype='bigint'), field(22, key, '所在城市编码')],
    })
    reader._entities[0].update(name='城市', main_table_name=table)
    reader._entities[1].update(name='经销商', main_table_name='distributor')
    reader._tables[0]['name'] = table
    reader._tables[1]['name'] = 'distributor'
    return reader


@pytest.mark.parametrize('table,key,with_id', [
    ('dim_city', 'city_code', True), ('dim_city', 'city_code', False),
    ('tb_city', 'city_code', True), ('t_city', 'city_code', True),
    ('dim_city', 'city_id', True), ('dim_city', 'city_uuid', True),
    ('dim_city', 'CITY_CODE', True), ('city', 'city_code', True),
])
def test_business_key_survives_table_prefix_and_surrogate_id(table, key, with_id):
    reader = city_reader(table, key, with_id)
    service = SemanticRecommendService(reader, None)
    for source, target in [('et-1', 'et-2'), ('et-2', 'et-1')]:
        assert [r['otherEntityTypeId'] for r in service.detect_relations(81, source)] == [target]
        rows = service.recommend_relations(81, source, target)
        assert len(rows) == 1
        row = rows[0]
        assert (row['sourceEntityTypeId'], row['targetEntityTypeId']) == ('et-1', 'et-2')
        assert row['cardinality'] == '1:N'
        assert row['fieldPairs'][0]['sourceColumnName'] == key
        assert row['fieldPairs'][0]['targetColumnName'] == key
        assert '，' not in row['semanticDescription']


@pytest.mark.parametrize('key', ['customer_type_id', 'customer_group_id', 'customer_type_code'])
def test_longer_business_name_does_not_point_to_customer(key):
    reader = base_reader(relations=[], fields={
        1: [field(11, 'id', ftype='bigint')],
        2: [field(21, 'id', ftype='bigint'), field(22, key, '客户类型标识', 'bigint')],
    })
    service = SemanticRecommendService(reader, None)
    assert service.detect_relations(81, 'et-1') == []
    assert service.recommend_relations(81, 'et-1', 'et-2') == []
    # 明确引用对方字段时仍然推荐，不因字段名字带额外词而丢弃直接证据。
    reader._fields[2][1]['comment'] = '关联 customer.id'
    assert len(service.recommend_relations(81, 'et-1', 'et-2')) == 1


@pytest.mark.parametrize('key', ['province_id', 'province_code', 'province_uuid'])
def test_common_third_dimension_is_not_a_direct_relation(key):
    service = SemanticRecommendService(city_reader(key=key), None)
    assert service.detect_relations(81, 'et-1') == []
    assert service.recommend_relations(81, 'et-1', 'et-2') == []


@pytest.mark.parametrize('review', [
    {'confirmed': False}, {}, {'confirmed': None}, {'confirmed': 'false'},
    {'confirmed': True},
])
def test_model_review_never_removes_direct_relation(review):
    reader = base_reader(relations=[], fields={
        1: [field(11, 'customer_code')], 2: [field(21, 'customer_code')],
    })
    gateway = FakeGateway(json.dumps([{'otherEntityTypeId': 'et-2', **review}]))
    service = SemanticRecommendService(reader, gateway)
    detected = service.detect_relations(81, 'et-1')
    assert len(detected) == len(service.recommend_relations(81, 'et-1', 'et-2')) == 1
    assert detected[0]['inferred'] is True
    expected = review.get('confirmed') if isinstance(review.get('confirmed'), bool) else None
    assert detected[0]['llmConfirmed'] is expected


def test_same_key_case_difference_does_not_add_surrogate_join():
    reader = base_reader(relations=[], fields={
        1: [field(11, 'id'), field(12, 'CUSTOMER_ID')],
        2: [field(21, 'id'), field(22, 'customer_id')],
    })
    rows = SemanticRecommendService(reader, None).recommend_relations(81, 'et-1', 'et-2')
    assert len(rows) == 1
    assert rows[0]['fieldPairs'][0]['sourceColumnName'] == 'CUSTOMER_ID'


def test_prefixed_table_pk_fk_still_detected_without_same_named_key():
    reader = city_reader(key='city_id')
    reader._fields[1] = [field(11, 'id', ftype='bigint')]
    rows = SemanticRecommendService(reader, None).recommend_relations(81, 'et-1', 'et-2')
    assert len(rows) == 1
    assert rows[0]['fieldPairs'][0]['sourceColumnName'] == 'id'
    assert rows[0]['fieldPairs'][0]['targetColumnName'] == 'city_id'
