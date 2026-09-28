import json
from copy import deepcopy
from types import SimpleNamespace as Obj
import pytest
from structured_binding import bind


def fixture():
    extraction={'意图':'统计查询','实体':['医院'],'指标':[{'name':'医院总数'}],
                '维度':[],'展示字段':[],'过滤条件':[{'field':'省份','op':'=','value':['上海']}],
                '排序':[],'时间粒度':{'unit':None,'time_range':None},'限制':None,'输出要求':'默认输出表格'}
    attrs=[{'attr_name':'省份','field_mapping':'hospital.province'},
           {'attr_name':'医院名称','field_mapping':'hospital.name'},
           {'attr_name':'金额','field_mapping':'hospital.amount'},
           {'attr_name':'日期','field_mapping':'hospital.date'}]
    knowledge={'entities':[Obj(metadata={'entity_code':'hospital','entity_name':'医院','attributes':attrs})],
       'metrics':[Obj(metadata={'metric_code':'count_hospital','metric_name':'区域全部医院总数','entity_code':'hospital'})],
       'dimensions':[], 'entity_attribute_values':[Obj(metadata={'source_field':'hospital.province','attr_value':'上海市'})],
       '_vector_authorized_fields':[a['field_mapping'] for a in attrs]}
    plan={'subject':'hospital','metrics':[{'index':0,'key':'count_hospital'}],
          'filters':[{'index':0,'key':'hospital.province','value_ids':[0]}]}
    return extraction,knowledge,plan


def run(extraction,knowledge,plan):
    return bind(extraction,knowledge,Obj(invoke=lambda messages:Obj(content=json.dumps(plan))))


def test_scalar_shape_and_standard_alias_not_reinterpreted():
    e,k,p=fixture();ast,_=run(e,k,p)
    assert not ast['ambiguity']
    assert ast['dimensions']==[] and ast['time_context'] is None
    assert ast['metrics']==[{'name':'count_hospital','alias':'区域全部医院总数','time_anchor':None}]
    assert ast['filters'][0]['value']=='上海市'


@pytest.mark.parametrize('literal',[1000,0,-1,12.5,'00123','M60 set','-001','1e3'])
def test_literal_numbers_and_codes_preserved(literal):
    e,k,p=fixture();e['过滤条件'][0]={'field':'金额','op':'>','value':[literal]}
    p['filters']=[{'index':0,'key':'hospital.amount'}]
    ast,_=run(e,k,p)
    assert not ast['ambiguity'] and ast['filters'][0]['value']==literal
    assert ast['filters'][0]['operator']=='>'


@pytest.mark.parametrize('slot,key',[('metrics','指标'),('filters','过滤条件')])
def test_missing_core_slot_is_explicit_not_silently_dropped(slot,key):
    e,k,p=fixture();p[slot]=[];ast,_=run(e,k,p)
    assert ast['ambiguity'] and key in ast['ambiguity'][0]['question']
    assert str(e[key][0].get('name') or e[key][0].get('field')) in ast['ambiguity'][0]['question']


def test_no_added_dimension_or_metric():
    e,k,p=fixture();p['dimensions']=[{'index':0,'key':'hospital.name'}]
    ast,_=run(e,k,p);assert ast['ambiguity'] and ast['dimensions']==[]


def test_partial_display_keeps_existing_contract_but_missing_filter_blocks():
    e,k,p=fixture();e['指标']=[];p['metrics']=[]
    e['展示字段']=[{'entity':'医院','field':'医院名称'},{'entity':'医院','field':'联系方式'}]
    p['display_fields']=[{'index':0,'key':'hospital.name'},{'index':1,'error':'目录无匹配'}]
    ast,repairs=run(e,k,p)
    assert not ast['ambiguity'] and ast['dimensions'][0]['name']=='hospital.name'
    assert repairs[0]['type']=='OMIT_UNAVAILABLE_DISPLAY_FIELD'
    p['filters']=[];ast,_=run(e,k,p);assert ast['ambiguity']


def test_explicit_time_without_anchor_is_not_removed():
    e,k,p=fixture();e['时间粒度']['time_range']='最近一年'
    p['time']={'mode':'rolling','amount':1,'unit':'year','anchor':'invented.date'}
    ast,_=run(e,k,p);assert '时间' in ast['ambiguity'][0]['question']


