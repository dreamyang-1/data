"""A table's consumers do not make its own uniquely named attributes ambiguous."""
import json
from copy import deepcopy
from types import SimpleNamespace as Doc

import pytest

from query_binding_review import review_bindings
from test_query_binding_review import catalog as location_catalog, draft as location_draft


def fixture(code='salesperson', label='业务员', attr='name', field_label='销售员姓名'):
    entities = [Doc(metadata={'entity_code': code, 'entity_name': label, 'attributes': [
        {'attr_code': 'id', 'attr_name': label + '编码', 'field_mapping': code + '.id'},
        {'attr_code': attr, 'attr_name': field_label, 'field_mapping': code + '.' + attr},
    ]})]
    relations = []
    for consumer in ('ledger', 'archive', 'audit'):
        entities.append(Doc(metadata={'entity_code': consumer, 'entity_name': consumer,
            'attributes': [{'field_mapping': consumer + '.' + code + '_id'}]}))
        relations.append(Doc(metadata={'join_key': {'source_field': consumer + '.' + code + '_id',
                                                   'target_field': code + '.id'}}))
    knowledge = {'entities': entities, 'relations': relations, '_vector_authorized_fields': [
        a['field_mapping'] for e in entities for a in e.metadata['attributes']]}
    ast = {'subject': {'entity': 'ledger'}, 'metrics': [{'name': 'amount'}],
           'dimensions': [{'name': 'archive'}], 'filters': [
               {'field': code + '.' + attr, 'operator': '=', 'value': '样本'}],
           'time_context': {'type': 'range', 'start': '2025-01-01', 'end': '2025-12-31'},
           'sort': {'field': 'amount', 'direction': 'DESC'}, 'limit': 3, 'ambiguity': []}
    return ast, knowledge, {'过滤条件': [{'field': field_label, 'op': '=', 'value': ['样本']}]}


def review(ast, knowledge, extraction):
    def forbidden(*args):
        pytest.fail('unique direct attributes need neither another model nor FK lookup')
    result, repairs = review_bindings(json.dumps(ast), knowledge, 'audit wording only', extraction,
        Doc(invoke=forbidden), forbidden, 121, 287, structured_only=True, relationship_required=False)
    return json.loads(result), repairs


@pytest.mark.parametrize('code,label,attr,field_label', [
    ('salesperson', '业务员', 'name', '销售员姓名'), ('orders', '销售订单', 'business_type', '渠道业态'),
    ('hospital', '医院', 'name', '医院名称'), ('dealer', '经销商', 'name', '经销商名称'),
    ('product', '商品', 'model', '规格型号'), ('manufacturer', '厂家', 'brand', '母厂牌'),
    ('department', '科室', 'name', '科室名称'), ('company', '销售公司', 'category', '公司类别'),
])
@pytest.mark.parametrize('op,value', [('=', '样本'), ('!=', '样本'), ('IN', ['样本', '其他']),
                                     ('NOT IN', ['样本', '其他'])])
def test_unique_attributes_keep_names_enums_models_and_every_other_parameter(code,label,attr,field_label,op,value):
    ast, knowledge, extraction = fixture(code,label,attr,field_label)
    ast['filters'][0].update(operator=op, value=value)
    extraction['过滤条件'][0].update(op=op, value=value if isinstance(value,list) else [value])
    result, repairs = review(ast,knowledge,extraction)
    assert result == ast and repairs == []


def test_published_attribute_alias_is_also_a_unique_direct_binding():
    ast,k,e = fixture()
    k['entities'][0].metadata['attributes'][1]['synonyms'] = ['负责人姓名']
    e['过滤条件'][0]['field'] = '负责人姓名'
    assert review(ast,k,e) == (ast, [])


@pytest.mark.parametrize('label', ['渠道业态','业务渠道'])
@pytest.mark.parametrize('op,value', [('=', '样本'), ('!=', '样本'), ('IN',['样本','其他']),
                                     ('NOT IN',['样本','其他'])])
def test_dimension_names_and_aliases_keep_their_unique_governed_field(label,op,value):
    ast,k,e=fixture('orders','销售订单','business_type','业态编码')
    k['dimensions']=[Doc(metadata={'dim_code':'channel_type','dim_name':'渠道业态',
        'synonyms':['业务渠道'],'field_mapping':'orders.business_type'})]
    e['过滤条件'][0].update(field=label,op=op,value=value if isinstance(value,list) else [value])
    ast['filters'][0].update(operator=op,value=value)
    assert review(ast,k,e) == (ast, [])


def test_same_named_dimensions_on_different_owners_remain_ambiguous():
    ast,k,e=fixture('orders','销售订单','business_type','业态编码')
    k['entities'][1].metadata['attributes'].append({'attr_name':'类型编码','field_mapping':'ledger.type'})
    k['_vector_authorized_fields'].append('ledger.type')
    k['dimensions']=[Doc(metadata={'dim_code':code,'dim_name':'渠道业态','field_mapping':field})
                     for code,field in [('order_type','orders.business_type'),('ledger_type','ledger.type')]]
    e['过滤条件'][0]['field']='渠道业态'
    result,repairs=review(ast,k,e)
    assert result['ambiguity'] and result['filters']==ast['filters'] and not repairs


def test_unresolved_dimension_mapping_cannot_supply_a_unique_owner():
    ast,k,e=fixture('orders','销售订单','business_type','业态编码')
    k['entities'][0].metadata['attributes'].append({'attr_name':'另一编码','field_mapping':'orders.other_type'})
    k['_vector_authorized_fields'].append('orders.other_type')
    k['dimensions']=[Doc(metadata={'dim_code':'channel_type','dim_name':'渠道业态',
        'bind_entities':[{'mappingTable':'orders','mappingColumn':column}
                         for column in ['business_type','other_type']]})]
    e['过滤条件'][0]['field']='渠道业态'
    result,repairs=review(ast,k,e)
    assert result['ambiguity'] and result['filters']==ast['filters'] and not repairs


def test_two_same_named_attributes_do_not_infer_owner_from_model_selection():
    ast,k,e = fixture(field_label='名称')
    k['entities'][1].metadata['attributes'].append({'attr_name':'名称','field_mapping':'ledger.name'})
    k['_vector_authorized_fields'].append('ledger.name')
    result, repairs = review(ast,k,e)
    assert result['ambiguity'] and result['filters'] == ast['filters'] and not repairs


def test_unresolved_explicit_owner_cannot_be_replaced_with_the_field_owner():
    ast,k,e = fixture(); e['过滤条件'][0]['entity'] = '未知实体'
    result, repairs = review(ast,k,e)
    assert result['ambiguity'] and not repairs


def test_unique_geography_dictionary_attribute_still_needs_business_owner():
    ast,k = location_draft(),location_catalog()
    k['entities'][2].metadata['attributes'][0]['attr_name'] = '省份名称'
    result, repairs = review(ast,k,{'过滤条件':[{'field':'省份名称','op':'=','value':['上海市']}]})
    assert result['ambiguity'] and result['filters'] == ast['filters'] and not repairs


def test_reordered_and_split_predicates_preserve_each_original_field_owner():
    ast,k,e = fixture(); ast['filters'].append(deepcopy(ast['filters'][0]))
    ast['filters'][1]['value']='另一名'
    e['过滤条件'].insert(0, {'field':'销售员姓名','op':'=','value':['另一名']})
    k['_structured_filter_origins']=[1,0]
    assert review(ast,k,e) == (ast, [])
