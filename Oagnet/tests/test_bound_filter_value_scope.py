"""A resolved filter uses its own value domain, including published dictionaries."""
import json
from types import SimpleNamespace as Obj

import pytest

from query_binding_review import review_bindings
from structured_binding import bind, catalog_candidates


def case(owner='hospital', entity='医院', dictionary='province', other='city',
         label='业务省', surface='上海', canonical='上海市'):
    def entity_row(code, name, attributes):
        return Obj(metadata={'entity_code': code, 'entity_name': name,
                             'business_domain_id': 205, 'attributes': attributes})

    def attribute(code, name, field):
        return {'attr_code': code, 'attr_name': name, 'field_mapping': field}

    source = f'{owner}.{dictionary}_id'
    name_field = f'dim_{dictionary}.name'
    other_name = f'dim_{other}.name'
    knowledge = {
        'entities': [entity_row(owner, entity, [
            attribute('name', '名称', owner + '.name'),
            attribute(dictionary + '_id', '首选字典编码', source),
            attribute(other + '_id', '其他字典编码', f'{owner}.{other}_id'),
            attribute('status', '状态', owner + '.status')]),
            *[entity_row(code, code, [attribute('id', '编码', f'dim_{code}.id'),
                                     attribute('name', '字典名称', f'dim_{code}.name')])
              for code in (dictionary, other)]],
        'relations': [Obj(metadata={'join_key': {'source_field': f'{owner}.{code}_id',
                                               'target_field': f'dim_{code}.id'}})
                      for code in (dictionary, other)],
        'metrics': [], 'dimensions': [],
        'entity_attribute_values': [Obj(metadata={'entity_code': code, 'attr_code': 'name',
            'source_field': f'dim_{code}.name', 'attr_value': canonical})
            for code in (dictionary, other)],
    }
    knowledge['_vector_authorized_fields'] = [attr['field_mapping']
        for row in knowledge['entities'] for attr in row.metadata['attributes']]
    # An explicit detail column keeps these tests independent of display-only
    # query handling; their subject is filter identity and value ownership.
    extraction = {'意图': '明细查询', '实体': [entity], '指标': [], '维度': [owner + '.name'],
        '展示字段': [{'entity': entity, 'field': '名称'}],
        '过滤条件': [{'entity': entity, 'field': label, 'op': '=', 'value': [surface]}],
        '排序': [], '时间粒度': {'unit': None, 'time_range': None}, '限制': None}
    decision = {'subject': owner, 'dimensions': [{'index': 0, 'key': owner + '.name'}],
                'display_fields': [{'index': 0, 'key': owner + '.name'}],
                'filters': [{'index': 0, 'key': source}]}
    return extraction, knowledge, decision, source, name_field, other_name


def run(extraction, knowledge, decision):
    return bind(extraction, knowledge, Obj(invoke=lambda _: Obj(content=json.dumps(decision))))[0]


@pytest.mark.parametrize('args', [
    ('hospital', '医院', 'province', 'city', '业务省', '上海', '上海市'),
    ('dealer', '经销商', 'province', 'city', '关联省份档案', '上海', '上海市'),
    ('asset', '设备', 'category', 'grade', '配置分类', '重点', '重点'),
])
def test_resolved_foreign_key_disambiguates_same_label_dictionaries(args):
    extraction, knowledge, decision, source, name_field, _ = case(*args)
    ast = run(extraction, knowledge, decision)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': name_field, 'operator': '=', 'value': args[-1]}]
    # Exercise the next handoff too: the dictionary value returns to this owner/key.
    resolved, _ = review_bindings(json.dumps(ast), knowledge, '', extraction,
        Obj(invoke=lambda _: pytest.fail('explicit owner needs no further model')),
        lambda *args: ['K1'], 81, 205, structured_only=True, relationship_required=False)
    assert json.loads(resolved)['filters'] == [{'field': source, 'operator': '=', 'value': 'K1'}]


