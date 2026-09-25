import json
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from query_binding_review import binding_options, review_bindings
from test_intent_asl_contract import _entity


def catalog():
    entities = [_entity(e, label, field, label) for e, label, field in [
        ('hospital', '医院', 'hospital.province_id'),
        ('dealer', '经销商', 'dealer.province_id'),
        ('province', '省份', 'dim_province.province_name'),
        ('sales_order', '销售订单', 'sales_order.created_date'),
    ]]
    for entity in entities:
        entity.metadata['attributes'] = json.loads(entity.metadata['attributes'])
    entities[2].metadata['attributes'].append({'attr_code':'province_id', 'field_mapping':'dim_province.province_id'})
    for e in entities:
        e.metadata['business_domain_id'] = 205
    relations = [SimpleNamespace(metadata={'join_key':{
        'source_field':owner+'.province_id', 'target_field':'dim_province.province_id'},
        'relation_semantic':owner+'所在地'}) for owner in ('hospital','dealer')]
    return {'entities':entities, 'relations':relations, 'metrics':[SimpleNamespace(metadata={
        'metric_code':'cooperating_hospital_count','business_definition':{'description':'有效订单对应医院数'},
        'time_caliber':{'time_anchor':'sales_order.created_date','special_rule':None},
    })], '_vector_authorized_fields':[
        'hospital.province_id','dealer.province_id','dim_province.province_id',
        'dim_province.province_name','sales_order.created_date']}


def draft():
    return {'subject':{'entity':'sales_order'}, 'metrics':[{'name':'cooperating_hospital_count'}],
            'dimensions':[{'name':'dealer'}], 'filters':[
                {'field':'dim_province.province_name','operator':'=','value':'上海市'}],
            'time_context':{'type':'this_year','unit':'year','anchor':'sales_order.created_date'},
            'sort':None,'limit':None,'having':[],'ambiguity':[]}


def run(decision, *, ast=None, knowledge=None, query='各经销商在上海地区的已合作医院数', resolver=None, **kwargs):
    knowledge = knowledge or catalog()
    calls = []
    def invoke(messages):
        calls.append(json.loads(messages[1]['content']))
        return SimpleNamespace(content=json.dumps(decision))
    original = json.dumps(ast or draft())
    result, repairs = review_bindings(original, knowledge, query, {'维度':['经销商']},
        SimpleNamespace(invoke=invoke), resolver or (lambda *a:['310000']), 81, 205,
        today=date(2026,9,24), **kwargs)
    return json.loads(result), repairs, calls


@pytest.mark.parametrize('owner,index', [('hospital',0), ('dealer',1)])
def test_context_selects_owner_without_changing_dealer_grouping(owner,index):
    result, repairs, calls = run({'bindings':[{'filter_index':0,'choice_index':index,'reason':'上下文明确的所在地'}],
                                 'time':{'mode':'none'}})
    assert result['filters'] == [{'field':owner+'.province_id','operator':'=','value':'310000'}]
    assert result['dimensions'] == draft()['dimensions']
    assert result['metrics'] == draft()['metrics']
    assert result['subject'] == draft()['subject']
    assert result['time_context'] is None
    assert len(repairs) == 2
    assert calls[0]['structured_extraction'] == {'维度':['经销商']}


def test_two_different_location_filters_are_resolved_independently():
    ast = draft()
    ast['filters'].append(dict(ast['filters'][0],value='北京市'))
    result, _, _ = run({'bindings':[
        {'filter_index':0,'choice_index':0,'reason':'上海医院'},
        {'filter_index':1,'choice_index':1,'reason':'北京经销商'}]}, ast=ast,
        resolver=lambda m,d,c,f,v:['310000' if v=='上海市' else '110000'])
    assert [(f['field'],f['value']) for f in result['filters']] == [('hospital.province_id','310000'),('dealer.province_id','110000')]


@pytest.mark.parametrize('operator,expected', [('=','IN'),('!=','NOT IN'),('IN','IN'),('NOT IN','NOT IN')])
def test_duplicate_dictionary_keys_preserve_label_set_semantics(operator,expected):
    ast = draft()
    ast['filters'][0]['operator'] = operator
    result, _, _ = run({'bindings':[{'filter_index':0,'choice_index':0,'reason':'医院所在地'}]},
        ast=ast, resolver=lambda *a:['001','002'])
    assert result['filters'][0]['operator'] == expected
    assert result['filters'][0]['value'] == ['001','002']


@pytest.mark.parametrize('keys', [[], None])
def test_unavailable_dictionary_does_not_fabricate_or_drop_filter(keys):
    result, repairs, _ = run({'bindings':[{'filter_index':0,'choice_index':0,'reason':'医院所在地'}]},
                             resolver=lambda *a:keys)
    assert result == draft()
    assert repairs == []


