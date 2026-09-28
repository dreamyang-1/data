"""STALE_TEST migration: query grain comes from planner slots, not a second NLP pass."""
import json
from copy import deepcopy
from types import SimpleNamespace as Obj
import pytest
import agent
from test_structured_binding import fixture
from types import SimpleNamespace

CODE = 'total_hospitals_in_region'
NAME = '区域全部医院总数'


def catalog():
    return {'_vector_authorized_fields':['hospital.hospital_name','hospital.province'],
            'metrics': [SimpleNamespace(id=CODE, score=1, metadata={
        'type':'metric', 'semantic_model_id':81, 'business_domain_id':205,
        'metric_code':CODE, 'metric_name':NAME, 'synonyms':['医院总数', '医院数量'],
        'source_dependency':{'bind_entity':['hospital']},
        'calculation_rule':{'calc_formula':'COUNT(DISTINCT hospital.hospital_code)'},
    })], 'entities':[SimpleNamespace(id='hospital', score=1, metadata={
        'type':'entity', 'semantic_model_id':81, 'business_domain_id':205,
        'entity_code':'hospital', 'entity_name':'医院', 'attributes':[
            {'attr_code':'hospital_name','attr_name':'医院名称',
             'field_mapping':'hospital.hospital_name','is_main_attribute':True},
            {'attr_code':'province','attr_name':'省份','field_mapping':'hospital.province'},
        ],
    })]}


def draft(dimensions=None):
    return {'version':'2.0','intent':'query','subject':{'entity':'hospital'},
            'metrics':[{'name':CODE,'alias':NAME}], 'dimensions':dimensions or [],
            'filters':[{'field':'hospital.province','operator':'=','value':'上海市'}],
            'time_context':None,'sort':None,'limit':None,'having':[],'ambiguity':[]}


@pytest.mark.parametrize('question', [
    '查询上海地区的区域全部医院总数', '上海医院一共有多少家',
    '按医院分组（只保留为审计原文，不得覆盖已补全的结构化任务）',
    '最近一年（原文中的旧条件不能再覆盖本轮结构化条件）',
])
def test_scalar_shape_comes_only_from_planner(monkeypatch, question):
    extraction, knowledge, plan = fixture()
    class Builder:
        def __init__(self,*args,**kwargs): pass
        def retrieve(self, text):
            assert json.loads(text) == extraction
            return deepcopy(knowledge)
    monkeypatch.setattr(agent,'PromptBuilder',Builder)
    monkeypatch.setattr(agent,'_get_chat_model',lambda:Obj(
        invoke=lambda messages:Obj(content=json.dumps(plan))))
    ast=json.loads(agent.main(question,semantic_model_id=81,business_domain_ids=[205],
                              structured_extraction=extraction))
    assert not ast['ambiguity']
    assert ast['metrics'][0]['name']=='count_hospital'
    assert ast['dimensions']==[] and ast['time_context'] is None
    assert ast['filters'][0]['value']=='上海市'


@pytest.mark.parametrize('slot', ['metrics','dimensions','filters','display_fields'])
def test_model_cannot_add_slots_absent_from_planner(slot):
    from structured_binding import bind
    extraction,knowledge,plan=fixture()
    plan[slot]=[*plan.get(slot,[]),{'index':50,'key':'hospital.name'}]
    ast,_=bind(extraction,knowledge,Obj(invoke=lambda _:Obj(content=json.dumps(plan))))
    assert ast['ambiguity']
    assert '不存在的参数' in ast['ambiguity'][-1]['question']


def test_declared_grouping_preserved_without_reading_original():
    from structured_binding import bind
    extraction,knowledge,plan=fixture()
    extraction['维度']=['医院名称']
    plan['dimensions']=[{'index':0,'key':'hospital.name'}]
    ast,_=bind(extraction,knowledge,Obj(invoke=lambda _:Obj(content=json.dumps(plan))))
    assert not ast['ambiguity']
    assert ast['dimensions'][0]['name']=='hospital.name'


def test_old_grain_review_and_silent_reference_drop_are_deleted():
    assert not hasattr(agent,'_review_metric_query_grain')
    assert not hasattr(agent,'_normalize_structured_reference')
    assert not hasattr(agent,'_structured_reference_retrieval_query')
