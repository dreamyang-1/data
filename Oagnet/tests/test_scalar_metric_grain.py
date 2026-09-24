import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import agent


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


@pytest.mark.parametrize('question', ['查询上海地区的区域全部医院总数', '查询上海地区医院总数'])
def test_metric_name_is_not_an_instruction_to_group_by_hospital(question):
    ast = draft()
    actual = json.loads(agent._normalize_semantic_references(json.dumps(ast), catalog(), question))
    assert actual['dimensions'] == []


def reference(**updates):
    return {'primary_intent':'统计查询', 'dimensions':[], 'fields':[],
            'metrics':[{'name':NAME}], 'filters':[], **updates}


def reviewer(shape='scalar', dimensions=None, reason='完整问题要求筛选范围内合计，不需要逐家结果。'):
    return SimpleNamespace(invoke=lambda messages: SimpleNamespace(content=json.dumps({
        'shape':shape, 'dimensions':dimensions or [], 'reason':reason})))


def review(content, question, ref=None, extraction=None, model=None):
    return agent._review_metric_query_grain(content, catalog(), question, ref, extraction, model or reviewer())


@pytest.mark.parametrize('question', [
    '查询上海地区的区域全部医院总数', '查询上海地区医院总数',
    '上海医院一共有多少家', '统计上海所有医院的总数',
    '最近一年上海医院总数',
])
def test_scalar_paraphrases_remove_model_added_grouping_without_changing_filters(question):
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['sort'] = {'field':'hospital.hospital_name','field_type':'dimension','direction':'ASC'}
    content, repairs = review(json.dumps(ast), question, reference())
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
    assert review(content, question, reference(), model=reviewer('grouped', ast['dimensions'])) == (content, [])


@pytest.mark.parametrize('ref', [None, reference(primary_intent='DETAIL_QUERY'),
    reference(dimensions=['医院等级']), reference(fields=['医院名称']),
    reference(dimensions=['未匹配维度']), reference(operators=['GROUP_BY']),
    reference(filters=[{'value':['上海','北京']}]),
])
def test_upstream_hints_do_not_override_contextual_model_decision(ref):
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    actual, _ = review(content, '只要上海合计，不按医院拆开', ref)
    assert json.loads(actual)['dimensions'] == []


def test_unparsed_upstream_dimension_objects_are_not_treated_as_empty():
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    extraction = {'维度':[{'name':'医院名称'}]}
    assert review(content, '各医院的统计', reference(), extraction,
                  reviewer('grouped', [{'name':'hospital.hospital_name'}])) == (content, [])


def test_metricless_detail_is_not_changed():
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['metrics'] = []
    content = json.dumps(ast)
    assert review(content, '医院名单', reference()) == (content, [])


def test_range_and_metric_sort_survive_removing_unrequested_name_projection():
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['sort'] = {'field':CODE,'field_type':'metric','direction':'DESC'}
    ast['time_context'] = {'type':'range','start':'2025-01-01','end':'2025-12-31'}
    content, _ = review(json.dumps(ast), '最近一年医院总数',
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
    monkeypatch.setattr(agent, '_get_chat_model', reviewer)
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


@pytest.mark.parametrize('question', ['不要按医院拆开，只要全市合计', '各家医院加起来一共有多少家'])
def test_negation_and_combined_totals_are_not_keyword_grouping(question):
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    actual, repairs = review(content, question, reference())
    assert json.loads(actual)['dimensions'] == []
    assert repairs[0]['query_shape'] == 'scalar'


def test_grouping_can_be_added_by_context_despite_empty_upstream_dimensions():
    content = json.dumps(draft())
    actual, repairs = review(content, '医院数量最多的是哪个省份', reference(),
                            model=reviewer('grouped', [{'name':'hospital.province'}], '需要比较各省的医院数量。'))
    assert json.loads(actual)['dimensions'] == [{'name':'hospital.province'}]
    assert repairs[0]['query_shape'] == 'grouped'


@pytest.mark.parametrize('decision', [
    {'shape':'scalar','dimensions':[{'name':'hospital.province'}],'reason':'bad'},
    {'shape':'grouped','dimensions':[],'reason':'bad'},
    {'shape':'grouped','dimensions':[{'name':'private.secret'}],'reason':'bad'},
    {'shape':'grouped','dimensions':[{'name':'hospital.province','sql':'anything'}],'reason':'bad'},
    {'shape':'detail','dimensions':[],'reason':'detail'},
    {'shape':'scalar','dimensions':[]},
    None, [], 'invalid JSON',
])
def test_invalid_review_cannot_change_original_or_expand_scope(decision):
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    model = SimpleNamespace(invoke=lambda _: SimpleNamespace(content=json.dumps(decision)))
    assert review(content, '任意完整问题', reference(), model=model) == (content, [])


def test_review_failure_is_nonblocking_and_not_retried():
    calls = []
    def fail(messages):
        calls.append(messages)
        raise TimeoutError('offline')
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    assert review(content, '完整问题', model=SimpleNamespace(invoke=fail)) == (content, [])
    assert len(calls) == 1


def test_review_receives_full_question_original_extraction_and_catalog():
    captured = []
    def invoke(messages):
        captured.append(json.loads(messages[1]['content']))
        return reviewer().invoke(messages)
    extraction = {'意图':'统计查询','实体':['医院'],'维度':[],'展示字段':[]}
    content = json.dumps(draft([{'name':'hospital.hospital_name'}]))
    actual, _ = review(content, '不要逐家，给我合计', reference(), extraction, SimpleNamespace(invoke=invoke))
    assert len(captured) == 1
    assert captured[0]['completed_question'] == '不要逐家，给我合计'
    assert captured[0]['structured_extraction'] == extraction
    assert captured[0]['scoped_catalog']['metrics'][0]['metric_code'] == CODE
    for key in ('metrics','filters','subject','time_context','limit','having'):
        assert json.loads(actual)[key] == json.loads(content)[key]
