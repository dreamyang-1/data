import json
from types import SimpleNamespace as Doc
from copy import deepcopy
import pytest
from related_scope import shared_scope_options, apply_shared_scope, related_fields
from query_binding_review import review_bindings


def fixture():
    mapping = {'sales':['product_id','hospital_id'], 'product':['id','brand','category'],
               'bridge':['product_id','tag_id'], 'tag':['id','name']}
    edges = [('sales.product_id','product.id'),('product.id','bridge.product_id'),('bridge.tag_id','tag.id')]
    entities = []
    for e, fields in mapping.items():
        entities.append(Doc(metadata={'entity_code':e, 'attributes':[
            {'field_mapping':f'{e}.{f}','attr_name':f} for f in fields], 'relations':[
                {'join_key':{'source_field':a,'target_field':b}} for a,b in edges if a.startswith(e+'.')]}))
    knowledge = {'entities':entities,'_vector_authorized_fields':[f'{e}.{f}' for e,fs in mapping.items() for f in fs]}
    ast = {'subject':{'entity':'sales'},'metrics':[{'name':'amount'}],'dimensions':[{'name':'dealer'}],
           'filters':[{'field':'sales.hospital_id','operator':'=','value':'H'},
                      {'field':'product.brand','operator':'=','value':'B'},
                      {'field':'sales.product_id','operator':'IN','value':['A','B']}],
           'sort':{'field':'amount','direction':'DESC'},'limit':10,'time_context':None}
    return ast,knowledge


def test_scope_preserves_target_conditions_and_outer_ranking():
    ast,k=fixture(); before=deepcopy(ast)
    options=shared_scope_options(ast,k)
    assert len(options)==1
    repairs=apply_shared_scope(ast,options,{'mode':'shared_attribute','option_index':0,'evidence':'找共同适用科室的渠道'},'找共同适用科室的渠道')
    assert ast['filters']==before['filters'][:1]
    assert ast['related_filters'][0]['target_filters'][1]['field']=='product.id'
    assert ast['related_filters'][0]['target_filters'][1]['value']==['A','B']
    for key in ('metrics','dimensions','sort','limit','time_context'):
        assert ast[key]==before[key]
    assert repairs[0]['type']=='SHARED_ATTRIBUTE_SCOPE'
    assert set(related_fields(ast)) <= set(k['_vector_authorized_fields'])


@pytest.mark.parametrize('decision',[{'mode':'direct'}, {'mode':'shared_attribute','option_index':99,'evidence':'适用科室'}, {'mode':'shared_attribute','option_index':0,'evidence':'编造关系要求'}])
def test_no_unrequested_rewrite(decision):
    ast,k=fixture(); before=deepcopy(ast)
    assert apply_shared_scope(ast,shared_scope_options(ast,k),decision,'查商品已有销售')==[]
    assert ast==before


def test_missing_vector_field_or_relation_does_not_invent_path():
    ast,k=fixture(); k['_vector_authorized_fields'].remove('bridge.tag_id')
    assert shared_scope_options(ast,k)==[]


def test_context_review_selects_scope_without_keyword_rules():
    ast,k=fixture()
    model=Doc(invoke=lambda messages:Doc(content=json.dumps({'related_scope':{
        'mode':'shared_attribute','option_index':0,'evidence':'寻找相同使用领域的渠道'}})))
    result,repairs=review_bindings(json.dumps(ast),k,'寻找相同使用领域的渠道',{},model,None,106,259)
    assert json.loads(result)['related_filters']
    assert repairs


def test_shared_target_is_not_rebound_onto_bridge_by_owner_review():
    ast,k=fixture()
    ast['filters'][2]={'field':'product.id','operator':'=','value':'A'}
    def invoke(messages):
        context=json.loads(messages[1]['content'])
        assert any(o['filter_index']==1 for o in context['filter_options'])
        return Doc(content=json.dumps({'related_scope':{'mode':'shared_attribute','option_index':0,'evidence':'寻找相同使用领域的渠道'},
            'bindings':[{'filter_index':1,'choice_index':0,'reason':'目标关联'}]}))
    result,_=review_bindings(json.dumps(ast),k,'寻找相同使用领域的渠道',{},Doc(invoke=invoke),lambda *a:['changed'],106,259)
    parsed=json.loads(result)
    assert parsed['related_filters'][0]['target_filters'][1]['value']=='A'
    assert parsed['related_filters'][0]['target_filters'][0]['value']=='B'
    assert parsed['filters']==ast['filters'][:1]
