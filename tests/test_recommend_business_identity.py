"""A surrogate primary key does not hide a declared business identity."""
import pytest

from app.services.semantic_recommend import SemanticRecommendService
from test_semantic_recommend import base_reader, field


def company_reader(key='guoyao_code', with_id=True, comment='国药公司编码'):
    reader = base_reader(relations=[], fields={
        1: ([field(11, 'id', ftype='int')] if with_id else []) + [field(12, key, comment)],
        2: [field(21, 'id', ftype='int'), field(22, 'salesperson_code', '销售员编码'),
            field(23, key, '国药公司编码')],
    })
    reader._entities[0].update(name='国药公司', main_table_name='guoyao_company')
    reader._entities[1].update(name='销售员', main_table_name='salesperson')
    reader._tables[0]['name'] = 'guoyao_company'
    reader._tables[1]['name'] = 'salesperson'
    return reader


@pytest.mark.parametrize('suffix', ['code', 'no', 'key', 'id', 'uuid'])
@pytest.mark.parametrize('with_id', [True, False])
def test_business_identity_is_recommended_in_both_directions(suffix, with_id):
    key = 'guoyao_' + suffix
    service = SemanticRecommendService(company_reader(key, with_id), None)
    for source, target in [('et-1', 'et-2'), ('et-2', 'et-1')]:
        detected = service.detect_relations(81, source)
        assert [row['otherEntityTypeId'] for row in detected] == [target]
        rows = service.recommend_relations(81, source, target)
        assert len(rows) == 1
        row = rows[0]
        assert (row['sourceEntityTypeId'], row['targetEntityTypeId'], row['cardinality']) == ('et-1', 'et-2', '1:N')
        assert row['fieldPairs'][0]['sourceColumnName'] == row['fieldPairs'][0]['targetColumnName'] == key
        assert '，' not in row['semanticDescription'] and not row.get('indirect')


@pytest.mark.parametrize('comment', [' 国药公司 编码 ', '国药公司编号', '国药公司标识', '国药公司ID', '国药公司编码（业务编号）'])
def test_registered_identity_label_variations(comment):
    rows = SemanticRecommendService(company_reader(comment=comment), None).recommend_relations(81, 'et-1', 'et-2')
    assert len(rows) == 1 and rows[0]['cardinality'] == '1:N'


def test_other_business_entities_use_the_same_identity_rule():
    reader = company_reader(key='vendor_code', comment='供应商代码')
    reader._entities[0].update(name='供应商', main_table_name='vendor_registry')
    reader._tables[0]['name'] = 'vendor_registry'
    reader._fields[2][-1]['comment'] = '供应商代码'
    rows = SemanticRecommendService(reader, None).recommend_relations(81, 'et-1', 'et-2')
    assert len(rows) == 1 and rows[0]['sourceEntityName'] == '供应商'


@pytest.mark.parametrize('key,comment', [
    ('guoyao_code', '所属省份编码'), ('guoyao_code', '国药公司类型编码'),
    ('province_code', '国药公司编码'), ('guoyao_type_code', '国药公司类型编码'),
    ('status', '国药公司编码'), ('guoyao_code', None),
])
def test_incomplete_or_contradictory_identity_does_not_create_a_direct_relation(key, comment):
    service = SemanticRecommendService(company_reader(key=key, comment=comment), None)
    assert service.detect_relations(81, 'et-1') == []
    assert service.recommend_relations(81, 'et-1', 'et-2') == []


def test_shared_third_party_business_code_still_does_not_connect_two_entities():
    reader = company_reader()
    reader._entities[0].update(name='医院', main_table_name='hospital')
    reader._tables[0]['name'] = 'hospital'
    assert SemanticRecommendService(reader, None).recommend_relations(81, 'et-1', 'et-2') == []


@pytest.mark.parametrize('boundary', ['source', 'domain', 'type'])
def test_business_identity_preserves_existing_boundaries(boundary):
    reader = company_reader()
    if boundary == 'source':
        reader._entities[1]['data_source_id'] = reader._tables[1]['data_source_id'] = 59
    elif boundary == 'domain':
        reader._entities[0]['business_domain_id'] = 1
        reader._entities[1]['business_domain_id'] = 2
    else:
        reader._fields[2][-1]['type'] = 'bigint'
    assert SemanticRecommendService(reader, None).detect_relations(81, 'et-1') == []