def test_original_question_cannot_reach_retrieval_or_model(monkeypatch):
    import agent
    e,k,p=fixture();seen=[]
    class Builder:
        def __init__(self,*args,**kwargs):self.last_knowledge=deepcopy(k)
        def retrieve(self,text):seen.append(text);return self.last_knowledge
        def build(self,text):pytest.fail('old prompt construction must be deleted')
    def invoke(messages):
        seen.append(json.dumps(messages,ensure_ascii=False));return Obj(content=json.dumps(p))
    monkeypatch.setattr(agent,'PromptBuilder',Builder)
    monkeypatch.setattr(agent,'_get_chat_model',lambda:Obj(invoke=invoke))
    result=agent.main('SECRET ORIGINAL',completed_question='OTHER SECRET',retrieval_query='WRONG EXTRA',
        semantic_model_id=81,business_domain_ids=[205],structured_extraction=e)
    assert not json.loads(result)['ambiguity']
    assert all(not any(secret in text for secret in ['SECRET','WRONG EXTRA','OLD extraction prompt']) for text in seen)


def test_missing_extraction_never_falls_back(monkeypatch):
    import agent
    monkeypatch.setattr(agent,'_get_chat_model',lambda:pytest.fail('must not re-extract'))
    result=json.loads(agent.main('医院总数',semantic_model_id=81,business_domain_ids=[205]))
    assert result['ambiguity'] and '上游未提供' in result['ambiguity'][0]['question']


def test_bare_source_column_and_full_per_parameter_pool_keep_standard_value():
    from structured_binding import catalog_candidates
    e,k,p=fixture()
    k['entity_attribute_values']=[]
    k['_ambiguity_candidates']={'entity_attribute_value':[Obj(metadata={
        'source_table':'hospital','source_field':'province','attr_value':'上海市'})]}
    values=catalog_candidates(k)['values']
    assert values==[{'id':0,'field':'hospital.province','value':'上海市'}]
    ast,_=run(e,k,p)
    assert not ast['ambiguity'] and ast['filters'][0]['value']=='上海市'


def test_time_grouping_uses_explicit_unit_without_adding_time_range():
    e,k,p=fixture();e['维度']=['日期'];e['时间粒度']['unit']='月'
    p['dimensions']=[{'index':0,'key':'hospital.date','granularity':'month'}]
    ast,_=run(e,k,p)
    assert not ast['ambiguity'] and ast['dimensions'][0]['granularity']=='month'
    assert ast['time_context'] is None


def test_explicit_rolling_period_is_resolved_without_default_scope():
    from datetime import date
    e,k,p=fixture();e['时间粒度']['time_range']='最近一年'
    p['time']={'mode':'rolling','amount':1,'unit':'year','anchor':'hospital.date'}
    ast,_=bind(e,k,Obj(invoke=lambda _:Obj(content=json.dumps(p))),today=date(2026,9,28))
    assert not ast['ambiguity']
    assert ast['time_context']['start']=='2025-09-28'
    assert ast['time_context']['end']=='2026-09-28'


@pytest.mark.parametrize('bad', [[],{},None])
def test_bad_model_keys_are_clarified_not_server_errors(bad):
    e,k,p=fixture();p['subject']=bad;p['metrics'][0]['key']=bad
    ast,_=run(e,k,p)
    assert ast['ambiguity']


@pytest.mark.parametrize('display,valid', [('hospital.name',True),('hospital.amount',False)])
def test_logical_group_identity_label_does_not_create_an_extra_group(display,valid):
    e,k,p=fixture();e['维度']=['医院'];e['展示字段']=[{'entity':'医院','field':display}]
    attrs=k['entities'][0].metadata['attributes']
    attrs[1]['attr_code']='hospital_name'
    attrs.append({'attr_code':'hospital_code','field_mapping':'hospital.code'})
    k['_vector_authorized_fields'].append('hospital.code')
    k['dimensions']=[Obj(metadata={'dim_code':'hospital','bind_entities':[
        {'mappingTable':'hospital','mappingColumn':'code'}]})]
    p['dimensions']=[{'index':0,'key':'hospital'}]
    p['display_fields']=[{'index':0,'key':display}]
    ast,_=run(e,k,p)
    assert (not ast['ambiguity']) == valid
    assert [d['name'] for d in ast['dimensions']]==['hospital']


def test_missing_output_requests_business_choice_not_system_repair():
    e,k,p=fixture();e['指标']=[];p['metrics']=[]
    ast,_=run(e,k,p)
    assert ast['ambiguity'][0]['type']=='operation_intent'
    assert '指标/展示字段' in ast['ambiguity'][0]['question']
