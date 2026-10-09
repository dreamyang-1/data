"""The alias contract is entity- and slot-independent, not department-specific."""
import json
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest

from structured_binding import bind


SUBJECTS = [
    ('dealer', '经销商', '渠道客户'), ('hospital', '医院', '医疗客户'),
    ('product', '商品', '货品'), ('manufacturer', '厂家', '供应厂商'),
    ('salesperson', '业务员', '销售人员'), ('company', '销售公司', '销售机构'),
    ('department', '科室', '适用科室'),
]


def fixture(subject, slot, mode='wrong'):
    code, name, alias = subject
    entities = []
    for owner, label, alternate in [subject, ('neighbor', '相邻实体', '相邻档案')]:
        entities.append(Obj(metadata={'entity_code': owner, 'entity_id': owner + '-id',
            'entity_name': label, 'entity_alias': [alternate], 'attributes': [
                {'attr_code': 'code', 'attr_name': '编码', 'field_mapping': owner + '.code'},
                {'attr_code': 'name', 'attr_name': '名称', 'field_mapping': owner + '.name', 'is_main_attribute': True},
                {'attr_code': 'status', 'attr_name': '状态', 'field_mapping': owner + '.status',
                 'enum_values': [{'code': owner + '-0', 'name': '其他'}, {'code': owner + '-1', 'name': '重点'}]},
            ]}))
    entities.append(Obj(metadata={'entity_code': 'orders', 'entity_name': '业务记录', 'attributes': []}))
    identity = code + '_identity'
    knowledge = {'entities': entities, 'dimensions': [Obj(metadata={
        'dim_code': identity, 'dim_name': name, 'bind_entities': [
            {'entity': code + '-id', 'attr': code + '-code', 'mappingTable': code, 'mappingColumn': 'code'}]})],
        'metrics': [Obj(metadata={'metric_code': 'record_count', 'metric_name': '记录数',
            'source_dependency': {'bind_entity': ['orders']}})],
        '_vector_authorized_fields': [a['field_mapping'] for e in entities for a in e.metadata['attributes']],
        'entity_attribute_values': [Obj(metadata={'entity_code': owner, 'attr_code': 'name',
            'source_field': owner + '.name', 'attr_value': value})
            for owner in [code, 'neighbor'] for value in ['样本甲', '样本乙']]}
    e = {'实体': [name, '相邻实体', '业务记录'], '指标': [{'name': '记录数'}],
         '维度': [], '展示字段': [], '过滤条件': [], '排序': [],
         '时间粒度': {'unit': None, 'time_range': None}, '限制': 5, '输出要求': '默认输出表格'}
    p = {'subject': 'orders', 'metrics': [{'index': 0, 'key': 'record_count'}]}
    section = {'filters': '过滤条件', 'display_fields': '展示字段', 'dimensions': '维度', 'sort': '排序'}[slot]
    item = {'field': alias}
    if slot == 'filters':
        item.update(op='IN', value=['样本甲', '样本乙'])
    if slot == 'sort':
        item['order'] = 'asc'
    e[section] = [item]
    if slot in {'filters', 'sort'}:
        e['维度'] = [{'entity': name, 'name': '名称'}]
        p['dimensions'] = [{'index': 0, 'key': code + '.name'}]
    if slot == 'display_fields':
        e['指标'] = []
        p['metrics'] = []
    p[slot] = [{'index': 0, 'key': 'neighbor.name'}]
    if mode == 'missing':
        p[slot] = []
    elif mode == 'error':
        p[slot][0]['error'] = '未绑定'
    return e, knowledge, p, code, identity


def run(e, k, p):
    before = deepcopy(e)
    ast, repairs = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    assert e == before
    assert ast['time_context'] is None and ast['limit'] == e['限制']
    return ast, repairs


@pytest.mark.parametrize('subject', SUBJECTS, ids=[s[0] for s in SUBJECTS])
@pytest.mark.parametrize('slot', ['filters', 'display_fields', 'dimensions', 'sort'])
@pytest.mark.parametrize('mode', ['wrong', 'missing', 'error'])
def test_entity_alias_binding_matrix(subject, slot, mode):
    e, k, p, code, identity = fixture(subject, slot, mode)
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    if slot == 'filters':
        assert ast['filters'] == [{'field': code + '.name', 'operator': 'IN', 'value': ['样本甲', '样本乙']}]
    elif slot == 'dimensions':
        assert ast['dimensions'][0]['name'] == identity
    elif slot == 'display_fields':
        assert ast['dimensions'][0]['name'] == code + '.name'
    else:
        assert ast['sort']['field'] in {code + '.name', code + '.code', identity}
        assert ast['sort']['direction'] == 'ASC'


