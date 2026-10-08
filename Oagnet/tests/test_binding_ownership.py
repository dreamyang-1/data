"""Same labels retain catalog owner identity throughout recall and binding."""
import json
from types import SimpleNamespace as Obj

import pytest

from binding_ownership import resolve_entities
from structured_binding import bind, catalog_candidates
from vector_store import SearchResult


def fixture():
    entities = []
    for code, name, entity_id, prefix in [('dealer', '经销商', 10, 'D'), ('hospital', '医院', 20, 'H')]:
        entities.append(Obj(metadata={'entity_code': code, 'entity_name': name, 'entity_id': entity_id,
            'entity_alias': [name + '档案'], 'attributes': [
                {'attribute_id': entity_id + 1, 'attr_code': 'name', 'attr_name': '名称', 'field_mapping': code + '.name'},
                {'attribute_id': entity_id + 2, 'attr_code': 'level', 'attr_name': '等级', 'field_mapping': code + '.level',
                 'enum_values': [{'code': prefix + '0', 'name': '其他'}, {'code': prefix + '1', 'name': '重点'}]},
                {'attr_code': 'model', 'attr_name': '型号', 'field_mapping': code + '.model'},
            ]}))
    entities.append(Obj(metadata={'entity_code': 'sales_order', 'entity_name': '销售订单', 'attributes': []}))
    k = {'entities': entities, 'metrics': [Obj(metadata={'metric_code': 'sales', 'metric_name': '销售额',
            'source_dependency': {'bind_entity': ['sales_order']}})],
         '_vector_authorized_fields': [a['field_mapping'] for e in entities for a in e.metadata['attributes']],
         'entity_attribute_values': [Obj(metadata={'entity_code': 'dealer', 'attr_code': 'model',
                                                   'source_field': 'dealer.model', 'attr_value': 'TDC-3'})]}
    e = {'实体': ['经销商', '医院', '销售订单'], '指标': [], '维度': [],
         '展示字段': [{'entity': '经销商', 'field': '名称'}],
         '过滤条件': [{'entity': '经销商', 'field': '等级', 'op': '=', 'value': ['其他']}],
         '排序': [], '时间粒度': {'unit': None, 'time_range': None}, '限制': None, '输出要求': '默认输出表格'}
    p = {'subject': 'sales_order', 'display_fields': [{'index': 0, 'key': 'hospital.name'}],
         'filters': [{'index': 0, 'key': 'hospital.level', 'value_ids': [2]}]}
    return e, k, p


def run(e, k, p):
    return bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))))


@pytest.mark.parametrize('model_mode', ['wrong', 'error', 'missing'])
@pytest.mark.parametrize('owner', ['经销商', 'dealer', '经销商档案', '10'])
def test_same_attribute_and_enum_labels_correct_wrong_model_binding(owner, model_mode):
    e, k, p = fixture()
    e['展示字段'][0]['entity'] = e['过滤条件'][0]['entity'] = owner
    if model_mode == 'error':
        p['display_fields'][0]['error'] = p['filters'][0]['error'] = '无法区分'
    elif model_mode == 'missing':
        p['display_fields'] = p['filters'] = []
    ast, _ = run(e, k, p)
    assert ast['ambiguity'] == []
    assert ast['subject'] == {'entity': 'sales_order'}  # overall subject is not the attribute owner
    assert ast['dimensions'][0]['name'] == 'dealer.name'
    assert ast['filters'] == [{'field': 'dealer.level', 'operator': '=', 'value': 'D0'}]


@pytest.mark.parametrize('operator', ['=', 'IN', '!=', 'NOT IN'])
def test_multi_value_enum_binding_never_mixes_entity_codes(operator):
    e, k, p = fixture()
    e['过滤条件'][0].update(op=operator, value=['其他', '重点'])
    p['filters'][0]['value_ids'] = [2, 3]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'dealer.level', 'operator': 'NOT IN' if operator in {'!=', 'NOT IN'} else 'IN', 'value': ['D0', 'D1']}]


def test_multi_entity_predicates_resolve_independently():
    e, k, p = fixture()
    e['过滤条件'].append({'entity': '医院', 'field': '等级', 'op': '=', 'value': ['其他']})
    p['filters'].append({'index': 1, 'key': 'dealer.level', 'value_ids': [0]})
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert [(item['field'], item['value']) for item in ast['filters']] == [('dealer.level', 'D0'), ('hospital.level', 'H0')]


