"""Requested display fields must be completed from scoped metadata before validation."""
import copy
import json
from types import SimpleNamespace as Obj

import pytest
import agent
from asl_contract import ASLValidationError


def knowledge():
    attributes=[{'attr_code':'salesperson_code','attr_name':'业务员编码',
        'field_mapping':{'mappingTable':'salesperson','mappingColumn':'salesperson_code'}},
        {'attr_code':'salesperson_name','attr_name':'销售员姓名','is_display_name':True,
        'field_mapping':{'mappingTable':'salesperson','mappingColumn':'salesperson_name'}}]
    return {'entities':[Obj(metadata={'entity_code':'salesperson','entity_name':'业务员',
        'attributes':json.dumps(attributes,ensure_ascii=False)})],
        'dimensions':[Obj(metadata={'dim_code':'salesperson','dim_name':'业务员','bind_entities':[
            {'entity':'salesperson','mappingTable':'salesperson','mappingColumn':'salesperson_code'}]})],
        'metrics':[Obj(metadata={'metric_code':'order_count','metric_name':'订单笔数'})]}


def draft():
    return {'version':'2.0','intent':'query','subject':{'entity':'salesperson'},
        'dimensions':[{'name':'salesperson'}],'metrics':[{'name':'order_count'}],
        'filters':[],'having':[],'time_context':None,'sort':None,'limit':None,'ambiguity':[]}


def contract():
    return {'intent':'METRIC_QUERY','query_object':'业务员','metric_required':True,
        'required_metric_codes':['order_count'],'required_projections':['销售员姓名'],
        'required_groupings':['业务员'],'filters':[],'negative_filters':[],
        'sorting':None,'time_dimension_required':False}


def test_missing_display_is_added_once_without_changing_metrics_filters_or_grouping():
    ast=draft();before=copy.deepcopy(ast);catalog=knowledge()
    with pytest.raises(ASLValidationError,match='projection'):
        agent._validate_intent_asl_contract(json.dumps(ast),contract(),catalog)
    field=agent._repair_contract_grouping(ast,'销售员姓名',catalog,query_object='业务员',prefer_display=True)
    assert field=='salesperson.salesperson_name'
    assert ast['dimensions']==before['dimensions']+[{'name':'salesperson.salesperson_name'}]
    assert {k:v for k,v in ast.items() if k!='dimensions'}=={k:v for k,v in before.items() if k!='dimensions'}
    assert agent._repair_contract_grouping(ast,'销售员姓名',catalog,query_object='业务员',prefer_display=True) is None
    agent._validate_intent_asl_contract(json.dumps(ast),contract(),catalog)


@pytest.mark.parametrize('mode',['missing','ambiguous'])
def test_projection_repair_cannot_invent_or_choose_between_fields(mode):
    ast=draft();catalog=knowledge()
    attrs=json.loads(catalog['entities'][0].metadata['attributes'])
    if mode=='missing':attrs=attrs[:1]
    else:attrs.append({**attrs[-1],'attr_code':'other_name',
        'field_mapping':{'mappingTable':'salesperson','mappingColumn':'other_name'}})
    catalog['entities'][0].metadata['attributes']=json.dumps(attrs)
    assert agent._repair_contract_grouping(ast,'销售员姓名',catalog,query_object='业务员',prefer_display=True) is None
    assert ast==draft()
    if mode=='ambiguous':
        with pytest.raises(ASLValidationError):agent._validate_intent_asl_contract(json.dumps(ast),contract(),catalog)


def test_repaired_field_still_has_to_pass_frozen_vector_authorization():
    catalog=knowledge();catalog['_vector_authorized_fields']=['salesperson.salesperson_code']
    ast=draft();agent._repair_contract_grouping(ast,'销售员姓名',catalog,query_object='业务员',prefer_display=True)
    with pytest.raises(ValueError):agent._validate_vector_grounded_asl(json.dumps(ast),catalog)


def test_original_question_generation_completes_required_projection_before_contract_validation(monkeypatch):
    observed=[]
    class Builder:
        def __init__(self,*args,**kwargs):self.last_knowledge=knowledge()
        def build(self,question):observed.append(question);return 'current scoped catalog'
    monkeypatch.setattr(agent,'PromptBuilder',Builder)
    monkeypatch.setattr(agent,'_get_chat_model',lambda:Obj(invoke=lambda _:Obj(content=json.dumps(draft()))))
    monkeypatch.setattr(agent,'_validate_asl_output',lambda value,*args,**kwargs:value)
    def ground(ast,*args,**kwargs):
        assert any(x['name']=='salesperson.salesperson_name' for x in ast['dimensions'])
        return []
    monkeypatch.setattr(agent,'_ground_executable_vector_values',ground)
    result=agent._recover_question_asl('查询业务员订单笔数并展示销售员姓名',store=object(),
        semantic_model_id=130,business_domain_id=None,domain_ids=[],metric_codes=['order_count'],
        metric_selection_authoritative=True,structured_extraction={},structured_reference=None,
        intent_asl_contract=contract())
    assert result is not None and observed
    ast=json.loads(result[0]);assert ast['metrics']==draft()['metrics']
    agent._validate_intent_asl_contract(result[0],contract(),result[1])
