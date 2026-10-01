"""Name/code predicates are execution inputs, not display-only rewrites."""
import json
from copy import deepcopy
from types import SimpleNamespace as Doc

import pytest

from query_binding_review import binding_options, review_bindings
from structured_binding import bind


def catalog():
    columns = {'product': ['product_code', 'product_name'],
               'sales_order': ['product_code', 'dealer_code'],
               'dealer': ['dealer_code', 'dealer_name']}
    edges = [('sales_order.product_code', 'product.product_code'),
             ('sales_order.dealer_code', 'dealer.dealer_code')]
    return {'entities': [Doc(metadata={'entity_code': code, 'entity_name': code,
            'attributes': [{'attr_code': col, 'field_mapping': code+'.'+col} for col in cols]})
            for code, cols in columns.items()],
        'relations': [Doc(metadata={'join_key': {'source_field': a, 'target_field': b}}) for a, b in edges],
        '_vector_authorized_fields': [code+'.'+col for code, cols in columns.items() for col in cols],
        'entity_attribute_values': [Doc(metadata={'source_field': 'product.product_name',
                                                 'attr_value': '空心纤维血液透析器'})]}


def extraction(label, value, op='='):
    return {'意图': '明细查询', '实体': ['商品', '经销商'], '指标': [], '维度': [],
            '展示字段': [{'entity': '经销商', 'field': '经销商名称'}],
            '过滤条件': [{'field': label, 'op': op, 'value': value}], '排序': [],
            '时间粒度': {'unit': None, 'time_range': None}, '限制': None, '输出要求': '默认输出表格'}


def forbidden_lookup(*args):
    pytest.fail('ordinary names/codes must not enumerate database keys')


@pytest.mark.parametrize('mode', ['keep', 'edge_only', 'owner_false'])
@pytest.mark.parametrize('operator', ['=', '!=', 'IN', 'NOT IN'])
def test_standard_name_survives_binding_and_review(mode, operator):
    k = catalog()
    e = extraction('商品名称', ['空心纤维透析器'], operator)
    plan = {'subject': 'dealer', 'display_fields': [{'index': 0, 'key': 'dealer.dealer_name'}],
            'filters': [{'index': 0, 'key': 'product.product_name', 'value_ids': [0]}]}
    ast, _ = bind(e, k, Doc(invoke=lambda _: Doc(content=json.dumps(plan))))
    assert not ast['ambiguity']
    before = deepcopy(ast)
    binding = {'filter_index': 0, 'reason': '保留用户按名称筛选的要求'}
    if mode == 'keep':
        binding['keep'] = True
    else:
        binding['choice_index'] = 0
        if mode == 'owner_false':
            binding['bind_owner'] = False
    def invoke(messages):
        assert '同名多编号' in messages[0]['content']
        assert 'bind_owner:true' in messages[0]['content']
        assert '原问题不参与绑定' not in messages[1]['content']
        return Doc(content=json.dumps({'bindings': [binding], 'related_scope': {'mode': 'direct'}}))
    result, repairs = review_bindings(json.dumps(ast), k, '原问题不参与绑定', e,
                                     Doc(invoke=invoke), forbidden_lookup, 81, 205)
    assert json.loads(result) == before
    assert repairs == []
    assert before['filters'][0] == {'field': 'product.product_name', 'operator': operator,
        'value': ['空心纤维血液透析器'] if operator in {'IN', 'NOT IN'} else '空心纤维血液透析器'}


@pytest.mark.parametrize('code', ['0000123', 'AbC-001', 'M60', '14233547'])
def test_code_literals_remain_exact_and_skip_name_to_key_lookup(code):
    k = catalog()
    e = extraction('商品编号', [code])
    plan = {'subject': 'dealer', 'display_fields': [{'index': 0, 'key': 'dealer.dealer_name'}],
            'filters': [{'index': 0, 'key': 'product.product_code'}]}
    ast, _ = bind(e, k, Doc(invoke=lambda _: Doc(content=json.dumps(plan))))
    assert not ast['ambiguity']
    assert binding_options(ast, k) == []
    result, repairs = review_bindings(json.dumps(ast), k, '', e, Doc(invoke=forbidden_lookup),
                                     forbidden_lookup, 81, 205)
    assert json.loads(result)['filters'] == [{'field': 'product.product_code', 'operator': '=', 'value': code}]
    assert repairs == []


def test_alternative_relation_key_does_not_rebind_an_explicit_code():
    k = catalog()
    k['entities'][0].metadata['attributes'].append({'field_mapping': 'product.id'})
    k['_vector_authorized_fields'].append('product.id')
    k['relations'].append(Doc(metadata={'join_key': {'source_field': 'sales_order.product_code',
                                                   'target_field': 'product.id'}}))
    assert binding_options({'filters': [{'field': 'product.product_code', 'operator': '=', 'value': '00001'}]}, k) == []