def test_missing_relation_or_field_cannot_authorize_owner():
    knowledge = catalog()
    knowledge['_vector_authorized_fields'].remove('hospital.province_id')
    options = binding_options(draft(),knowledge)
    assert [c['field'] for c in options[0]['choices']] == ['dealer.province_id']
    knowledge['relations'] = []
    assert binding_options(draft(),knowledge) == []


def test_explicit_rolling_period_uses_real_month_arithmetic_not_calendar_year():
    knowledge = catalog()
    knowledge['metrics'][0].metadata['time_caliber']['special_rule'] = '未指定时最近12个月'
    result, _, _ = run({'time':{'mode':'rolling','source':'question','evidence':'最近12个月',
        'amount':12,'unit':'month','anchor':'sales_order.created_date'}},knowledge=knowledge,
        query='最近12个月各经销商在上海地区的已合作医院数')
    assert result['time_context'] == {'type':'range','start':'2025-09-24','end':'2026-09-24',
                                     'unit':'day','anchor':'sales_order.created_date'}


@pytest.mark.parametrize('mode', ['none','rolling'])
def test_explicit_time_cannot_be_cleared_or_overridden_by_default(mode):
    knowledge = catalog()
    knowledge['metrics'][0].metadata['time_caliber']['special_rule'] = '默认最近6个月'
    result, _, _ = run({'time':{'mode':mode,'source':'catalog','evidence':'默认最近6个月',
        'amount':6,'unit':'month','anchor':'sales_order.created_date'}},knowledge=knowledge, explicit_time=True)
    assert result['time_context'] == draft()['time_context']


@pytest.mark.parametrize('time', [
    {'mode':'rolling','source':'catalog','evidence':'默认最近12个月','amount':12,'unit':'month','anchor':'sales_order.created_date'},
    {'mode':'rolling','source':'question','evidence':'上海','amount':0,'unit':'month','anchor':'sales_order.created_date'},
    {'mode':'rolling','source':'question','evidence':'上海','amount':12,'unit':'month','anchor':'private.secret'},
])
def test_unproven_rule_or_invalid_time_cannot_change_draft(time):
    result, repairs, _ = run({'time':time})
    assert result == draft()
    assert repairs == []


def test_unselected_metric_cannot_supply_default_rule():
    knowledge = catalog()
    extra = deepcopy(knowledge['metrics'][0])
    extra.metadata.update(metric_code='other',time_caliber={'special_rule':'默认最近6个月'})
    knowledge['metrics'].append(extra)
    result, _, calls = run({'time':{'mode':'rolling','source':'catalog','evidence':'默认最近6个月',
        'amount':6,'unit':'month','anchor':'sales_order.created_date'}},knowledge=knowledge)
    assert result == draft()
    assert len(calls[0]['selected_policies']) == 2  # selected metric and subject


def test_review_failure_is_nonblocking():
    def fail(*a): raise TimeoutError('offline')
    original = json.dumps(draft())
    assert review_bindings(original,catalog(),'问题',None,SimpleNamespace(invoke=fail),None,81,205) == (original,[])


def test_live_dictionary_lookup_is_parameterized_and_preserves_leading_zeros(monkeypatch):
    import mysql_tool as mysql
    from unittest.mock import MagicMock
    rows = [dict(entity_code='province',business_domain_id=205,data_source_id=1,
                 mapping_table='dim_province',mapping_column=col,db_type='mysql',host='unused',port=1,
                 username='unused',db_name='unused') for col in ['province_name','province_id']]
    monkeypatch.setattr(mysql,'_authorized_entity_field_rows',lambda *a,**kw:(
        [('province',205,'dim_province',r['mapping_column']) for r in rows],rows))
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [('001',),('002',)]
    monkeypatch.setattr(mysql.pymysql,'connect',lambda **kw:connection)
    value = "x' OR 1=1 --"
    assert mysql.resolve_scoped_dictionary_keys(81,205,{'dictionary_entity':'province',
        'dictionary_key':'dim_province.province_id','business_domain_id':205},
        'dim_province.province_name',value) == ['001','002']
    sql, params = cursor.execute.call_args.args
    assert value not in sql and params == (value,)
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


def test_dictionary_lookup_rejects_cross_source_pair(monkeypatch):
    import mysql_tool as mysql
    monkeypatch.setattr(mysql,'_authorized_entity_field_rows',lambda *a,**kw:(
        [('province',205,'dim_province','province_name'),('province',205,'dim_province','province_id')],
        [dict(entity_code='province',business_domain_id=205,data_source_id=i,mapping_table='dim_province') for i in [1,2]]))
    assert mysql.resolve_scoped_dictionary_keys(81,205,{'dictionary_entity':'province',
        'dictionary_key':'dim_province.province_id','business_domain_id':205},'dim_province.province_name','上海市') == []


def test_trend_contract_can_group_time_without_filtering_to_default_year():
    from asl_contract import IntentASLContract
    contract = IntentASLContract(version='1.0',intent='TREND_ANALYSIS',metric_required=True,
        required_metrics=['销售额'],time_dimension_required=True,time_policy='OPTIONAL')
    assert contract.canonical_time_range is None