@pytest.mark.parametrize('target', ['dimensions', 'sort'])
def test_group_and_sort_attributes_use_their_own_entity_not_subject(target):
    e, k, p = fixture()
    e['指标'] = [{'name': '销售额'}]
    e['展示字段'] = []
    e['维度'] = [{'entity': '经销商', 'name': '等级'}]
    p['metrics'] = [{'index': 0, 'key': 'sales'}]
    p['dimensions'] = [{'index': 0, 'key': 'hospital.level'}]
    if target == 'sort':
        e['排序'] = [{'entity': '经销商', 'field': '等级', 'order': 'asc'}]
        p['sort'] = [{'index': 0, 'key': 'hospital.level'}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['name'] == 'dealer.level'
    if target == 'sort':
        assert ast['sort']['field'] == 'dealer.level'


def test_multi_owner_dimension_corrects_its_attribute_binding():
    e, k, p = fixture()
    e['指标'], e['展示字段'] = [{'name': '销售额'}], []
    e['维度'] = [{'entity': '经销商', 'name': '实体等级'}]
    k['dimensions'] = [Obj(metadata={'dim_code': 'entity_level', 'dim_name': '实体等级', 'bind_entities': [
        {'entity': 10, 'attr': 12, 'mappingTable': 'dealer', 'mappingColumn': 'level'},
        {'entity': 20, 'attr': 22, 'mappingTable': 'hospital', 'mappingColumn': 'level'}]})]
    p['metrics'] = [{'index': 0, 'key': 'sales'}]
    p['dimensions'] = [{'index': 0, 'key': 'entity_level', 'attr': 22}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['attr'] == 12


@pytest.mark.parametrize('section', ['展示字段', '过滤条件'])
def test_unqualified_homonym_without_owner_is_not_selected_by_model_rank(section):
    e, k, p = fixture()
    e[section][0].pop('entity')
    if section == '过滤条件':
        p['filters'][0].pop('value_ids')
    ast, _ = run(e, k, p)
    assert ast['ambiguity']
    assert any(item['field'].startswith(section) for item in ast['ambiguity'])


def test_top_level_entities_only_break_a_real_same_name_tie():
    e, k, p = fixture()
    e['实体'] = ['经销商']
    e['展示字段'][0].pop('entity')
    e['过滤条件'][0].pop('entity')
    ast, _ = run(e, k, p)
    assert not ast['ambiguity'] and ast['filters'][0]['field'] == 'dealer.level'


def test_wrong_attribute_role_can_still_recover_same_owner_model_number():
    e, k, p = fixture()
    e['过滤条件'][0].update(field='名称', value=['TDC-3'])
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'dealer.model', 'operator': '=', 'value': 'TDC-3'}]


