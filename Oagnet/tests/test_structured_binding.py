import json
from copy import deepcopy
from types import SimpleNamespace as Obj
import pytest
from structured_binding import bind


@pytest.mark.parametrize('value,expected', [
    ({'field':'分类','value':['透析器']}, '透析器'),
    ({'name':'销售额'}, '销售额'), ('医院', '医院'),
])
def test_clarification_phrase_is_a_business_literal_not_json(value, expected):
    from structured_binding import issue
    result = issue('过滤条件[1]', value, '请选择', ['标准候选'])
    assert result['phrase'] == expected
    assert json.dumps(value, ensure_ascii=False) in result['question']


def test_retrieval_preserves_each_filter_role_and_literal():
    from structured_binding import filter_value_queries
    e = {'过滤条件': [
        {'field': '商品品类', 'value': ['血液透析器']},
        {'entity': '医院', 'field': '名称', 'value': ['华山医院']},
        {'field': '金额', 'value': [1000]},
    ], '输出要求': '不应从这里重新提取关键词'}
    terms = filter_value_queries(e)
    assert '商品品类 血液透析器' in terms
    assert '医院 名称 华山医院' in terms
    assert len(terms) == len(set(terms))
    assert not any('不应' in term or '1000' in term for term in terms)


def test_each_structured_filter_recalls_its_own_scoped_value_pool():
    from prompt_build import PromptBuilder
    from vector_store import SearchResult
    calls = []
    hit = SearchResult(id='category-value', score=0.8, text='standard category',
        metadata={'type': 'entity_attribute_value', 'semantic_model_id': 81,
                  'business_domain_id': 205, 'attr_value': '标准分类'})
    class Store:
        def search(self, vector, *, top_k, where):
            calls.append((vector, top_k, where))
            return [hit] if vector == [2] and top_k == 40 else []
    builder = PromptBuilder(Store(), lambda text: [2] if text == '分类 值' else [1],
                            semantic_model_id=81, business_domain_ids=[205])
    builder.value_queries = ['分类 值', '分类 值']
    result = builder.retrieve('long structured request')
    assert hit in result['_ambiguity_candidates']['entity_attribute_value']
    assert [(k, w) for v, k, w in calls if v == [2]] == [
        (40, builder._build_where('entity_attribute_value'))]


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


def dealer_list_fixture():
    e, _, _ = fixture()
    e.update(实体=['商品', '经销商'], 指标=[],
             展示字段=[{'entity': '经销商', 'field': '经销商名称'}],
             过滤条件=[{'field': '商品名称', 'op': '=', 'value': ['空心纤维血液透析器']}])
    k = {'entities': [Obj(metadata={'entity_code': code, 'attributes': [
             {'attr_code': column, 'field_mapping': f'{code}.{column}'}]})
             for code, column in [('product', 'product_name'), ('dealer', 'dealer_name'), ('sales_order', 'order_id')]],
         'relations': [Obj(metadata={'join_key': {'source_field': a, 'target_field': b}})
                       for a, b in [('sales_order.product_code', 'product.product_code'),
                                    ('sales_order.dealer_code', 'dealer.dealer_code')]],
         'entity_attribute_values': [Obj(metadata={'source_field': 'product.product_name', 'attr_value': '空心纤维血液透析器'})],
         '_vector_authorized_fields': ['product.product_name', 'dealer.dealer_name', 'sales_order.order_id']}
    p = {'display_fields': [{'index': 0, 'key': 'dealer.dealer_name'}],
         'filters': [{'index': 0, 'key': 'product.product_name', 'value_ids': [0]}]}
    return e, k, p