@pytest.mark.parametrize('encoding', ['list', 'json', 'separated'])
@pytest.mark.parametrize('slot', ['metrics', 'dimensions', 'filters', 'sort'])
def test_registered_synonyms_correct_wrong_model_keys(encoding, slot):
    e, k, p, code, identity = fixture(SUBJECTS[0], 'filters')
    synonyms = ['统计别名'] if encoding == 'list' else '["统计别名"]' if encoding == 'json' else '统计别名,另一别名'
    if slot == 'metrics':
        k['metrics'][0].metadata['synonyms'] = synonyms
        k['metrics'].append(Obj(metadata={'metric_code': 'wrong_count', 'metric_name': '其他统计'}))
        e['指标'] = [{'name': '统计别名'}]
        p['metrics'][0]['key'] = 'wrong_count'
    else:
        k['dimensions'][0].metadata['synonyms'] = synonyms
        section = {'dimensions': '维度', 'filters': '过滤条件', 'sort': '排序'}[slot]
        e[section] = [{'field': '统计别名', **({'op': '=', 'value': ['样本甲']} if slot == 'filters' else
            {'order': 'desc'} if slot == 'sort' else {})}]
        p[slot] = [{'index': 0, 'key': 'neighbor.name'}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    if slot == 'metrics':
        assert ast['metrics'][0]['name'] == 'record_count'
    elif slot == 'dimensions':
        assert ast['dimensions'][0]['name'] == identity
    elif slot == 'filters':
        assert ast['filters'][0]['field'] == code + '.name'
    else:
        assert ast['sort']['field'] in {identity, code + '.code'}


@pytest.mark.parametrize('slot', ['dimensions', 'sort', 'display_fields'])
def test_shared_entity_alias_is_not_selected_by_model_rank(slot):
    e, k, p, _, _ = fixture(SUBJECTS[0], slot)
    k['entities'][1].metadata['entity_alias'] = [SUBJECTS[0][2]]
    ast, _ = run(e, k, p)
    assert ast['ambiguity']


@pytest.mark.parametrize('owner', ['未发布实体', '相邻档案,渠道客户'])
def test_unknown_explicit_owner_cannot_fall_back_to_global_value_match(owner):
    e, k, p, _, _ = fixture(SUBJECTS[0], 'filters')
    e['过滤条件'][0]['entity'] = owner
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and not ast['filters']


def test_exact_standard_value_cannot_be_replaced_by_another_same_field_value_id():
    e, k, p, code, _ = fixture(SUBJECTS[0], 'filters')
    e['过滤条件'] = [{'entity': '经销商', 'field': '名称', 'op': '=', 'value': ['样本甲']}]
    p['filters'] = [{'index': 0, 'key': code + '.name', 'value_ids': [1]}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': code + '.name', 'operator': '=', 'value': '样本甲'}]


@pytest.mark.parametrize('section,key', [('指标', 'metrics'), ('维度', 'dimensions'), ('排序', 'sort')])
def test_shared_registered_synonym_remains_ambiguous(section, key):
    e, k, p, _, _ = fixture(SUBJECTS[0], 'filters')
    pool = 'metrics' if key == 'metrics' else 'dimensions'
    first = k[pool][0].metadata
    first['synonyms'] = ['共享统计别名']
    second = deepcopy(first)
    second['metric_code' if pool == 'metrics' else 'dim_code'] = 'another_meaning'
    second['metric_name' if pool == 'metrics' else 'dim_name'] = '另一业务含义'
    k[pool].append(Obj(metadata=second))
    e[section] = [{'name': '共享统计别名', **({'order': 'desc'} if key == 'sort' else {})}]
    p[key] = [{'index': 0, 'key': first['metric_code' if pool == 'metrics' else 'dim_code']}]
    ast, _ = run(e, k, p)
    assert ast['ambiguity']


@pytest.mark.parametrize('slot', ['filters', 'dimensions', 'sort', 'display_fields'])
def test_alias_owner_recall_does_not_depend_on_broad_top_k(slot):
    from prompt_build import PromptBuilder
    from vector_store import SearchResult
    e, k, _, code, _ = fixture(SUBJECTS[0], slot)
    records = [SearchResult(id=str(i), text='', score=1.0, metadata=dict(entity.metadata,
        type='entity', semantic_model_id=7, business_domain_id=9))
        for i, entity in enumerate(k['entities'])]
    attr = SearchResult(id='attr', text='', score=1.0, metadata=dict(
        k['entities'][0].metadata['attributes'][1], parent=code,
        type='attribute', semantic_model_id=7, business_domain_id=9))
    class Store:
        def get_by_where(self, where):
            return [attr] if "'attribute'" in str(where) else records
        def search(self, *args, **kwargs):
            return []
    builder = PromptBuilder(Store(), lambda _: [0.1], semantic_model_id=7, business_domain_ids=[9])
    builder.structured_extraction = {section: rows for section, rows in e.items()
        if section == {'filters': '过滤条件', 'dimensions': '维度', 'sort': '排序', 'display_fields': '展示字段'}[slot]}
    entities, attributes, _ = builder._structured_owner_recall([], [])
    assert attr in attributes and records[0] in entities


@pytest.mark.parametrize('qualified', [False, True])
@pytest.mark.parametrize('encoding', ['list', 'json'])
def test_attribute_synonyms_retain_their_parent(qualified, encoding):
    e, k, p, code, _ = fixture(SUBJECTS[0], 'filters')
    for entity in k['entities'][:2]:
        entity.metadata['attributes'][2]['synonyms'] = ['档案状态'] if encoding == 'list' else '["档案状态"]'
    e['过滤条件'] = [{'field': '渠道客户.档案状态' if qualified else '档案状态',
        **({} if qualified else {'entity': '渠道客户'}), 'op': '=', 'value': ['其他']}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': code + '.status', 'operator': '=', 'value': code + '-0'}]


def test_canonical_attribute_name_precedes_an_unrelated_attribute_synonym():
    e, k, p, code, _ = fixture(SUBJECTS[0], 'filters')
    k['entities'][0].metadata['attributes'][2]['attr_name'] = '注册状态'
    k['entities'][1].metadata['attributes'][2]['synonyms'] = ['注册状态']
    e['过滤条件'] = [{'field': '注册状态', 'op': '=', 'value': ['其他']}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'][0]['field'] == code + '.status'


@pytest.mark.parametrize('slot', ['dimensions', 'sort'])
def test_canonical_entity_name_precedes_another_entity_alias(slot):
    e, k, p, code, identity = fixture(SUBJECTS[0], slot)
    k['entities'][1].metadata['entity_alias'] = ['经销商']
    e['维度' if slot == 'dimensions' else '排序'][0]['field'] = '经销商'
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    if slot == 'dimensions':
        assert ast['dimensions'][0]['name'] == identity
    else:
        assert ast['sort']['field'] in {identity, code + '.code', code + '.name'}


def test_qualified_canonical_entity_does_not_bind_another_entity_alias():
    e, k, p, code, _ = fixture(SUBJECTS[0], 'filters')
    k['entities'][1].metadata['entity_alias'] = ['经销商']
    e['过滤条件'] = [{'field': '经销商.状态', 'op': '=', 'value': ['其他']}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'][0]['field'] == code + '.status'


def test_entity_named_dimension_with_foreign_bindings_is_not_owner_proof():
    e, k, p, code, _ = fixture(SUBJECTS[0], 'dimensions')
    k['dimensions'][0].metadata.update(dim_code=code, bind_entities=[
        {'entity': 'neighbor-id', 'attr': 'neighbor-code', 'mappingTable': 'neighbor', 'mappingColumn': 'code'}])
    p['dimensions'][0]['key'] = code
    ast, _ = run(e, k, p)
    assert ast['ambiguity']


def test_multiple_main_attributes_are_not_selected_by_model_rank():
    e, k, p, code, _ = fixture(SUBJECTS[0], 'display_fields')
    k['entities'][0].metadata['attributes'][0]['is_main_attribute'] = True
    p['display_fields'][0]['key'] = code + '.name'
    ast, _ = run(e, k, p)
    assert ast['ambiguity']


def test_entity_alias_does_not_create_name_grouping_without_a_published_identity_dimension():
    e, k, p, code, _ = fixture(SUBJECTS[0], 'dimensions')
    k['dimensions'] = []
    p['dimensions'][0]['key'] = 'neighbor.name'
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] and not ast['dimensions']


@pytest.mark.parametrize('operator', ['=', 'IN', '!=', 'NOT IN'])
def test_multi_value_same_field_model_ids_cannot_change_exact_literals(operator):
    e, k, p, code, _ = fixture(SUBJECTS[0], 'filters')
    e['过滤条件'] = [{'entity': '经销商', 'field': '名称', 'op': operator, 'value': ['样本甲', '样本乙']}]
    p['filters'] = [{'index': 0, 'key': code + '.name', 'value_ids': [1, 0]}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': code + '.name',
        'operator': 'NOT IN' if operator in {'!=', 'NOT IN'} else 'IN', 'value': ['样本甲', '样本乙']}]


@pytest.mark.parametrize('slot', ['field', 'filter'])
@pytest.mark.parametrize('conflict', ['different_parent', 'different_field', 'duplicate_copy'])
def test_semantic_display_does_not_collapse_parent_or_field_identity(monkeypatch, slot, conflict):
    import api
    records = []
    for index in range(2):
        owner = 'dealer' if index == 0 or conflict != 'different_parent' else 'hospital'
        field = owner + ('.legacy_status' if index and conflict == 'different_field' else '.status')
        records.append(Obj(id='record-' + str(index), score=0.99 - index / 10, metadata={
            'semantic_model_id': 7, 'business_domain_id': 9,
            'type': 'attribute' if slot == 'field' else 'entity_attribute_value',
            'parent': owner, 'entity_code': owner, 'attr_code': 'status', 'attr_name': '状态',
            'field_mapping': field, 'source_field': field, 'attr_value': '其他'}))
    monkeypatch.setattr(api, '_store', Obj(search=lambda *a, **k: records, find_exact=lambda *a: []))
    monkeypatch.setattr(api, 'embed_query', lambda text: [0.1])
    response = api.semantic_display_elements_resolve(api.SemanticDisplayResolveRequest(
        semantic_model_id=7, business_domain_ids=[9], candidates=[{
            'candidate_id': 'slot', 'slot': slot, 'value': '状态' if slot == 'field' else '其他'}]))
    assert len(response.matches) == (1 if conflict == 'duplicate_copy' else 0)


@pytest.mark.parametrize('hint', ['dealer.status', '经销商.状态', '渠道客户.状态'])
@pytest.mark.parametrize('owner,expected', [('dealer', True), ('hospital', False)])
def test_semantic_display_qualified_filter_hint_cannot_match_another_parent(hint, owner, expected):
    import api
    meta = {'entity_code': owner, 'entity_name': '经销商' if owner == 'dealer' else '医院',
        'entity_alias': ['渠道客户'] if owner == 'dealer' else [],
        'attr_code': 'status', 'attr_name': '状态', 'source_field': owner + '.status'}
    assert api._semantic_filter_field_matches(hint, meta) is expected


@pytest.mark.parametrize('declared', ['explicit', 'alias'])
@pytest.mark.parametrize('operator', ['=', '!=', 'IN', 'NOT IN'])
def test_relation_review_preserves_already_owned_name_predicates_without_model_guessing(declared, operator):
    from query_binding_review import review_bindings
    from test_name_filter_execution import catalog
    k = catalog()
    k['entities'][0].metadata['entity_alias'] = ['货品']
    ast = {'version': '2.0', 'intent': 'query', 'subject': {'entity': 'dealer'}, 'metrics': [],
        'dimensions': [{'name': 'dealer.dealer_name'}], 'filters': [{
            'field': 'product.product_name', 'operator': operator,
            'value': ['样本甲'] if operator in {'IN', 'NOT IN'} else '样本甲'}],
        'sort': None, 'limit': None, 'time_context': None, 'having': [], 'ambiguity': []}
    original = {'field': '名称', 'entity': 'product'} if declared == 'explicit' else {'field': '货品'}
    original.update(op=operator, value=['样本甲'])
    def forbidden(*args):
        pytest.fail('an already owned name predicate requires neither a model decision nor foreign-key enumeration')
    result, repairs = review_bindings(json.dumps(ast), k, 'not used', {'过滤条件': [original]},
        Obj(invoke=forbidden), forbidden, 7, 9)
    assert json.loads(result) == ast and repairs == []
