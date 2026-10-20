import json
from types import SimpleNamespace
import pytest
import agent
from asl_contract import ASLValidationError


def fixture(formula='quantity=SUM(sales_order.quantity)'):
    metadata={'metric_code':'quantity','metric_name':'销售总数量','synonyms':['销量'],
              'calculation_rule':{'calc_formula':formula}}
    knowledge={'metrics':[SimpleNamespace(metadata=metadata)],
               '_vector_authorized_fields':['sales_order.quantity']}
    ast={'version':'2.0','intent':'query','subject':{'entity':'sales_order'},
         'metrics':[{'name':'quantity'}],'dimensions':[],'filters':[],'having':[],'ambiguity':[]}
    return ast,knowledge


@pytest.mark.parametrize('label,operator,value',[('销售总数量','>',0),('销量','>=',1),('quantity','<',0.5)])
def test_registered_aggregate_contract_uses_having_without_source_value_lookup(monkeypatch,label,operator,value):
    ast,knowledge=fixture()
    def forbidden(*args,**kwargs):raise AssertionError('An aggregate threshold is not an attribute value')
    monkeypatch.setattr(agent,'load_published_entity_attribute_candidates',forbidden)
    expected={'field':label,'operator':operator,'value':[value]}
    repair=agent._repair_contract_filter(ast,expected,knowledge,negative=False,semantic_model_id=130)
    assert repair['type']=='APPLY_REGISTERED_METRIC_HAVING'
    assert ast['filters']==[] and ast['having']==[f'SUM(sales_order.quantity) {operator} {value}']
    contract={'intent':'METRIC_QUERY','metric_required':True,'filters':[expected]}
    agent._validate_intent_asl_contract(json.dumps(ast),contract,knowledge)
    ast['having']=[]
    with pytest.raises(ASLValidationError):
        agent._validate_intent_asl_contract(json.dumps(ast),contract,knowledge)


@pytest.mark.parametrize('formula',['quantity=SUM(sales_order.quantity)-SUM(sales_order.refunds)',
    'quantity=SUM(sales_order.quantity)/2','quantity=unknown(sales_order.quantity)'])
def test_compound_or_unrecognized_formula_is_not_reduced_to_its_first_aggregate(formula):
    _,knowledge=fixture(formula)
    assert agent._contract_metric_having({'field':'销量','operator':'>','value':[0]},knowledge) is None


def test_metric_threshold_requires_unique_metric_and_vector_authorized_physical_field():
    _,knowledge=fixture();expected={'field':'销量','operator':'>','value':[0]}
    knowledge['metrics']*=2
    assert agent._contract_metric_having(expected,knowledge) is None
    knowledge['metrics']=knowledge['metrics'][:1];knowledge['_vector_authorized_fields']=['sales_order.order_key']
    assert agent._contract_metric_having(expected,knowledge) is None


def test_negative_metric_threshold_and_literal_type_are_preserved():
    ast,knowledge=fixture()
    agent._repair_contract_filter(ast,{'field':'销量','operator':'NE','value':[0]},knowledge,negative=True)
    assert ast['having']==['SUM(sales_order.quantity) != 0']
    for value in [True,'zero',float('inf')]:
        assert agent._contract_metric_having({'field':'销量','operator':'>','value':[value]},knowledge) is None


def test_exact_vector_bound_dependency_key_is_not_reinterpreted_as_a_display_name():
    knowledge={'entities':[SimpleNamespace(metadata={'entity_code':'sales_order','entity_name':'销售订单',
        'attributes':[{'attr_code':'order_key','attr_name':'订单号','is_primary_key':True,
                       'field_mapping':'sales_order.order_key'}]})]}
    assert agent._contract_filter_candidates('sales_order.order_key',knowledge)==['sales_order.order_key']
    assert agent._contract_filter_candidates('other.order_key',knowledge)==[]


@pytest.mark.parametrize('actual,accepted',[
    (['B','A'],True),(['A','B','A'],True),(['A'],False),(['A','B','C'],False),([1,'B'],False)])
def test_dependency_in_filter_compares_typed_members_not_list_order(actual,accepted):
    ast={'filters':[{'field':'sales_order.order_key','operator':'IN','value':actual}]}
    expected={'field':'sales_order.order_key','operator':'IN','value':['A','B']}
    assert agent._contract_filter_present(ast,expected,negative=False,
        allowed_fields={'sales_order.order_key'}) is accepted