@pytest.mark.parametrize('subject', [None, '', 'unknown_entity'])
def test_multiple_declared_entities_bind_unique_catalog_hub_without_question(subject):
    from agent import _detail_projection_subject_candidate
    e, k, p = dealer_list_fixture()
    p['subject'] = subject
    ast, repairs = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))),
                        detail_subject_resolver=_detail_projection_subject_candidate)
    assert not ast['ambiguity']
    assert ast['subject'] == {'entity': 'sales_order'}
    assert ast['dimensions'][0]['name'] == 'dealer.dealer_name'
    assert ast['filters'] == [{'field': 'product.product_name', 'operator': '=', 'value': '空心纤维血液透析器'}]
    assert ast['metrics'] == [] and ast['time_context'] is None
    assert any(r['source'] == 'SCOPED_RELATION_GRAPH' for r in repairs)


@pytest.mark.parametrize('subject', ['product', {'entity': 'dealer'}])
def test_existing_valid_subject_or_asl_object_shape_is_preserved(subject):
    e, k, p = dealer_list_fixture()
    p['subject'] = subject
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))),
                  detail_subject_resolver=lambda *_: pytest.fail('must preserve valid subject'))
    assert not ast['ambiguity']
    assert ast['subject']['entity'] == (subject if isinstance(subject, str) else subject['entity'])


def test_missing_catalog_path_is_not_presented_as_missing_user_entity_meaning():
    from agent import _detail_projection_subject_candidate
    e, k, p = dealer_list_fixture()
    k['relations'] = []
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))),
                  detail_subject_resolver=_detail_projection_subject_candidate)
    assert not ast['subject'] and len(ast['ambiguity']) == 1
    assert '实体物理映射与关联关系' in ast['ambiguity'][0]['question']
    assert '不需要重复解释' in ast['ambiguity'][0]['question']


def test_subject_resolver_cannot_add_out_of_scope_entity():
    e, k, p = dealer_list_fixture()
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))),
                  detail_subject_resolver=lambda *_: 'other_model_sales')
    assert not ast['subject'] and ast['ambiguity']


def test_unbound_filter_is_not_hidden_by_subject_recovery():
    e, k, p = dealer_list_fixture()
    p['filters'] = []
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))),
                  detail_subject_resolver=lambda *_: pytest.fail('cannot recover incomplete bindings'))
    assert ast['ambiguity'][0]['field'] == '过滤条件[1]'


def test_metric_query_does_not_use_detail_subject_fallback():
    e, k, p = fixture()
    p.pop('subject')
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))),
                  detail_subject_resolver=lambda *_: pytest.fail('not a detail query'))
    assert ast['ambiguity'] and not ast['subject']


def test_main_reconnects_existing_subject_binding_without_reextracting(monkeypatch):
    import agent
    e, k, p = dealer_list_fixture()
    class Builder:
        def __init__(self, *args, **kwargs): pass
        def retrieve(self, text): return deepcopy(k)
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: Obj(invoke=lambda _: Obj(content=json.dumps(p))))
    monkeypatch.setattr(agent, '_validate_vector_grounded_asl', lambda *args: None)
    monkeypatch.setattr(agent, '_validate_asl_output', lambda content, *args: content)
    monkeypatch.setattr(agent, '_validate_intent_asl_contract', lambda *args: None)
    result = json.loads(agent.main('MUST NOT REEXTRACT', semantic_model_id=81,
                                   business_domain_ids=[205], structured_extraction=e))
    assert result['subject'] == {'entity': 'sales_order'}
    assert not result['ambiguity']


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


def test_category_field_and_value_must_bind_as_a_pair():
    e,k,p=fixture()
    e['过滤条件']=[{'field':'分类','op':'=','value':['透析器']}]
    p['filters']=[{'index':0,'key':'hospital.province'}]
    k['entity_attribute_values']=[Obj(metadata={'source_field':'hospital.name','attr_value':'01透析器具'})]
    ast,_=run(e,k,p)
    assert ast['ambiguity'] and not ast['filters']
    assert '条件值' in ast['ambiguity'][0]['question']
    p['filters']=[{'index':0,'key':'hospital.name','value_ids':[0]}]
    ast,_=run(e,k,p)
    assert not ast['ambiguity']
    assert ast['filters']==[{'field':'hospital.name','operator':'=','value':'01透析器具'}]
