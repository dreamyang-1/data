"""Contract checks accept the same declared value and the same bound field."""
import json
from types import SimpleNamespace as Obj

import pytest
from agent import _contract_filter_present, _contract_projection_candidates, _validate_intent_asl_contract
from asl_contract import ASLValidationError


@pytest.mark.parametrize('operator,negative',[('=',False),('!=',True),('>',False)])
@pytest.mark.parametrize('actual,expected',[('华山医院',['华山医院']),(['华山医院'],'华山医院'),(3,[3])])
def test_scalar_and_single_value_list_represent_the_same_declared_condition(operator,negative,actual,expected):
    ast={'filters':[{'field':'hospital.name','operator':operator,'value':actual}]}
    contract={'operator':operator,'value':expected}
    assert _contract_filter_present(ast,contract,negative=negative,allowed_fields={'hospital.name'})
    assert not _contract_filter_present(ast,contract,negative=negative,allowed_fields={'dealer.name'})


def test_singleton_equivalence_does_not_accept_different_or_partial_values():
    ast={'filters':[{'field':'hospital.name','operator':'=','value':'华山医院'}]}
    assert not _contract_filter_present(ast,{'operator':'=','value':['新华医院']},negative=False)
    assert not _contract_filter_present(ast,{'operator':'=','value':['华山医院','新华医院']},negative=False)
    assert not _contract_filter_present(ast,{'operator':'=','value':['华山医院']},negative=True)


def test_declared_hospital_filter_passes_full_contract_after_scalar_binding():
    k={'_vector_authorized_fields':['hospital.hospital_name'],
       'entities':[Obj(metadata={'entity_code':'hospital','entity_name':'医院','attributes':[
           {'attr_name':'医院名称','attr_code':'hospital_name','field_mapping':'hospital.hospital_name'}]})]}
    ast={'subject':{'entity':'hospital'},'metrics':[],'dimensions':[],'display_fields':[],
         'filters':[{'field':'hospital.hospital_name','operator':'=','value':'复旦大学附属华山医院'}]}
    contract={'intent':'METRIC_QUERY','query_object':'商品','filters':[
        {'entity':'医院','field':'医院名称','operator':'=','value':['复旦大学附属华山医院']}]}
    _validate_intent_asl_contract(json.dumps(ast,ensure_ascii=False),contract,k)
    ast['filters'][0]['value']='其他医院'
    with pytest.raises(ASLValidationError):
        _validate_intent_asl_contract(json.dumps(ast,ensure_ascii=False),contract,k)


def catalog():
    return {'_vector_authorized_fields':['salesperson.salesperson_code','salesperson.salesperson_name'],
      'entities':[Obj(metadata={'entity_code':'salesperson','entity_name':'业务员','attributes':[
          {'attr_code':'salesperson_code','attr_name':'销售员编码','field_mapping':'salesperson.salesperson_code'},
          {'attr_code':'salesperson_name','attr_name':'销售员姓名','field_mapping':'salesperson.salesperson_name'}]})],
      'dimensions':[Obj(metadata={'dim_code':'salesperson','dim_name':'业务员','synonyms':['销售员编码']})]}


def test_registered_explicit_code_display_is_not_mistaken_for_logical_dimension():
    k=catalog();selected={'salesperson.salesperson_code','salesperson.salesperson_name'}
    assert _contract_projection_candidates('销售员编码','业务员',k,selected,prefer_display=True)==['salesperson.salesperson_code']
    ast={'metrics':[],'dimensions':[],'display_fields':[{'name':n} for n in sorted(selected)],'filters':[]}
    contract={'intent':'DETAIL_QUERY','required_projections':['销售员编码','销售员姓名']}
    _validate_intent_asl_contract(json.dumps(ast,ensure_ascii=False),contract,k)


def test_unselected_or_unregistered_code_does_not_gain_projection_proof():
    k=catalog()
    assert _contract_projection_candidates('销售员编码','业务员',k,{'salesperson.salesperson_name'},prefer_display=True)!=['salesperson.salesperson_code']
    k['_vector_authorized_fields'].remove('salesperson.salesperson_code')
    assert _contract_projection_candidates('销售员编码','业务员',k,{'salesperson.salesperson_code'},prefer_display=True)!=['salesperson.salesperson_code']
