"""Planner-owned predicates resolve independently of subject/grouping/model rank."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from query_binding_review import review_bindings
from test_query_binding_review import catalog, draft


def resolve(extraction, *, ast=None, knowledge=None, resolver=None, structured=True):
    calls = []
    def forbidden(messages):
        calls.append(messages)
        raise AssertionError('explicit ownership needs no model decision')
    knowledge = knowledge or catalog()
    result, repairs = review_bindings(json.dumps(ast or draft()), knowledge,
        '原问题不能作为重新解释参数的依据', extraction, SimpleNamespace(invoke=forbidden),
        resolver or (lambda *args: ['001']), 81, 205, structured_only=structured,
        relationship_required=False)
    return json.loads(result), repairs, calls, knowledge


@pytest.mark.parametrize('owner,code', [('医院','hospital'), ('经销商','dealer'), ('hospital','hospital')])
@pytest.mark.parametrize('op,expected', [('=','='), ('!=','!='), ('IN','IN'), ('NOT IN','NOT IN')])
def test_explicit_owner_skips_model_and_preserves_every_other_parameter(owner, code, op, expected):
    ast = draft()
    ast['filters'][0]['operator'] = op
    source = {'entity':owner, 'field':'省份', 'op':op, 'value':['上海市']}
    result, repairs, calls, knowledge = resolve({'过滤条件':[source]}, ast=ast)
    assert not calls and not result['ambiguity']
    assert result['filters'] == [{'field':code+'.province_id', 'operator':expected,
                                  'value':['001'] if op in {'IN','NOT IN'} else '001'}]
    assert {k:v for k,v in result.items() if k!='filters'} == {k:v for k,v in ast.items() if k!='filters'}
    assert repairs[0]['source_filter'] == source
    assert repairs[0]['owner_entity'] == code
    assert knowledge['_filter_owner_bindings'] == repairs


def test_two_owners_with_identical_literals_are_not_merged():
    ast = draft(); ast['filters'].append(deepcopy(ast['filters'][0]))
    result, _, calls, _ = resolve({'过滤条件':[
        {'entity':owner,'field':'省份','op':'=','value':['上海市']} for owner in ['医院','经销商']]}, ast=ast)
    assert not calls and not result['ambiguity']
    assert [f['field'] for f in result['filters']] == ['hospital.province_id','dealer.province_id']


def test_origin_indices_survive_split_or_reordered_filters():
    k = catalog(); k['_structured_filter_origins'] = [1,0]
    ast = draft(); ast['filters'].append(dict(ast['filters'][0],value='北京市'))
    result, _, _, _ = resolve({'过滤条件':[
        {'entity':'经销商','field':'省份','op':'=','value':['北京市']},
        {'entity':'医院','field':'省份','op':'=','value':['上海市']}]}, ast=ast, knowledge=k)
    assert [f['field'] for f in result['filters']] == ['hospital.province_id','dealer.province_id']


@pytest.mark.parametrize('keys', [[],None])
def test_missing_dictionary_keys_blocks_instead_of_changing_owner(keys):
    result, repairs, calls, _ = resolve({'过滤条件':[
        {'entity':'医院','field':'省份','op':'=','value':['上海市']}]}, resolver=lambda *args:keys)
    assert not calls and result['ambiguity'] and not repairs
    assert result['filters'] == draft()['filters']


def test_unowned_shared_dictionary_cannot_be_guessed_from_dealer_grouping():
    result, repairs, calls, _ = resolve({'过滤条件':[{'field':'省份','op':'=','value':['上海市']}]})
    assert not calls and result['ambiguity'] and not repairs
    assert result['filters'] == draft()['filters']


def test_explicit_dictionary_scope_keeps_its_name_predicate():
    result, repairs, calls, _ = resolve({'过滤条件':[
        {'entity':'省份','field':'省份','op':'=','value':['上海市']}]})
    assert not calls and not result['ambiguity'] and not repairs
    assert result['filters'] == draft()['filters']


def test_missing_authorized_owner_edge_cannot_use_other_entity():
    k = catalog(); k['_vector_authorized_fields'].remove('hospital.province_id')
    result, repairs, calls, _ = resolve({'过滤条件':[
        {'entity':'医院','field':'省份','op':'=','value':['上海市']}]}, knowledge=k)
    assert not calls and result['ambiguity'] and not repairs


def test_owner_with_two_different_published_keys_needs_disambiguation():
    k = catalog()
    k['entities'][0].metadata['attributes'].append({'attr_code':'registered_province',
        'field_mapping':'hospital.registered_province'})
    k['_vector_authorized_fields'].append('hospital.registered_province')
    k['relations'].append(SimpleNamespace(metadata={'join_key':{
        'source_field':'hospital.registered_province','target_field':'dim_province.province_id'}}))
    result, repairs, calls, _ = resolve({'过滤条件':[
        {'entity':'医院','field':'省份','op':'=','value':['上海市']}]}, knowledge=k)
    assert not calls and result['ambiguity'] and not repairs


@pytest.mark.parametrize('negative', [False,True])
def test_dictionary_receipt_passes_contract_but_never_another_owner(negative):
    from agent import _validate_intent_asl_contract
    from asl_contract import ASLValidationError
    op = '!=' if negative else '='
    ast = draft(); ast['filters'][0]['operator'] = op
    source = {'entity':'医院','field':'省份','op':op,'value':['上海市']}
    result, _, _, knowledge = resolve({'过滤条件':[source]},ast=ast)
    expected = {'entity':'医院','field':'省份','operator':'NE' if negative else 'EQ','value':'上海市'}
    contract = {'intent':'METRIC_QUERY','metric_required':False,
                'negative_filters' if negative else 'filters':[expected]}
    _validate_intent_asl_contract(json.dumps(result),contract,knowledge)
    expected['entity'] = '经销商'
    with pytest.raises(ASLValidationError):
        _validate_intent_asl_contract(json.dumps(result),contract,knowledge)


def test_full_binding_keeps_name_catalog_then_resolves_declared_hospital_owner():
    from structured_binding import bind
    k=catalog()
    k['entities'][0].metadata['attributes'][0]['attr_name']='省份'
    k['entities'][1].metadata['attributes'][0]['attr_name']='省份'
    k['entities'][2].metadata['attributes'][0]['attr_name']='省份名称'
    k['entities'][0].metadata['attributes'].append({'attr_code':'hospital_name','attr_name':'医院名称',
        'field_mapping':'hospital.hospital_name'})
    k['_vector_authorized_fields'].append('hospital.hospital_name')
    k['entity_attribute_values']=[SimpleNamespace(metadata={'entity_code':'province',
        'attr_code':'province_name','source_field':'dim_province.province_name','attr_value':'上海市'})]
    source={'entity':'医院','field':'省份','op':'=','value':['上海市']}
    extraction={'实体':['医院','经销商','省份'],'指标':[],'维度':[],
        '展示字段':[{'entity':'医院','field':'医院名称'}],'过滤条件':[source],
        '排序':[],'时间粒度':{'unit':None,'time_range':None},'限制':None}
    model=SimpleNamespace(invoke=lambda _:SimpleNamespace(content=json.dumps({
        'subject':'hospital','display_fields':[{'index':0,'key':'hospital.hospital_name'}],
        'filters':[{'index':0,'key':'dealer.province_id','value_ids':[0]}]})))
    ast,_=bind(extraction,k,model)
    assert not ast['ambiguity']
    result,_,calls,_=resolve(extraction,ast=ast,knowledge=k)
    assert not calls and not result['ambiguity']
    assert result['filters']==[{'field':'hospital.province_id','operator':'=','value':'001'}]