def test_value_id_from_other_dictionary_cannot_override_resolved_field():
    extraction, knowledge, decision, _, name_field, other = case()
    wrong_id = next(v['id'] for v in catalog_candidates(knowledge)['values'] if v['field'] == other)
    decision['filters'][0]['value_ids'] = [wrong_id]
    ast = run(extraction, knowledge, decision)
    assert not ast['ambiguity'] and ast['filters'][0]['field'] == name_field


@pytest.mark.parametrize('current_has_value', [False, True])
def test_text_field_expands_only_when_current_field_has_no_value(current_has_value):
    extraction, knowledge, decision, *_ = case()
    extraction['过滤条件'][0].update(field='名称', value=['正常'])
    knowledge['entity_attribute_values'].append(Obj(metadata={'entity_code': 'hospital',
        'attr_code': 'status', 'source_field': 'hospital.status', 'attr_value': '正常'}))
    decision['filters'][0]['key'] = 'hospital.name'
    if current_has_value:
        knowledge['entity_attribute_values'].append(Obj(metadata={'entity_code': 'hospital',
            'attr_code': 'name', 'source_field': 'hospital.name', 'attr_value': '正常'}))
    ast = run(extraction, knowledge, decision)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': 'hospital.name' if current_has_value else 'hospital.status',
                               'operator': '=', 'value': '正常'}]


@pytest.mark.parametrize('missing', ['relation', 'authorized_field', 'value'])
def test_empty_bound_dictionary_can_expand_within_authorized_scope(missing):
    extraction, knowledge, decision, _, name_field, other = case()
    if missing == 'relation':
        knowledge['relations'] = knowledge['relations'][1:]
    elif missing == 'authorized_field':
        knowledge['_vector_authorized_fields'].remove(name_field)
    else:
        knowledge['entity_attribute_values'] = knowledge['entity_attribute_values'][1:]
    ast = run(extraction, knowledge, decision)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': other, 'operator': '=', 'value': '上海市'}]


def test_partial_in_match_does_not_switch_to_another_dictionary():
    extraction, knowledge, decision, _, _, other = case()
    extraction['过滤条件'][0].update(op='IN', value=['上海', '北京'])
    knowledge['entity_attribute_values'].append(Obj(metadata={'entity_code': 'city',
        'attr_code': 'name', 'source_field': other, 'attr_value': '北京市'}))
    ast = run(extraction, knowledge, decision)
    assert ast['ambiguity'] and not ast['filters']


@pytest.mark.parametrize('fault', ['unauthorized', 'unrelated_owner', 'ambiguous'])
def test_expansion_still_requires_unique_authorized_owner_scoped_evidence(fault):
    extraction, knowledge, decision, _, _, other = case()
    knowledge['entity_attribute_values'] = knowledge['entity_attribute_values'][1:]
    if fault == 'unauthorized':
        knowledge['_vector_authorized_fields'].remove(other)
    elif fault == 'unrelated_owner':
        knowledge['relations'] = knowledge['relations'][:1]
    else:
        knowledge['entity_attribute_values'].append(Obj(metadata={'entity_code': 'hospital',
            'attr_code': 'status', 'source_field': 'hospital.status', 'attr_value': '上海市'}))
    ast = run(extraction, knowledge, decision)
    assert ast['ambiguity'] and not ast['filters']


def test_two_values_within_same_bound_field_remain_ambiguous():
    extraction, knowledge, decision, _, name_field, _ = case()
    knowledge['entity_attribute_values'].append(Obj(metadata={'entity_code': 'province',
        'attr_code': 'name', 'source_field': name_field, 'attr_value': '上海省'}))
    ast = run(extraction, knowledge, decision)
    assert ast['ambiguity'] and not ast['filters']


@pytest.mark.parametrize('surface,expected', [(37, 37), ('003100', '003100'), ('K1', 'K1')])
def test_bound_field_keeps_numeric_thresholds_and_explicit_codes(surface, expected):
    extraction, knowledge, decision, source, _, other = case()
    extraction['过滤条件'][0]['value'] = [surface]
    knowledge['entity_attribute_values'].append(Obj(metadata={'entity_code': 'city',
        'attr_code': 'name', 'source_field': other, 'attr_value': str(surface)}))
    ast = run(extraction, knowledge, decision)
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field': source, 'operator': '=', 'value': expected}]