def test_no_explicit_slot_owner_keeps_unique_cross_entity_value_recovery():
    e, k, p = fixture()
    e['过滤条件'] = [{'field': '商品名称', 'op': '=', 'value': ['TDC-3']}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity'] and ast['filters'][0]['field'] == 'dealer.model'


@pytest.mark.parametrize('fault', ['owner', 'unknown_owner', 'attribute'])
def test_value_record_cannot_lie_about_its_field_owner_or_attribute(fault):
    _, k, _ = fixture()
    record = k['entity_attribute_values'][0].metadata
    record['attr_code' if fault == 'attribute' else 'entity_code'] = {'owner': 'hospital', 'unknown_owner': 'unknown', 'attribute': 'level'}[fault]
    assert not any(v['value'] == 'TDC-3' for v in catalog_candidates(k)['values'])


def test_standalone_attribute_recall_retains_parent_owner():
    e, k, p = fixture()
    attrs = k['entities'][0].metadata.pop('attributes')
    k['attributes'] = [Obj(metadata=dict(attr, parent='dealer')) for attr in attrs]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity'] and ast['dimensions'][0]['name'] == 'dealer.name'


def test_entity_alias_collision_does_not_become_an_identity():
    e, k, _ = fixture()
    catalog = catalog_candidates(k)
    for meta in catalog['entities'].values():
        meta['entity_alias'] = ['档案']
    assert resolve_entities('档案', catalog['entities']) == set()
    assert resolve_entities('dealer', catalog['entities']) == {'dealer'}


@pytest.mark.parametrize('wrong_decision', ['keep', 'wrong_owner', 'missing'])
def test_explicit_shared_dictionary_owner_overrides_wrong_model_review(wrong_decision):
    from query_binding_review import review_bindings
    from test_query_binding_review import catalog, draft
    decision = {'bindings': [{'filter_index': 0, 'keep': True, 'reason': 'keep'}]}
    if wrong_decision == 'wrong_owner':
        decision['bindings'] = [{'filter_index': 0, 'choice_index': 0, 'bind_owner': True, 'reason': 'wrong hospital'}]
    elif wrong_decision == 'missing':
        decision['bindings'] = []
    content, _ = review_bindings(json.dumps(draft()), catalog(), 'audit only',
        {'过滤条件': [{'entity': '经销商', 'field': '省份', 'value': ['上海市']}]},
        Obj(invoke=lambda _: Obj(content=json.dumps(decision))), lambda *args: ['310000'], 81, 205)
    ast = json.loads(content)
    assert not ast['ambiguity']
    assert ast['filters'][0]['field'] == 'dealer.province_id'


def test_shared_dictionary_binding_survives_explicit_slot_owner_before_review():
    from test_query_binding_review import catalog
    e, _, p = fixture()
    k = catalog()
    k['entity_attribute_values'] = [Obj(metadata={'entity_code': 'province', 'source_field': 'dim_province.province_name', 'attr_value': '上海市'})]
    e['过滤条件'] = [{'entity': '经销商', 'field': '省份', 'op': '=', 'value': ['上海市']}]
    e['展示字段'] = [{'entity': '经销商', 'field': '经销商'}]
    p['display_fields'] = [{'index': 0, 'key': 'dealer.province_id'}]
    p['filters'] = [{'index': 0, 'key': 'dim_province.province_name'}]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity'] and ast['filters'][0]['field'] == 'dim_province.province_name'


def record(identity, kind, **meta):
    return SearchResult(id=identity, score=0.8, text=identity,
        metadata=dict(type=kind, semantic_model_id=81, business_domain_id=205, **meta))


def test_owner_specific_recall_finds_attribute_and_enum_hidden_by_global_top_k():
    from prompt_build import PromptBuilder
    d = record('dealer', 'entity', entity_code='dealer', entity_name='经销商', attributes=[])
    h = record('hospital', 'entity', entity_code='hospital', entity_name='医院', attributes=[])
    attr = record('dealer.level', 'attribute', parent='dealer', attr_code='level', attr_name='等级', field_mapping='dealer.level')
    value = record('dealer.value', 'entity_attribute_value', entity_code='dealer', entity_name='经销商', attr_code='level', attr_value='其他')
    calls = []

    class Store:
        def search(self, vector, *, top_k, where):
            calls.append(where)
            return [value] if "'entity_code':" in str(where) else []

        def get_by_where(self, where):
            text = str(where)
            if "'entity'" in text:
                return [d, h]
            if "'attribute'" in text:
                return [attr]
            return []

    builder = PromptBuilder(Store(), lambda _: [0.1], semantic_model_id=81, business_domain_ids=[205])
    builder.structured_extraction = {'过滤条件': [{'entity': '经销商', 'field': '等级', 'value': ['其他']}]}
    knowledge = builder.retrieve('same labels')
    assert attr in knowledge['attributes'] and d in knowledge['entities']
    assert value in knowledge['_ambiguity_candidates']['entity_attribute_value']
    owner_queries = [where for where in calls if "'entity_code':" in str(where)]
    assert owner_queries and all("'semantic_model_id': 81" in str(where) and "'business_domain_id'" in str(where) for where in owner_queries)


def test_owner_recall_rejects_foreign_scope_even_when_parent_name_matches():
    from prompt_build import PromptBuilder
    d = record('dealer', 'entity', entity_code='dealer', entity_name='经销商')
    foreign = record('value', 'entity_attribute_value', entity_code='dealer')
    foreign.metadata['semantic_model_id'] = 120
    class Store:
        def search(self, *args, **kwargs):
            return [foreign]
        def get_by_where(self, *args, **kwargs):
            return [d]
    builder = PromptBuilder(Store(), lambda _: [0.1], semantic_model_id=81, business_domain_ids=[205])
    builder.structured_extraction = {'过滤条件': [{'entity': '经销商', 'field': '等级', 'value': ['其他']}]}
    with pytest.raises(ValueError, match='SEMANTIC_SCOPE_MISMATCH'):
        builder._structured_owner_recall([], [])


@pytest.mark.parametrize('bad_key', [[], {}, None])
def test_invalid_model_key_cannot_crash_owner_validation(bad_key):
    e, k, p = fixture()
    e['展示字段'][0]['field'] = '未知字段'
    p['display_fields'][0]['key'] = bad_key
    ast, _ = run(e, k, p)
    assert ast['ambiguity']


@pytest.mark.parametrize('reverse', [False, True])
def test_shared_physical_field_keeps_both_logical_attribute_and_enum_owners(reverse):
    e, k, p = fixture()
    for entity in k['entities'][:2]:
        entity.metadata['attributes'][1]['field_mapping'] = 'shared.level'
    k['_vector_authorized_fields'].append('shared.level')
    if reverse:
        k['entities'].reverse()
    p['filters'][0].update(key='shared.level', value_ids=[2])
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'shared.level', 'operator': '=', 'value': 'D0'}]
    assert set(catalog_candidates(k)['fields']['shared.level']['owners']) == {'dealer', 'hospital'}


