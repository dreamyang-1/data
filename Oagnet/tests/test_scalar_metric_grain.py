import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import agent


CODE = 'total_hospitals_in_region'
NAME = '区域全部医院总数'


def catalog():
    return {'metrics': [SimpleNamespace(id=CODE, score=1, metadata={
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


@pytest.mark.parametrize('question', ['查询上海地区的区域全部医院总数', '查询上海地区医院总数'])
def test_metric_name_is_not_an_instruction_to_group_by_hospital(question):
    ast = draft()
    agent._normalize_explicit_grouping_dimensions(ast, catalog(), question)
    assert ast['dimensions'] == []


def reference(**updates):
    return {'primary_intent':'统计查询', 'dimensions':[], 'fields':[],
            'metrics':[{'name':NAME}], 'filters':[], **updates}


@pytest.mark.parametrize('question', [
    '查询上海地区的区域全部医院总数', '查询上海地区医院总数',
    '上海医院一共有多少家', '统计上海所有医院的总数',
    '最近一年上海医院总数',
])
def test_scalar_paraphrases_remove_model_added_grouping_without_changing_filters(question):
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['sort'] = {'field':'hospital.hospital_name','field_type':'dimension','direction':'ASC'}
    content, repairs = agent._repair_unrequested_metric_grouping(json.dumps(ast), catalog(), question, reference())
    actual = json.loads(content)
    assert actual['dimensions'] == []
    assert actual['sort'] is None
    assert actual['filters'] == ast['filters']
    assert actual['metrics'] == ast['metrics']
    assert len(repairs) == 1


@pytest.mark.parametrize('question', [
    '各经销商的区域全部医院总数', '按医院等级统计区域全部医院总数',
    '每月区域全部医院总数', '医院总数趋势', '上海和北京分别有多少医院',
    '医院总数排名前10的地区', '列出医院名称和医院总数', '医院总数同比变化',
    '按照城市统计医院数量', '医院数量最多的城市',
])
def test_question_grouping_wins_even_when_upstream_dimensions_are_empty(question):
    ast = draft([{'name':'hospital.hospital_name'}])
    content = json.dumps(ast)
    assert agent._repair_unrequested_metric_grouping(content, catalog(), question, reference()) == (content, [])


@pytest.mark.parametrize('ref', [None, reference(primary_intent='DETAIL_QUERY'),
    reference(dimensions=['医院等级']), reference(fields=['医院名称']),
    reference(dimensions=['未匹配维度']), reference(operators=['GROUP_BY']),
    reference(filters=[{'value':['上海','北京']}]),
])
def test_missing_or_non_scalar_upstream_context_does_not_remove_grain(ref):
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    assert agent._repair_unrequested_metric_grouping(content, catalog(), '医院总数', ref) == (content, [])


def test_unparsed_upstream_dimension_objects_are_not_treated_as_empty():
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    extraction = {'维度':[{'name':'医院名称'}]}
    assert agent._repair_unrequested_metric_grouping(content, catalog(), '医院总数', reference(), extraction) == (content, [])


def test_metricless_detail_is_not_changed():
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['metrics'] = []
    content = json.dumps(ast)
    assert agent._repair_unrequested_metric_grouping(content, catalog(), '医院名单', reference()) == (content, [])


def test_range_and_metric_sort_survive_removing_unrequested_name_projection():
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['sort'] = {'field':CODE,'field_type':'metric','direction':'DESC'}
    ast['time_context'] = {'type':'range','start':'2025-01-01','end':'2025-12-31'}
    content, _ = agent._repair_unrequested_metric_grouping(json.dumps(ast), catalog(), '最近一年医院总数',
        reference(operators=['FILTER','AGGREGATE','RENDER_TABLE']))
    actual = json.loads(content)
    assert actual['dimensions'] == []
    assert actual['time_context'] == ast['time_context']
    assert actual['sort'] == ast['sort']


@pytest.mark.parametrize('model_adds_dimension', [False, True])
@pytest.mark.parametrize('question', ['查询上海地区的区域全部医院总数', '查询上海地区医院总数'])
def test_main_scalar_shape_independent_of_wording_and_model_projection(monkeypatch, model_adds_dimension, question):
    ast = draft([{'name':'hospital.hospital_name'}] if model_adds_dimension else [])
    ast['filters'] = []  # No source database calls: the grain under test is independent of filter binding.
    class Builder:
        def __init__(self, *args, **kwargs): self.last_knowledge = deepcopy(catalog())
        def build(self, query): return 'offline scoped catalog'
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: object())
    monkeypatch.setattr(agent, 'create_deep_agent', lambda **kwargs: SimpleNamespace(
        invoke=lambda _: {'messages':[SimpleNamespace(content=json.dumps(ast))]}))
    monkeypatch.setattr(agent, '_normalize_dynamic_subject', lambda value, *args: value)
    # Evidence lookup is I/O; final grounding/shape validation remains real.
    monkeypatch.setattr(agent, '_build_semantic_evidence', lambda *args: {})
    result = agent.main(question, store=object(), semantic_model_id=81, business_domain_ids=[205],
        structured_extraction={'意图':'统计查询','实体':['医院'], '指标':[{'name':NAME}], '维度':[], '展示字段':[]},
        include_evidence=True)
    actual = json.loads(result['result'])
    assert actual['dimensions'] == []
    assert actual['metrics'][0]['name'] == CODE
    assert actual['metrics'][0]['alias'] == NAME
    assert result['asl_validation'] == 'PASS'
