from copy import deepcopy
from types import SimpleNamespace as Doc

import pytest
import agent
from asl_contract import ASLValidationError


def knowledge():
    return {'_vector_authorized_fields':['sales_order.quantity','sales_order.status'],
        'entities':[Doc(metadata={'entity_code':'sales_order','attributes':[
            {'attr_name':'销售数量','attr_code':'quantity','field_mapping':'sales_order.quantity'},
            {'attr_name':'状态','attr_code':'status','field_mapping':'sales_order.status'}]})]}


@pytest.mark.parametrize('actual,surface',[(0,'0'),(12.5,'12.5'),(-2,'-2')])
def test_typed_numeric_asl_proves_identical_caller_threshold_without_value_vector(actual,surface):
    ast={'filters':[{'field':'sales_order.quantity','operator':'>','value':actual}]}
    contract={'filters':[{'field':'销售数量','operator':'>','value':surface}]}
    before=deepcopy((ast,contract))
    assert agent._ground_executable_vector_values(ast,knowledge(),contract,
        store=None,semantic_model_id=130,domain_ids=[])==[]
    assert (ast,contract)==before


@pytest.mark.parametrize('change',['value','field','operator','boolean','string_asl'])
def test_numeric_proof_cannot_authorize_different_predicate_or_untyped_asl(change):
    actual={'field':'sales_order.quantity','operator':'>','value':0}
    if change=='value':actual['value']=1
    if change=='field':actual['field']='sales_order.status'
    if change=='operator':actual['operator']='>='
    if change=='boolean':actual['value']=False
    if change=='string_asl':actual['value']='0'
    with pytest.raises(ASLValidationError):
        agent._ground_executable_vector_values({'filters':[actual]},knowledge(),
            {'filters':[{'field':'销售数量','operator':'>','value':'0'}]},
            store=None,semantic_model_id=130,domain_ids=[])


def test_business_names_still_need_same_field_vector_standard():
    with pytest.raises(ASLValidationError):
        agent._ground_executable_vector_values({'filters':[{'field':'sales_order.status','operator':'=','value':'有效'}]},
            knowledge(),{'filters':[{'field':'状态','operator':'=','value':'有效'}]},
            store=None,semantic_model_id=130,domain_ids=[])


@pytest.mark.parametrize('actual,surface',[(0,'0'),(12.5,'12.5'),(-2,'-2')])
def test_contract_repair_does_not_replace_a_correct_numeric_literal_with_text(actual,surface):
    ast={'filters':[{'field':'sales_order.quantity','operator':'>','value':actual}]}
    expected={'field':'销售数量','operator':'>','value':surface}
    original=deepcopy((ast,expected))
    agent._repair_contract_filter(ast,expected,knowledge(),negative=False)
    assert ast==original[0] and type(ast['filters'][0]['value']) is type(actual)
    assert agent._contract_filter_present(ast,expected,negative=False,allowed_fields={'sales_order.quantity'})
    agent._ground_executable_vector_values(ast,knowledge(),{'filters':[expected]},
        store=None,semantic_model_id=130,domain_ids=[])
    assert expected==original[1]