def test_shared_field_value_metadata_checks_the_right_logical_attribute():
    _, k, _ = fixture()
    first = k['entities'][0].metadata['attributes'][2]
    first['field_mapping'] = 'shared.model'
    k['entities'][1].metadata['attributes'].append(dict(first, attr_code='device_model'))
    k['_vector_authorized_fields'].append('shared.model')
    k['entity_attribute_values'][0].metadata['source_field'] = 'shared.model'
    values = catalog_candidates(k)['values']
    assert any(value['value'] == 'TDC-3' for value in values)


def test_having_does_not_shift_the_owner_of_subsequent_dictionary_predicate():
    from query_binding_review import review_bindings
    from test_query_binding_review import catalog
    e, _, p = fixture()
    k = catalog()
    k['metrics'][0].metadata.update(metric_name='已合作医院数', calc_formula='已合作医院数 = COUNT(DISTINCT hospital.hospital_id)')
    k['entity_attribute_values'] = [Obj(metadata={'entity_code': 'province', 'source_field': 'dim_province.province_name', 'attr_value': '上海市'})]
    e.update(指标=[{'name': '已合作医院数'}], 维度=[{'entity': '经销商', 'name': '经销商'}], 展示字段=[])
    e['过滤条件'] = [{'entity': '医院', 'field': '已合作医院数', 'op': '>', 'value': 3},
                       {'entity': '经销商', 'field': '省份', 'op': '=', 'value': ['上海市']}]
    p.update(metrics=[{'index': 0, 'key': 'cooperating_hospital_count'}],
             dimensions=[{'index': 0, 'key': 'dealer.province_id'}], display_fields=[],
             filters=[{'index': 0, 'key': 'cooperating_hospital_count'}, {'index': 1, 'key': 'dim_province.province_name'}])
    ast, _ = run(e, k, p)
    assert not ast['ambiguity'] and ast['having'] and k['_structured_filter_origins'] == [1]
    decision = Obj(invoke=lambda _: Obj(content=json.dumps({'bindings': [{'filter_index': 0, 'keep': True, 'reason': 'keep'}]})))
    result, _ = review_bindings(json.dumps(ast), k, 'audit', e, decision, lambda *args: ['310000'], 81, 205)
    assert not json.loads(result)['ambiguity']
    assert json.loads(result)['filters'][0]['field'] == 'dealer.province_id'


def test_same_enum_labels_without_entity_names_do_not_collapse_different_parents():
    from prompt_build import PromptBuilder
    values = [record(code, 'entity_attribute_value', entity_code=code, attr_code='level', attr_value='其他')
              for code in ['dealer', 'hospital']]
    assert len(PromptBuilder._rerank_exact_mentions('其他', values, 10)) == 2


def test_standalone_attributes_can_resolve_shared_dictionary_owner():
    from query_binding_review import review_bindings
    from test_query_binding_review import catalog, draft
    k = catalog()
    k['attributes'] = [Obj(metadata=dict(attr, parent=entity.metadata['entity_code']))
                       for entity in k['entities'] for attr in entity.metadata['attributes']]
    for entity in k['entities']:
        entity.metadata['attributes'] = []
    decision = Obj(invoke=lambda _: Obj(content=json.dumps({'bindings': []})))
    content, _ = review_bindings(json.dumps(draft()), k, 'audit',
        {'过滤条件': [{'entity': '经销商', 'field': '省份', 'value': ['上海市']}]},
        decision, lambda *args: ['310000'], 81, 205)
    assert not json.loads(content)['ambiguity']
    assert json.loads(content)['filters'][0]['field'] == 'dealer.province_id'


def test_repeated_relation_descriptions_do_not_make_a_known_dictionary_owner_ambiguous():
    from query_binding_review import review_bindings
    from test_query_binding_review import catalog, draft
    k = catalog()
    k['relations'].append(Obj(metadata=dict(k['relations'][1].metadata, description='duplicate published edge', relation_semantic='different label')))
    decision = Obj(invoke=lambda _: Obj(content=json.dumps({'bindings': []})))
    content, _ = review_bindings(json.dumps(draft()), k, 'audit',
        {'过滤条件': [{'entity': '经销商', 'field': '省份', 'value': ['上海市']}]},
        decision, lambda *args: ['310000'], 81, 205)
    assert not json.loads(content)['ambiguity']
    assert json.loads(content)['filters'][0]['field'] == 'dealer.province_id'
